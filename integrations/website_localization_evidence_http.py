#!/usr/bin/env python3
"""Request-bound HTTPS transport for one localization quality-evidence job."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


REQUEST_SCHEMA = "blun.localization-quality-evidence-http-request.v1"
RESPONSE_SCHEMA = "blun.localization-quality-evidence-http-response.v1"
EVIDENCE_REQUEST_SCHEMA = "blun.localization-quality-evidence-request.v4"
EVIDENCE_RESPONSE_SCHEMA = "blun.localization-quality-evidence-response.v2"
MAX_ENDPOINT_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 4096
MAX_TEXT_BYTES = 2_000_000
MAX_REQUEST_BYTES = 8_500_000
MAX_RESPONSE_BYTES = 1_000_000
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
REQUEST_ID = re.compile(r"^blun-l10n-evidence-[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
REQUEST_FIELDS = {
    "schema", "request_id", "evidence_revision", "event_id", "plan_id",
    "job_id", "result_sha256", "source_sha256", "target_sha256",
    "source_locale", "target_locale", "content_type", "glossary_version",
    "policy_version", "provider", "software_version", "source_text",
    "target_text", "review_confidence", "quality_profile",
    "commercial_profile", "human_review_required",
    "independent_review_required",
}
EVIDENCE_FIELDS = {
    "schema", "request_id", "result_sha256", "quality_receipt",
    "human_review_receipt", "independent_model_review",
}
RESERVED_HEADERS = {
    "accept", "accept-encoding", "connection", "content-length",
    "content-type", "host", "idempotency-key", "transfer-encoding",
    "x-localization-evidence-request-id",
    "x-localization-evidence-request-sha256",
}


class HTTPEvidenceProviderFailed(RuntimeError):
    """Content-free adapter failure understood by release coordination."""

    localization_evidence_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("HTTP evidence error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("HTTP evidence retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
    ) -> HTTPResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class URLTransport:
    """Perform one bounded HTTP request without following redirects."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
    ) -> HTTPResult:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST",
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response_headers = tuple(error.headers.items()) if error.headers else ()
            return HTTPResult(int(error.code), response_headers, b"")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise HTTPEvidenceProviderFailed("network", retryable=True) from None
        try:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), response_body,
            )
        except (TimeoutError, socket.timeout, OSError):
            raise HTTPEvidenceProviderFailed("network", retryable=True) from None
        finally:
            response.close()


def _canonical_json(value: Any, *, code: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise HTTPEvidenceProviderFailed(code, retryable=False) from None
    if not encoded or len(encoded) > maximum:
        raise HTTPEvidenceProviderFailed(code, retryable=False)
    return encoded


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or not value.isascii()
        or len(value) > MAX_ENDPOINT_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint is invalid") from None
    if (
        not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ValueError("endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and allow_loopback_http
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        pass
    else:
        raise ValueError("endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint is invalid")
    return value


def _authentication_headers(
    provider: Callable[[], Mapping[str, str]],
) -> dict[str, str]:
    try:
        supplied = provider()
    except Exception:
        raise HTTPEvidenceProviderFailed("authentication", retryable=False) from None
    if not isinstance(supplied, Mapping) or not supplied:
        raise HTTPEvidenceProviderFailed("authentication", retryable=False)
    result: dict[str, str] = {}
    normalized_names: set[str] = set()
    for name, value in supplied.items():
        normalized = name.lower() if isinstance(name, str) else ""
        if (
            not isinstance(name, str)
            or HEADER_NAME.fullmatch(name) is None
            or normalized in RESERVED_HEADERS
            or normalized in normalized_names
            or not isinstance(value, str)
            or not value
            or not value.isascii()
            or len(value) > MAX_HEADER_VALUE_LENGTH
            or "\r" in value
            or "\n" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise HTTPEvidenceProviderFailed("authentication", retryable=False)
        normalized_names.add(normalized)
        result[name] = value
    return result


def _response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise HTTPEvidenceProviderFailed("transport_invalid", retryable=True)
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise HTTPEvidenceProviderFailed("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                raise HTTPEvidenceProviderFailed("response_headers", retryable=True)
            result[name] = content
    return result


def _token(value: Any) -> bool:
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _safe_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\x00" not in value
        and unicodedata.is_normalized("NFC", value)
        and len(value.encode("utf-8")) <= MAX_TEXT_BYTES
    )


def _request_payload(request: Any) -> tuple[dict[str, Any], bytes]:
    try:
        payload = request.as_payload()
        request_id = request.request_id
    except Exception:
        raise HTTPEvidenceProviderFailed("request_invalid", retryable=False) from None
    if (
        not isinstance(payload, dict)
        or set(payload) != REQUEST_FIELDS
        or payload.get("schema") != EVIDENCE_REQUEST_SCHEMA
        or payload.get("request_id") != request_id
        or not isinstance(request_id, str)
        or REQUEST_ID.fullmatch(request_id) is None
    ):
        raise HTTPEvidenceProviderFailed("request_invalid", retryable=False)
    token_fields = (
        "evidence_revision", "event_id", "plan_id", "job_id",
        "source_locale", "target_locale", "content_type", "glossary_version",
        "policy_version", "software_version",
    )
    if any(not _token(payload[field]) for field in token_fields):
        raise HTTPEvidenceProviderFailed("request_invalid", retryable=False)
    for field in ("result_sha256", "source_sha256", "target_sha256"):
        if not isinstance(payload[field], str) or SHA256.fullmatch(payload[field]) is None:
            raise HTTPEvidenceProviderFailed("request_invalid", retryable=False)
    source_text = payload["source_text"]
    target_text = payload["target_text"]
    if (
        not _safe_text(source_text)
        or not _safe_text(target_text)
        or hashlib.sha256(source_text.encode("utf-8")).hexdigest() != payload["source_sha256"]
        or hashlib.sha256(target_text.encode("utf-8")).hexdigest() != payload["target_sha256"]
    ):
        raise HTTPEvidenceProviderFailed("request_invalid", retryable=False)
    provider = payload["provider"]
    confidence = payload["review_confidence"]
    profile = payload["quality_profile"]
    if (
        not isinstance(provider, dict)
        or set(provider) != {"id", "model_id", "model_version"}
        or any(not _token(provider[field]) for field in provider)
        or not isinstance(confidence, dict)
        or set(confidence) != {"target_native", "source_fidelity"}
        or any(value not in {"high", "low"} for value in confidence.values())
        or not isinstance(profile, dict)
        or set(profile) != {"locale", "version", "sha256"}
        or profile["locale"] != payload["target_locale"]
        or not _token(profile["version"])
        or not isinstance(profile["sha256"], str)
        or SHA256.fullmatch(profile["sha256"]) is None
        or type(payload["human_review_required"]) is not bool
        or type(payload["independent_review_required"]) is not bool
        or (
            payload["commercial_profile"] is not None
            and not _token(payload["commercial_profile"])
        )
    ):
        raise HTTPEvidenceProviderFailed("request_invalid", retryable=False)
    return payload, _canonical_json(
        payload, code="request_invalid", maximum=MAX_REQUEST_BYTES,
    )


class HTTPEvidenceProviderAdapter:
    """Perform one HTTPS attempt for one leased quality-evidence request."""

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
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.timeout = float(timeout)

    def obtain(self, request: Any) -> Mapping[str, Any]:
        payload, request_bytes = _request_payload(request)
        request_hash = hashlib.sha256(request_bytes).hexdigest()
        envelope = {
            "schema": REQUEST_SCHEMA,
            "request_id": payload["request_id"],
            "request_sha256": request_hash,
            "request": payload,
        }
        body = _canonical_json(
            envelope, code="request_invalid", maximum=MAX_REQUEST_BYTES,
        )
        headers = _authentication_headers(self.authentication_headers)
        headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": payload["request_id"],
            "X-Localization-Evidence-Request-Id": payload["request_id"],
            "X-Localization-Evidence-Request-Sha256": request_hash,
        })
        try:
            result = self.transport.post(
                self.endpoint, headers, body, timeout=self.timeout,
            )
        except HTTPEvidenceProviderFailed:
            raise
        except Exception:
            raise HTTPEvidenceProviderFailed("network", retryable=True) from None
        if (
            not isinstance(result, HTTPResult)
            or isinstance(result.status, bool)
            or not isinstance(result.status, int)
            or not 100 <= result.status <= 599
        ):
            raise HTTPEvidenceProviderFailed("transport_invalid", retryable=True)
        if result.status != 200:
            if 300 <= result.status <= 399:
                raise HTTPEvidenceProviderFailed("redirect", retryable=False)
            retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            raise HTTPEvidenceProviderFailed("http_status", retryable=retryable)
        response_headers = _response_headers(result.headers)
        content_type = response_headers.get("content-type", "").lower().replace(" ", "")
        if content_type not in {"application/json", "application/json;charset=utf-8"}:
            raise HTTPEvidenceProviderFailed("response_content_type", retryable=True)
        if (
            not isinstance(result.body, bytes)
            or not result.body
            or len(result.body) > MAX_RESPONSE_BYTES
        ):
            raise HTTPEvidenceProviderFailed("response_size", retryable=True)
        declared = response_headers.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdecimal()
            or int(declared) != len(result.body)
        ):
            raise HTTPEvidenceProviderFailed("response_size", retryable=True)
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            response_envelope = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise HTTPEvidenceProviderFailed("response_json", retryable=True) from None
        if not isinstance(response_envelope, dict) or set(response_envelope) != {
            "schema", "request_id", "request_sha256", "result_sha256", "evidence",
        }:
            raise HTTPEvidenceProviderFailed("response_binding", retryable=False)
        evidence = response_envelope["evidence"]
        if (
            response_envelope["schema"] != RESPONSE_SCHEMA
            or response_envelope["request_id"] != payload["request_id"]
            or response_envelope["request_sha256"] != request_hash
            or response_envelope["result_sha256"] != payload["result_sha256"]
            or not isinstance(evidence, dict)
            or set(evidence) != EVIDENCE_FIELDS
            or evidence.get("schema") != EVIDENCE_RESPONSE_SCHEMA
            or evidence.get("request_id") != payload["request_id"]
            or evidence.get("result_sha256") != payload["result_sha256"]
        ):
            raise HTTPEvidenceProviderFailed("response_binding", retryable=False)
        return evidence
