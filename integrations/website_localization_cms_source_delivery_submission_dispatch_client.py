#!/usr/bin/env python3
"""Contract-pinned HTTPS client for the durable CMS submission sidecar.

The adapter lets non-Python CMS backends enqueue and inspect caller-owned
submission records without sharing a process or database.  It discovers the
exact sidecar contract before every operation, performs one transport attempt
per request, and binds every content-free response to both the sidecar and the
downstream public-submission capability generations.  Retry scheduling remains
the caller's responsibility.
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
MAX_AUTHENTICATION_HEADERS = 58
MAX_RESPONSE_BYTES = 4_000_000
SHA256 = re.compile(r"^[a-f0-9]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
AUTH_CONTEXT_SCHEMA = "blun.cms-public-submission-dispatch-client-auth-context.v2"
RESERVED_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host",
    "idempotency-key", "transfer-encoding",
    "x-localization-source-payload-sha256",
    "x-localization-capabilities-sha256",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load submission sidecar client dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_HTTP = _load_module(
    "blun_website_localization_submission_dispatch_client_http",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_submission_dispatch_http.py",
)


class CMSSourceDeliverySubmissionDispatchClientBlocked(RuntimeError):
    """Stable content-free client failure with an explicit retry decision."""

    cms_source_delivery_submission_dispatch_client_failure = True

    def __init__(
        self, code: str, *, retryable: bool,
        http_status: int | None = None, remote_error_code: str | None = None,
    ):
        remote_valid = (
            remote_error_code is None
            or isinstance(remote_error_code, str)
            and ERROR_CODE.fullmatch(remote_error_code) is not None
        )
        status_valid = (
            http_status is None
            or isinstance(http_status, int) and not isinstance(http_status, bool)
            and 400 <= http_status <= 599
        )
        if (
            ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool)
            or not remote_valid or not status_valid
            or (remote_error_code is None) != (http_status is None)
        ):
            raise ValueError("submission sidecar client failure is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.remote_error_code = remote_error_code


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self, method: str, url: str, headers: Mapping[str, str],
        body: bytes | None, *, timeout: float,
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
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), raw,
            )
        except (TimeoutError, socket.timeout, OSError):
            _fail("network", retryable=True)
        finally:
            response.close()


def _fail(
    code: str, *, retryable: bool = False,
    http_status: int | None = None, remote_error_code: str | None = None,
) -> None:
    raise CMSSourceDeliverySubmissionDispatchClientBlocked(
        "source_delivery_submission_dispatch_client." + code,
        retryable=retryable,
        http_status=http_status,
        remote_error_code=remote_error_code,
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


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _token(value: Any) -> bool:
    return (
        isinstance(value, str) and TOKEN.fullmatch(value) is not None
        and unicodedata.is_normalized("NFC", value)
    )


def _count(value: Any, *, minimum: int = 0, maximum: int = 20) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _timestamp(value: Any, *, nullable: bool = False) -> bool:
    return (
        nullable and value is None
        or isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)) and float(value) >= 0
    )


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str) or value != value.strip() or not value
        or not value.isascii() or len(value) > MAX_ENDPOINT_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("origin is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("origin is invalid") from None
    secure = parsed.scheme == "https"
    loopback = (
        parsed.scheme == "http" and allow_loopback_http
        and isinstance(hostname, str)
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        not (secure or loopback) or not hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path or parsed.query or parsed.fragment
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
    if not items or len(items) > MAX_AUTHENTICATION_HEADERS:
        _fail("authentication")
    result: dict[str, str] = {}
    names: set[str] = set()
    for name, value in items:
        normalized = name.lower() if isinstance(name, str) else ""
        if (
            not isinstance(name, str) or _HTTP.HEADER_NAME.fullmatch(name) is None
            or normalized in RESERVED_HEADERS or normalized in names
            or not isinstance(value, str) or not value
            or len(value) > MAX_HEADER_VALUE_LENGTH
            or "\r" in value or "\n" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            _fail("authentication")
        names.add(normalized)
        result[name] = value
    return result


def _remote_error(
    value: Any, status: int, error_codes: Mapping[str, Any],
) -> None:
    allowed = error_codes.get(str(status))
    code = value.get("error_code") if isinstance(value, Mapping) else None
    if (
        not isinstance(allowed, list) or not allowed
        or not all(isinstance(item, str) for item in allowed)
        or not isinstance(value, Mapping)
        or set(value) != {"schema", "status", "error_code"}
        or value.get("schema") != _HTTP.ERROR_SCHEMA
        or value.get("status") != "BLOCK"
        or not isinstance(code, str) or code not in allowed
    ):
        _fail("error_response", retryable=status >= 500)
    _fail(
        "http_status", retryable=status in {408, 425, 429} or status >= 500,
        http_status=status, remote_error_code=code,
    )


def _json_response(
    result: Any, allowed_statuses: set[int], error_codes: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(result, HTTPResult) or isinstance(result.status, bool)
        or not isinstance(result.status, int) or not 100 <= result.status <= 599
    ):
        _fail("transport_invalid", retryable=True)
    if 300 <= result.status <= 399:
        _fail("redirect")
    known_error = (
        result.status not in allowed_statuses
        and str(result.status) in error_codes
    )
    if result.status not in allowed_statuses and not known_error:
        _fail(
            "http_status",
            retryable=result.status in {408, 425, 429} or result.status >= 500,
        )
    if not isinstance(result.headers, tuple):
        _fail("transport_invalid", retryable=True)
    headers: dict[str, str] = {}
    for item in result.headers:
        if (
            not isinstance(item, tuple) or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            _fail("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in headers:
            _fail("response_headers")
        headers[name] = content
    if headers.get("content-type", "").lower().replace(" ", "") not in {
        "application/json", "application/json;charset=utf-8",
    }:
        _fail("response_content_type")
    if (
        not isinstance(result.body, bytes) or not result.body
        or len(result.body) > MAX_RESPONSE_BYTES
    ):
        _fail("response_size")
    declared = headers.get("content-length")
    if declared is not None and (
        not declared.isascii() or not declared.isdecimal()
        or int(declared) != len(result.body)
    ):
        _fail("response_size")
    try:
        text = result.body.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _fail("response_json")
    if not isinstance(value, dict):
        _fail("response_binding")
    if known_error:
        _remote_error(value, result.status, error_codes)
    return value


def _status(value: Any, identity: Mapping[str, str]) -> dict[str, Any]:
    fields = {
        "operation", "request_id", "event_id", "site_id", "payload_sha256",
        "source_max_attempts", "delivery_max_attempts", "status", "attempts",
        "client_max_attempts", "next_attempt_at", "lease_expires_at",
        "lease_expired", "last_error_code", "remote_status", "remote_attempts",
        "remote_capabilities_sha256", "remote_binding_sha256", "response_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("status_binding")
    payload = dict(value)
    valid = (
        payload["operation"] in _HTTP.OPERATIONS
        and all(payload.get(name) == wanted for name, wanted in identity.items())
        and all(_token(payload[name]) for name in ("request_id", "event_id", "site_id"))
        and _sha256(payload["payload_sha256"])
        and all(_count(payload[name], minimum=1) for name in (
            "source_max_attempts", "delivery_max_attempts", "client_max_attempts",
        ))
        and _count(payload["attempts"])
        and payload["attempts"] <= payload["client_max_attempts"]
        and _timestamp(payload["next_attempt_at"])
        and _timestamp(payload["lease_expires_at"], nullable=True)
        and isinstance(payload["lease_expired"], bool)
        and payload["status"] in _HTTP.STATUSES
        and (payload["status"] == "leased") == (payload["lease_expires_at"] is not None)
        and (payload["last_error_code"] is None or (
            isinstance(payload["last_error_code"], str)
            and ERROR_CODE.fullmatch(payload["last_error_code"]) is not None
        ))
        and payload["remote_status"] in {
            None, "pending", "leased", "retry_wait", "succeeded", "failed",
        }
        and (payload["remote_attempts"] is None or _count(payload["remote_attempts"]))
        and all(payload[name] is None or _sha256(payload[name]) for name in (
            "remote_capabilities_sha256", "remote_binding_sha256", "response_sha256",
        ))
    )
    remote = (
        payload["remote_status"], payload["remote_attempts"],
        payload["remote_capabilities_sha256"], payload["remote_binding_sha256"],
        payload["response_sha256"],
    )
    if not valid or (payload["status"] == "accepted") != all(item is not None for item in remote):
        _fail("status_binding")
    return payload


class CMSSourceDeliverySubmissionDispatchHTTPClient:
    """Operate one durable CMS submission sidecar through its exact contract."""

    def __init__(
        self, origin: str, expected_capabilities_sha256: str,
        expected_public_submission_capabilities_sha256: str,
        authentication_headers: Callable[[Mapping[str, Any]], Mapping[str, str]],
        *, transport: HTTPTransport | None = None, timeout: float | int = 30,
        allow_loopback_http: bool = False,
    ):
        if not isinstance(allow_loopback_http, bool):
            raise TypeError("allow_loopback_http must be boolean")
        self.origin = _endpoint(origin, allow_loopback_http)
        if not _sha256(expected_capabilities_sha256) or not _sha256(
            expected_public_submission_capabilities_sha256
        ):
            raise ValueError("expected capability digest is invalid")
        expected = _HTTP._capabilities_payload(
            expected_public_submission_capabilities_sha256
        )
        if expected["sha256"] != expected_capabilities_sha256:
            raise ValueError("sidecar and public capability pins are inconsistent")
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        self.expected_capabilities_sha256 = expected_capabilities_sha256
        self.expected_public_submission_capabilities_sha256 = (
            expected_public_submission_capabilities_sha256
        )
        self._expected_capabilities = expected
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "request", None)):
            raise TypeError("transport must provide request")
        self.timeout = float(timeout)

    def __repr__(self) -> str:
        return "CMSSourceDeliverySubmissionDispatchHTTPClient(configured=True)"

    def _request(
        self, method, path, scope, body, statuses, context,
        error_codes, headers=None,
    ):
        authentication = {
            "schema": AUTH_CONTEXT_SCHEMA, "method": method,
            "origin": self.origin, "path": path, "scope": scope,
            "body_sha256": hashlib.sha256(body or b"").hexdigest(),
            **context,
        }
        if path != _HTTP.CAPABILITIES_PATH:
            authentication["capabilities_sha256"] = (
                self.expected_capabilities_sha256
            )
        request_headers = _authentication_headers(
            self.authentication_headers, authentication,
        )
        request_headers["Accept"] = "application/json"
        if body is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        if path != _HTTP.CAPABILITIES_PATH:
            request_headers[_HTTP.CAPABILITIES_PRECONDITION_HEADER] = (
                self.expected_capabilities_sha256
            )
        if headers:
            request_headers.update(headers)
        try:
            result = self.transport.request(
                method, self.origin + path, request_headers, body,
                timeout=self.timeout,
            )
        except CMSSourceDeliverySubmissionDispatchClientBlocked:
            raise
        except Exception:
            _fail("network", retryable=True)
        return _json_response(result, statuses, error_codes)

    def capabilities(self) -> Mapping[str, Any]:
        contract = self._expected_capabilities["operations"]["capabilities"]
        response = self._request(
            "GET", _HTTP.CAPABILITIES_PATH, _HTTP.SCOPES[_HTTP.CAPABILITIES_PATH],
            None, {200}, {}, contract["error_codes"],
        )
        if (
            set(response) != {"schema", "capabilities"}
            or response.get("schema") != _HTTP.CAPABILITIES_RESPONSE_SCHEMA
            or response.get("capabilities") != self._expected_capabilities
        ):
            _fail("capabilities_binding")
        return response

    def _contract(self, name: str) -> Mapping[str, Any]:
        return self.capabilities()["capabilities"]["operations"][name]

    def enqueue(
        self, payload: Mapping[str, Any], *, source_max_attempts: int = 5,
        delivery_max_attempts: int = 5, client_max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        if not all(_count(value, minimum=1) for value in (
            source_max_attempts, delivery_max_attempts, client_max_attempts,
        )):
            _fail("request_invalid")
        try:
            copied, identity, canonical = _HTTP._DISPATCH._payload(payload)
        except Exception:
            _fail("request_invalid")
        contract = self._contract("enqueue")
        request = {
            "schema": contract["request_schema"], "payload": copied,
            "source_max_attempts": source_max_attempts,
            "delivery_max_attempts": delivery_max_attempts,
            "client_max_attempts": client_max_attempts,
        }
        body = _canonical(request)
        response = self._request(
            contract["method"], contract["path"], contract["scope"], body,
            {contract["success_status"]}, identity,
            contract["error_codes"],
            {
                "Idempotency-Key": identity["request_id"],
                "X-Localization-Source-Payload-Sha256": hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest(),
            },
        )
        if (
            set(response) != {
                "schema", "api_schema", "status", "capabilities_sha256",
                "accepted_implies_publication",
            }
            or response.get("schema") != contract["response_schema"]
            or response.get("api_schema") != _HTTP.API_SCHEMA
            or response.get("capabilities_sha256") != self.expected_capabilities_sha256
            or response.get("accepted_implies_publication") is not False
        ):
            _fail("enqueue_binding")
        status = _status(response.get("status"), identity)
        if (
            status["source_max_attempts"] != source_max_attempts
            or status["delivery_max_attempts"] != delivery_max_attempts
            or status["client_max_attempts"] != client_max_attempts
        ):
            _fail("enqueue_binding")
        return response

    def status(
        self, operation: str, request_id: str, event_id: str, site_id: str,
        payload_sha256: str,
    ) -> Mapping[str, Any]:
        identity = {
            "operation": operation, "request_id": request_id,
            "event_id": event_id, "site_id": site_id,
            "payload_sha256": payload_sha256,
        }
        if (
            operation not in _HTTP.OPERATIONS
            or not all(_token(identity[name]) for name in ("request_id", "event_id", "site_id"))
            or operation == "change" and request_id != event_id
            or not _sha256(payload_sha256)
        ):
            _fail("request_invalid")
        contract = self._contract("status")
        body = _canonical({"schema": contract["request_schema"], **identity})
        response = self._request(
            contract["method"], contract["path"], contract["scope"], body,
            {contract["success_status"]}, identity, contract["error_codes"],
        )
        if (
            set(response) != {
                "schema", "api_schema", "status", "capabilities_sha256",
                "accepted_implies_publication",
            }
            or response.get("schema") != contract["response_schema"]
            or response.get("api_schema") != _HTTP.API_SCHEMA
            or response.get("capabilities_sha256") != self.expected_capabilities_sha256
            or response.get("accepted_implies_publication") is not False
        ):
            _fail("status_binding")
        _status(response.get("status"), identity)
        return response

    def health(self) -> Mapping[str, Any]:
        return self._monitor("health")

    def readiness(self) -> Mapping[str, Any]:
        return self._monitor("readiness")

    def openapi(self) -> Mapping[str, Any]:
        """Return the exact origin-free API document for this pinned deployment."""
        contract = self._contract("openapi")
        response = self._request(
            contract["method"], contract["path"], contract["scope"], None,
            {contract["success_status"]}, {}, contract["error_codes"],
        )
        expected = _HTTP._OPENAPI.build_document(self._expected_capabilities)
        if (
            set(response) != {
                "schema", "openapi", "openapi_sha256", "capabilities_sha256",
            }
            or response.get("schema") != contract["response_schema"]
            or response.get("capabilities_sha256")
            != self.expected_capabilities_sha256
            or response.get("openapi") != expected
            or response.get("openapi_sha256")
            != _HTTP._OPENAPI.document_sha256(expected)
        ):
            _fail("openapi_binding")
        return response

    def _monitor(self, name: str) -> Mapping[str, Any]:
        contract = self._contract(name)
        response = self._request(
            contract["method"], contract["path"], contract["scope"], None,
            {contract["success_status"], 503}, {}, contract["error_codes"],
        )
        if response.get("schema") == _HTTP.ERROR_SCHEMA:
            _remote_error(response, 503, contract["error_codes"])
        key = name
        if (
            set(response) != {"schema", key, "capabilities_sha256"}
            or response.get("schema") != contract["response_schema"]
            or response.get("capabilities_sha256") != self.expected_capabilities_sha256
            or not isinstance(response.get(key), Mapping)
        ):
            _fail(name + "_binding")
        payload = response[key]
        if name == "health":
            valid = (
                set(payload) == {
                    "status", "counts", "operations", "due", "expired_leases",
                    "failed", "expected_capabilities_sha256",
                }
                and payload.get("status") in {"ok", "blocked"}
                and isinstance(payload.get("counts"), Mapping)
                and set(payload["counts"]) == _HTTP.STATUSES
                and isinstance(payload.get("operations"), Mapping)
                and set(payload["operations"]) == _HTTP.OPERATIONS
                and all(_count(value, maximum=1_000_000) for value in payload["counts"].values())
                and all(_count(value, maximum=1_000_000) for value in payload["operations"].values())
                and all(_count(payload[name], maximum=1_000_000) for name in (
                    "due", "expired_leases", "failed",
                ))
                and sum(payload["counts"].values()) == sum(payload["operations"].values())
                and payload["due"] <= payload["counts"]["pending"] + payload["counts"]["retry_wait"]
                and payload["expired_leases"] <= payload["counts"]["leased"]
                and payload["failed"] == payload["counts"]["failed"]
                and (payload["status"] == "ok") == (
                    payload["expired_leases"] == 0 and payload["failed"] == 0
                )
                and payload["expected_capabilities_sha256"]
                == self.expected_public_submission_capabilities_sha256
            )
        else:
            valid = (
                set(payload) == {
                    "schema", "status", "worker_state", "outbox_status",
                    "error_code", "capabilities_sha256",
                }
                and payload.get("schema") == "blun.cms-public-submission-worker-readiness.v1"
                and payload.get("status") in {"ready", "not_ready"}
                and payload.get("worker_state") in _HTTP.WORKER_STATES
                and payload.get("outbox_status") in {None, "ok", "blocked"}
                and (payload.get("error_code") is None or (
                    isinstance(payload["error_code"], str)
                    and ERROR_CODE.fullmatch(payload["error_code"]) is not None
                ))
                and payload.get("capabilities_sha256")
                == self.expected_public_submission_capabilities_sha256
            )
            ready = (
                payload.get("worker_state") == "running"
                and payload.get("outbox_status") == "ok"
                and payload.get("error_code") is None
            )
            valid = valid and ready == (payload.get("status") == "ready")
            valid = valid and (ready or payload.get("error_code") is not None)
        if not valid:
            _fail(name + "_binding")
        return response
