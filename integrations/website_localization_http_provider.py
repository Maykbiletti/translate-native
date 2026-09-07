#!/usr/bin/env python3
"""Provider-neutral, request-bound HTTP adapter for localization LLM calls."""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable, Mapping, Protocol


REQUEST_SCHEMA = "blun.localization-provider-request.v1"
RESPONSE_SCHEMA = "blun.localization-provider-response.v1"
MAX_BODY_BYTES = 4_000_000
MAX_ENDPOINT_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 4096
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
REQUEST_ID = re.compile(r"^blun-l10n-call-[a-f0-9]{64}$")
WORKER_REQUEST_SCHEMA = "blun.website-localization-worker.v1"
WORKER_REQUEST_FIELDS = {
    "schema", "request_id", "phase", "provider_id", "model_id",
    "model_version", "system_instruction", "input",
}
WORKER_PHASES = {"transcreation", "target_native", "source_fidelity"}
RESERVED_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host",
    "idempotency-key", "transfer-encoding", "x-localization-request-id",
    "x-localization-request-sha256",
}


class HTTPProviderFailed(RuntimeError):
    """Content-free failure consumed by the generic localization worker."""

    localization_provider_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("provider error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("provider retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, *, timeout: float) -> HTTPResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class URLTransport:
    """One-attempt stdlib HTTPS transport with redirects disabled."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def post(self, url: str, headers: Mapping[str, str], body: bytes, *, timeout: float) -> HTTPResult:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            headers = tuple(error.headers.items()) if error.headers is not None else ()
            return HTTPResult(int(error.code), headers, b"")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise HTTPProviderFailed("network", retryable=True) from None
        try:
            raw = response.read(MAX_BODY_BYTES + 1)
            return HTTPResult(int(response.status), tuple(response.headers.items()), raw)
        except (TimeoutError, socket.timeout, OSError):
            raise HTTPProviderFailed("network", retryable=True) from None
        finally:
            response.close()


def _canonical_json(value: Any) -> bytes:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise HTTPProviderFailed("request_invalid", retryable=False) from None
    raw = text.encode("utf-8")
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise HTTPProviderFailed("request_too_large", retryable=False)
    return raw


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite number")


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str) or value != value.strip() or len(value) > MAX_ENDPOINT_LENGTH
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint is invalid") from None
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("endpoint is invalid")
    if not hostname or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise ValueError("endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and allow_loopback_http and hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        pass
    else:
        raise ValueError("endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint is invalid")
    return value


def _auth_headers(provider: Callable[[], Mapping[str, str]]) -> dict[str, str]:
    try:
        supplied = provider()
    except Exception:
        raise HTTPProviderFailed("authentication", retryable=False) from None
    if not isinstance(supplied, Mapping):
        raise HTTPProviderFailed("authentication", retryable=False)
    result = {}
    normalized_names = set()
    for name, value in supplied.items():
        normalized = name.lower() if isinstance(name, str) else ""
        if (
            not isinstance(name, str) or HEADER_NAME.fullmatch(name) is None
            or normalized in RESERVED_HEADERS or normalized in normalized_names
        ):
            raise HTTPProviderFailed("authentication", retryable=False)
        if (
            not isinstance(value, str) or not value or len(value) > MAX_HEADER_VALUE_LENGTH
            or "\r" in value or "\n" in value or any(ord(char) < 32 for char in value)
            or any(ord(char) == 127 for char in value)
        ):
            raise HTTPProviderFailed("authentication", retryable=False)
        normalized_names.add(normalized)
        result[name] = value
    if not result:
        raise HTTPProviderFailed("authentication", retryable=False)
    return result


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise HTTPProviderFailed("transport_invalid", retryable=True)
    result = {}
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise HTTPProviderFailed("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in result:
            raise HTTPProviderFailed("response_headers", retryable=True)
        result[name] = content
    return result


class HTTPProviderAdapter:
    """One HTTP attempt for one deterministic worker phase."""

    def __init__(
        self,
        endpoint: str,
        authentication_headers: Callable[[], Mapping[str, str]],
        *,
        transport: HTTPTransport | None = None,
        timeout: float = 60.0,
        allow_loopback_http: bool = False,
    ):
        if not isinstance(allow_loopback_http, bool):
            raise TypeError("allow_loopback_http must be boolean")
        self.endpoint = _endpoint(endpoint, allow_loopback_http)
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("timeout is outside the supported range")
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.timeout = float(timeout)

    def invoke(self, request: Any) -> Mapping[str, Any]:
        try:
            payload_method = getattr(request, "as_payload", None)
            request_id = getattr(request, "request_id", None)
        except Exception:
            raise HTTPProviderFailed("request_invalid", retryable=False) from None
        if not callable(payload_method) or not isinstance(request_id, str) or not request_id:
            raise HTTPProviderFailed("request_invalid", retryable=False)
        try:
            payload = payload_method()
        except Exception:
            raise HTTPProviderFailed("request_invalid", retryable=False) from None
        if (
            REQUEST_ID.fullmatch(request_id) is None
            or not isinstance(payload, dict)
            or set(payload) != WORKER_REQUEST_FIELDS
            or payload.get("schema") != WORKER_REQUEST_SCHEMA
            or payload.get("request_id") != request_id
            or payload.get("phase") not in WORKER_PHASES
            or not isinstance(payload.get("input"), dict)
            or any(not isinstance(payload.get(field), str) or not payload[field] for field in (
                "provider_id", "model_id", "model_version", "system_instruction",
            ))
        ):
            raise HTTPProviderFailed("request_invalid", retryable=False)
        request_bytes = _canonical_json(payload)
        request_hash = sha256(request_bytes).hexdigest()
        envelope = {
            "schema": REQUEST_SCHEMA,
            "request_id": request_id,
            "request_sha256": request_hash,
            "request": payload,
        }
        body = _canonical_json(envelope)
        headers = _auth_headers(self.authentication_headers)
        headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": request_id,
            "X-Localization-Request-Id": request_id,
            "X-Localization-Request-Sha256": request_hash,
        })
        try:
            result = self.transport.post(self.endpoint, headers, body, timeout=self.timeout)
        except HTTPProviderFailed:
            raise
        except Exception:
            raise HTTPProviderFailed("network", retryable=True) from None
        if (
            not isinstance(result, HTTPResult) or isinstance(result.status, bool)
            or not isinstance(result.status, int) or not 100 <= result.status <= 599
        ):
            raise HTTPProviderFailed("transport_invalid", retryable=True)
        if result.status != 200:
            if 300 <= result.status <= 399:
                raise HTTPProviderFailed("redirect", retryable=False)
            retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            raise HTTPProviderFailed("http_status", retryable=retryable)
        response_headers = _headers(result.headers)
        content_type = response_headers.get("content-type", "").lower().replace(" ", "")
        if content_type not in {"application/json", "application/json;charset=utf-8"}:
            raise HTTPProviderFailed("response_content_type", retryable=True)
        if not isinstance(result.body, bytes) or not result.body or len(result.body) > MAX_BODY_BYTES:
            raise HTTPProviderFailed("response_size", retryable=True)
        declared = response_headers.get("content-length")
        if declared is not None and (not declared.isascii() or not declared.isdecimal() or int(declared) != len(result.body)):
            raise HTTPProviderFailed("response_size", retryable=True)
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            envelope = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise HTTPProviderFailed("response_json", retryable=True) from None
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema", "request_id", "request_sha256", "response",
        }:
            raise HTTPProviderFailed("response_binding", retryable=False)
        if (
            envelope["schema"] != RESPONSE_SCHEMA
            or envelope["request_id"] != request_id
            or envelope["request_sha256"] != request_hash
            or not isinstance(envelope["response"], dict)
        ):
            raise HTTPProviderFailed("response_binding", retryable=False)
        return envelope["response"]
