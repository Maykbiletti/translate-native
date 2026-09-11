#!/usr/bin/env python3
"""Contract-pinned HTTPS client for the durable source-CMS ingress.

Website backends use this adapter to enqueue one immutable source change or
removal and to inspect only content-free operational state. Every operation
first verifies the live public contract, performs one bounded request against
one fixed origin, and binds the response to the request and contract digest.
Retry scheduling remains the caller's responsibility.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import socket
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


MAX_ENDPOINT_LENGTH = 2_048
MAX_HEADER_VALUE_LENGTH = 4_096
# Leave six slots for Host, Content-Length, Accept, Content-Type, idempotency,
# and the exact request-body binding header at the WSGI boundary.
MAX_AUTHENTICATION_HEADERS = 58
SHA256 = re.compile(r"^[a-f0-9]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
RESERVED_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host",
    "idempotency-key", "transfer-encoding",
    "x-localization-source-payload-sha256",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source client dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_HTTP = _load_module(
    "blun_website_localization_cms_source_client_http",
    _ROOT / "integrations" / "website_localization_cms_source_http.py",
)
_CMS = _load_module(
    "blun_website_localization_cms_source_client_cms",
    _ROOT / "integrations" / "website_localization_cms.py",
)


class CMSSourceClientBlocked(RuntimeError):
    """Stable, content-free client failure with an explicit retry decision."""

    cms_source_client_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool):
            raise ValueError("source client failure is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: float,
    ) -> HTTPResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class URLTransport:
    """Perform one bounded HTTPS request without following redirects."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, url, headers, body, *, timeout):
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            _fail("network", retryable=True)
        try:
            raw = response.read(_HTTP.MAX_BODY_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), raw,
            )
        except (TimeoutError, socket.timeout, OSError):
            _fail("network", retryable=True)
        finally:
            response.close()


def _fail(code: str, *, retryable: bool = False) -> None:
    raise CMSSourceClientBlocked(
        "source_client." + code, retryable=retryable,
    )


def _canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _fail("request_invalid")
    if not raw or len(raw) > _HTTP.MAX_BODY_BYTES:
        _fail("request_invalid")
    return raw


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ValueError("non-finite number")


def _token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and TOKEN.fullmatch(value) is not None
        and unicodedata.is_normalized("NFC", value)
    )


def _count(value: Any, *, maximum: int = 1_000_000) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= maximum
    )


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or not value.isascii()
        or len(value) > MAX_ENDPOINT_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("origin is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("origin is invalid") from None
    secure = parsed.scheme == "https"
    loopback = (
        parsed.scheme == "http"
        and allow_loopback_http
        and isinstance(hostname, str)
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        not (secure or loopback)
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65_535
        or value != f"{parsed.scheme}://{parsed.netloc}"
    ):
        raise ValueError("origin must be one exact HTTPS origin")
    return value


def _authentication_headers(provider, context) -> dict[str, str]:
    try:
        supplied = provider(dict(context))
        if not isinstance(supplied, Mapping):
            raise TypeError
        items = tuple(supplied.items())
    except Exception:
        _fail("authentication")
    if len(items) > MAX_AUTHENTICATION_HEADERS:
        _fail("authentication")
    result: dict[str, str] = {}
    names: set[str] = set()
    for name, value in items:
        normalized = name.lower() if isinstance(name, str) else ""
        if (
            not isinstance(name, str)
            or _HTTP.HEADER_NAME.fullmatch(name) is None
            or normalized in RESERVED_HEADERS
            or normalized in names
            or not isinstance(value, str)
            or not value
            or len(value) > MAX_HEADER_VALUE_LENGTH
            or "\r" in value
            or "\n" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            _fail("authentication")
        names.add(normalized)
        result[name] = value
    if not result:
        _fail("authentication")
    return result


def _response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        _fail("transport_invalid", retryable=True)
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            _fail("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in result:
            _fail("response_headers")
        result[name] = content
    return result


def _json_response(result: Any, allowed_statuses: set[int]) -> dict[str, Any]:
    if (
        not isinstance(result, HTTPResult)
        or isinstance(result.status, bool)
        or not isinstance(result.status, int)
        or not 100 <= result.status <= 599
    ):
        _fail("transport_invalid", retryable=True)
    if result.status not in allowed_statuses:
        if 300 <= result.status <= 399:
            _fail("redirect")
        retryable = result.status in {408, 425, 429} or result.status >= 500
        _fail("http_status", retryable=retryable)
    headers = _response_headers(result.headers)
    content_type = headers.get("content-type", "").lower().replace(" ", "")
    if content_type not in {"application/json", "application/json;charset=utf-8"}:
        _fail("response_content_type")
    if (
        not isinstance(result.body, bytes)
        or not result.body
        or len(result.body) > _HTTP.MAX_BODY_BYTES
    ):
        _fail("response_size")
    declared = headers.get("content-length")
    if declared is not None and (
        not declared.isascii()
        or not declared.isdecimal()
        or int(declared) != len(result.body)
    ):
        _fail("response_size")
    try:
        text = result.body.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _fail("response_json")
    if not isinstance(value, dict):
        _fail("response_binding")
    return value


def _copy_payload(value: Any, operation: str) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, Mapping):
        _fail("request_invalid")
    raw = _canonical(dict(value))
    try:
        copied = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if operation == "change":
            _CMS.WebsiteLocalizationCMSBridge._validated_event(None, copied)
        elif copied.get("schema") == _CMS.CANCELLATION_SCHEMA:
            operation = "cancellation"
            _CMS.WebsiteLocalizationCMSBridge._validated_cancellation(None, copied)
        elif copied.get("schema") == _CMS.TOMBSTONE_SCHEMA:
            operation = "tombstone"
            _CMS.WebsiteLocalizationCMSBridge._validated_tombstone(None, copied)
        else:
            raise ValueError
    except Exception:
        _fail("request_invalid")
    return copied, raw


class _PayloadView:
    def __init__(self, value: Mapping[str, Any]):
        self.value = value

    def as_payload(self) -> Mapping[str, Any]:
        return self.value


class CMSLocalizationSourceHTTPClient:
    """Operate one source runtime only through its freshly pinned contract."""

    def __init__(
        self,
        origin: str,
        expected_capabilities_sha256: str,
        authentication_headers: Callable[[Mapping[str, Any]], Mapping[str, str]],
        *,
        transport: HTTPTransport | None = None,
        timeout: float | int = 30,
        allow_loopback_http: bool = False,
    ):
        if not isinstance(allow_loopback_http, bool):
            raise TypeError("allow_loopback_http must be boolean")
        self.origin = _endpoint(origin, allow_loopback_http)
        if (
            not isinstance(expected_capabilities_sha256, str)
            or SHA256.fullmatch(expected_capabilities_sha256) is None
        ):
            raise ValueError("expected capability digest is invalid")
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        self.expected_capabilities_sha256 = expected_capabilities_sha256
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "request", None)):
            raise TypeError("transport must provide request")
        self.timeout = float(timeout)

    def __repr__(self) -> str:
        return "CMSLocalizationSourceHTTPClient(configured=True)"

    def _request(self, method, path, body, allowed_statuses, context, headers=None):
        body_sha256 = hashlib.sha256(body or b"").hexdigest()
        authentication = {
            "schema": _HTTP.AUTH_REQUEST_SCHEMA,
            "method": method,
            "origin": self.origin,
            "path": path,
            "scope": _HTTP.SCOPES[path],
            "body_sha256": body_sha256,
            **context,
        }
        request_headers = _authentication_headers(
            self.authentication_headers, authentication,
        )
        request_headers["Accept"] = "application/json"
        if body is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
            request_headers["X-Localization-Source-Payload-Sha256"] = body_sha256
        if headers:
            request_headers.update(headers)
        try:
            result = self.transport.request(
                method, self.origin + path, request_headers, body,
                timeout=self.timeout,
            )
        except CMSSourceClientBlocked:
            raise
        except Exception:
            _fail("network", retryable=True)
        return result, _json_response(result, allowed_statuses)

    def capabilities(self) -> Mapping[str, Any]:
        _result, response = self._request(
            "GET", _HTTP.CAPABILITIES_PATH, None, {200}, {},
        )
        try:
            expected = _HTTP._capabilities_payload()
        except Exception:
            _fail("local_contract_invalid")
        if (
            set(response) != {"schema", "capabilities"}
            or response.get("schema") != _HTTP.CAPABILITIES_RESPONSE_SCHEMA
            or response.get("capabilities") != expected
            or expected.get("sha256") != self.expected_capabilities_sha256
        ):
            _fail("capabilities_binding")
        return response

    def _contract(self, operation: str) -> Mapping[str, Any]:
        capabilities = self.capabilities()["capabilities"]
        return capabilities["operations"][operation]

    def submit_change(
        self, change: Mapping[str, Any], *, max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        payload, payload_body = _copy_payload(change, "change")
        return self._enqueue(
            "change", payload, payload_body, payload["event_id"],
            payload["event_id"], max_attempts,
        )

    def submit_removal(
        self, removal: Mapping[str, Any], *, max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        payload, payload_body = _copy_payload(removal, "removal")
        if payload["schema"] == _CMS.CANCELLATION_SCHEMA:
            operation, identity = "cancellation", "cancellation_id"
        else:
            operation, identity = "tombstone", "tombstone_id"
        return self._enqueue(
            operation, payload, payload_body, payload[identity],
            payload["event_id"], max_attempts,
        )

    def _enqueue(
        self, operation, payload, payload_body, request_id, event_id,
        max_attempts,
    ) -> Mapping[str, Any]:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 20
        ):
            _fail("request_invalid")
        contract_name = "change" if operation == "change" else "removal"
        contract = self._contract(contract_name)
        envelope_key = "change" if operation == "change" else "removal"
        request = {
            "schema": contract["request_schema"],
            envelope_key: payload,
            "max_attempts": max_attempts,
        }
        body = _canonical(request)
        _result, response = self._request(
            contract["method"], contract["path"], body,
            {contract["success_status"]},
            {"event_id": event_id, "request_id": request_id},
            {
                "Idempotency-Key": request_id,
                "X-Localization-Source-Payload-Sha256": (
                    hashlib.sha256(body).hexdigest()
                ),
            },
        )
        expected_fields = set(contract["response_fields"])
        payload_sha256 = hashlib.sha256(payload_body).hexdigest()
        if (
            set(response) != expected_fields
            or response.get("schema") != contract["response_schema"]
            or response.get("operation") != operation
            or response.get("request_id") != request_id
            or response.get("event_id") != event_id
            or response.get("payload_sha256") != payload_sha256
            or response.get("status") not in _HTTP.STATUSES
            or not _count(response.get("attempts"), maximum=20)
            or response.get("max_attempts") != max_attempts
            or response.get("attempts") > max_attempts
            or response.get("capabilities_sha256")
            != self.expected_capabilities_sha256
        ):
            _fail("enqueue_binding")
        return response

    def status(self, event_id: str, site_id: str) -> Mapping[str, Any]:
        if not _token(event_id) or not _token(site_id):
            _fail("request_invalid")
        contract = self._contract("status")
        request = {
            "schema": contract["request_schema"],
            "event_id": event_id,
            "site_id": site_id,
        }
        body = _canonical(request)
        _result, response = self._request(
            contract["method"], contract["path"], body,
            {contract["success_status"]},
            {"event_id": event_id, "site_id": site_id},
        )
        if (
            set(response) != set(contract["response_fields"])
            or response.get("schema") != contract["response_schema"]
            or response.get("capabilities_sha256")
            != self.expected_capabilities_sha256
        ):
            _fail("status_binding")
        try:
            normalized = _HTTP._source_status_payload(
                _PayloadView(response["status"]),
                expected_event_id=event_id,
                expected_site_id=site_id,
            )
        except Exception:
            _fail("status_binding")
        if normalized != response["status"]:
            _fail("status_binding")
        return response

    def health(self) -> Mapping[str, Any]:
        contract = self._contract("health")
        result, response = self._request(
            contract["method"], contract["path"], None,
            {200, 503}, {},
        )
        if (
            set(response) != set(contract["response_fields"])
            or response.get("schema") != contract["response_schema"]
            or response.get("capabilities_sha256")
            != self.expected_capabilities_sha256
        ):
            _fail("health_binding")
        try:
            normalized = _HTTP._health_payload(_PayloadView(response["health"]))
        except Exception:
            _fail("health_binding")
        if (
            normalized != response["health"]
            or (result.status == 503) != (normalized["status"] == "blocked")
        ):
            _fail("health_binding")
        return response

    def readiness(self) -> Mapping[str, Any]:
        contract = self._contract("readiness")
        result, response = self._request(
            contract["method"], contract["path"], None,
            {200, 503}, {},
        )
        if (
            set(response) != set(contract["response_fields"])
            or response.get("schema") != contract["response_schema"]
            or response.get("capabilities_sha256")
            != self.expected_capabilities_sha256
        ):
            _fail("readiness_binding")
        try:
            normalized = _HTTP._readiness_payload(response["readiness"])
        except Exception:
            _fail("readiness_binding")
        if (
            normalized != response["readiness"]
            or (result.status == 200) != (normalized["status"] == "ready")
        ):
            _fail("readiness_binding")
        return response
