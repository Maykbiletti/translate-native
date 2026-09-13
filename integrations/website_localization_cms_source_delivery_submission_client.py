#!/usr/bin/env python3
"""Contract-pinned client for the public website submission edge.

The client discovers the complete website-to-source contract before every
operation. It performs one bounded transport attempt, never follows redirects,
and validates every content-free response against the exact request identities
and capability generation. Retry scheduling remains the caller's responsibility.
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
MAX_RESPONSE_BYTES = 1_000_000
SHA256 = re.compile(r"^[a-f0-9]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
AUTH_CONTEXT_SCHEMA = (
    "blun.cms-source-delivery-submission-client-auth-context.v1"
)
RESERVED_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host",
    "idempotency-key", "transfer-encoding",
    "x-localization-source-payload-sha256",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load submission client dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_HTTP = _load_module(
    "blun_website_localization_submission_client_http",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_http.py",
)
_CMS = _load_module(
    "blun_website_localization_submission_client_cms",
    _ROOT / "integrations" / "website_localization_cms.py",
)


class CMSSourceDeliverySubmissionClientBlocked(RuntimeError):
    """Stable content-free failure with an explicit retry decision."""

    cms_source_delivery_submission_client_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool):
            raise ValueError("submission client failure is invalid")
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
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), raw,
            )
        except (TimeoutError, socket.timeout, OSError):
            _fail("network", retryable=True)
        finally:
            response.close()


def _fail(code: str, *, retryable: bool = False) -> None:
    raise CMSSourceDeliverySubmissionClientBlocked(
        "source_delivery_submission_client." + code, retryable=retryable,
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


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _count(value: Any, *, minimum: int = 0, maximum: int = 20) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
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
        or len(result.body) > MAX_RESPONSE_BYTES
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


def _copy_payload(value: Any, *, change: bool) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, Mapping):
        _fail("request_invalid")
    raw = _canonical(dict(value))
    try:
        copied = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if change:
            _CMS.WebsiteLocalizationCMSBridge._validated_event(None, copied)
        elif copied.get("schema") == _CMS.CANCELLATION_SCHEMA:
            _CMS.WebsiteLocalizationCMSBridge._validated_cancellation(None, copied)
        elif copied.get("schema") == _CMS.TOMBSTONE_SCHEMA:
            _CMS.WebsiteLocalizationCMSBridge._validated_tombstone(None, copied)
        else:
            raise ValueError
    except Exception:
        _fail("request_invalid")
    return copied, raw


def _identity(payload: Mapping[str, Any], *, change: bool) -> dict[str, str]:
    try:
        operation, request_id, event_id, site_id = _HTTP._identity(
            payload, change=change,
        )
    except Exception:
        _fail("request_invalid")
    return {
        "operation": operation,
        "request_id": request_id,
        "event_id": event_id,
        "site_id": site_id,
        "payload_sha256": hashlib.sha256(_canonical(dict(payload))).hexdigest(),
    }


class _PayloadView:
    def __init__(self, value: Mapping[str, Any]):
        self.value = value

    def as_payload(self) -> Mapping[str, Any]:
        return self.value


class CMSSourceDeliverySubmissionHTTPClient:
    """Operate one public website edge through its freshly pinned contract."""

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
        if not _sha256(expected_capabilities_sha256):
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
        return "CMSSourceDeliverySubmissionHTTPClient(configured=True)"

    def _request(
        self, method, path, scope, body, allowed_statuses, context, headers=None,
    ):
        authentication = {
            "schema": AUTH_CONTEXT_SCHEMA,
            "method": method,
            "origin": self.origin,
            "path": path,
            "scope": scope,
            "body_sha256": hashlib.sha256(body or b"").hexdigest(),
            **context,
        }
        request_headers = _authentication_headers(
            self.authentication_headers, authentication,
        )
        request_headers["Accept"] = "application/json"
        if body is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            request_headers.update(headers)
        try:
            result = self.transport.request(
                method, self.origin + path, request_headers, body,
                timeout=self.timeout,
            )
        except CMSSourceDeliverySubmissionClientBlocked:
            raise
        except Exception:
            _fail("network", retryable=True)
        return result, _json_response(result, allowed_statuses)

    def capabilities(self) -> Mapping[str, Any]:
        _result, response = self._request(
            "GET",
            _HTTP.CAPABILITIES_PATH,
            _HTTP._SUBMISSION.CAPABILITIES_HTTP_SCOPE,
            None,
            {200},
            {},
        )
        if (
            set(response) != {"schema", "api_schema", "capabilities"}
            or response.get("schema")
            != _HTTP._SUBMISSION.CAPABILITIES_HTTP_RESPONSE_SCHEMA
            or response.get("api_schema") != _HTTP._CAPABILITIES.API_SCHEMA
            or not isinstance(response.get("capabilities"), Mapping)
        ):
            _fail("capabilities_binding")
        try:
            normalized = _HTTP._CAPABILITIES._capabilities(
                _PayloadView(response["capabilities"])
            )
        except Exception:
            _fail("capabilities_binding")
        if (
            normalized != response["capabilities"]
            or normalized.get("sha256") != self.expected_capabilities_sha256
        ):
            _fail("capabilities_binding")
        return response

    def _contract(self, name: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        capabilities = self.capabilities()["capabilities"]
        return capabilities["operations"][name], capabilities

    def submit_change(
        self,
        change: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        payload, _raw = _copy_payload(change, change=True)
        return self._submit(
            payload, change=True, source_max_attempts=source_max_attempts,
            delivery_max_attempts=delivery_max_attempts,
        )

    def submit_removal(
        self,
        removal: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        payload, _raw = _copy_payload(removal, change=False)
        return self._submit(
            payload, change=False, source_max_attempts=source_max_attempts,
            delivery_max_attempts=delivery_max_attempts,
        )

    def _submit(
        self, payload, *, change, source_max_attempts, delivery_max_attempts,
    ):
        if not _count(source_max_attempts, minimum=1):
            _fail("request_invalid")
        if not _count(delivery_max_attempts, minimum=1):
            _fail("request_invalid")
        identity = _identity(payload, change=change)
        name = "enqueue_change" if change else "enqueue_removal"
        contract, capabilities = self._contract(name)
        key = "change" if change else "removal"
        request = {
            "schema": contract["request_schema"],
            key: payload,
            "source_max_attempts": source_max_attempts,
            "delivery_max_attempts": delivery_max_attempts,
        }
        body = _canonical(request)
        _result, response = self._request(
            contract["method"], contract["path"], contract["scope"], body,
            {202}, identity,
            {
                "Idempotency-Key": identity["request_id"],
                "X-Localization-Source-Payload-Sha256": (
                    identity["payload_sha256"]
                ),
            },
        )
        expected_fields = {
            "schema", "api_schema", "operation", "request_id", "event_id",
            "site_id", "payload_sha256", "status", "attempts",
            "delivery_max_attempts", "source_max_attempts",
            "capabilities_sha256", "website_capability_binding",
            "accepted_implies_publication",
        }
        binding = capabilities["website_capability_binding"]
        valid = (
            set(response) == expected_fields
            and response.get("schema") == contract["response_schema"]
            and response.get("api_schema") == _HTTP.API_SCHEMA
            and all(response.get(name) == value for name, value in identity.items())
            and response.get("status") in _HTTP.STATUSES
            and _count(response.get("attempts"))
            and response["attempts"] <= delivery_max_attempts
            and response.get("delivery_max_attempts") == delivery_max_attempts
            and response.get("source_max_attempts") == source_max_attempts
            and response.get("capabilities_sha256")
            == binding["delivery_capabilities_sha256"]
            and response.get("website_capability_binding") == binding
            and response.get("accepted_implies_publication") is False
        )
        if not valid:
            _fail("submission_binding")
        return response

    @staticmethod
    def _read_identity(
        operation, request_id, event_id, site_id, payload_sha256,
    ) -> dict[str, str]:
        identity = {
            "operation": operation,
            "request_id": request_id,
            "event_id": event_id,
            "site_id": site_id,
            "payload_sha256": payload_sha256,
        }
        if (
            operation not in {"change", "cancellation", "tombstone"}
            or not all(_token(identity[name]) for name in (
                "request_id", "event_id", "site_id",
            ))
            or operation == "change" and request_id != event_id
            or not _sha256(payload_sha256)
        ):
            _fail("request_invalid")
        return identity

    def submission_status(
        self, operation, request_id, event_id, site_id, payload_sha256,
    ) -> Mapping[str, Any]:
        return self._read(
            "submission_status", operation, request_id, event_id, site_id,
            payload_sha256,
        )

    def submission_lifecycle(
        self, operation, request_id, event_id, site_id, payload_sha256,
    ) -> Mapping[str, Any]:
        return self._read(
            "submission_lifecycle", operation, request_id, event_id, site_id,
            payload_sha256,
        )

    def _read(
        self, name, operation, request_id, event_id, site_id, payload_sha256,
    ):
        identity = self._read_identity(
            operation, request_id, event_id, site_id, payload_sha256,
        )
        contract, capabilities = self._contract(name)
        request = {"schema": contract["request_schema"], **identity}
        body = _canonical(request)
        _result, response = self._request(
            contract["method"], contract["path"], contract["scope"], body,
            {200}, identity,
        )
        if (
            set(response)
            != {"schema", "api_schema", "result", "accepted_implies_publication"}
            or response.get("schema") != contract["response_schema"]
            or response.get("api_schema") != _HTTP.API_SCHEMA
            or response.get("accepted_implies_publication") is not False
            or not isinstance(response.get("result"), Mapping)
        ):
            _fail("status_binding" if name == "submission_status" else "lifecycle_binding")
        try:
            if name == "submission_status":
                normalized = _HTTP._submission_status_payload(
                    _PayloadView(response["result"]), identity,
                )
            else:
                normalized = _HTTP._submission_lifecycle_payload(
                    _PayloadView(response["result"]), identity,
                )
        except Exception:
            _fail("status_binding" if name == "submission_status" else "lifecycle_binding")
        binding = capabilities["website_capability_binding"]
        if normalized != response["result"] or (
            normalized.get("website_capability_binding") != binding
        ):
            _fail("status_binding" if name == "submission_status" else "lifecycle_binding")
        if name == "submission_lifecycle" and (
            normalized["sidecar_capabilities_sha256"]
            != capabilities["sidecar_capabilities_sha256"]
            or normalized["source_capabilities_sha256"]
            != capabilities["source_capabilities_sha256"]
        ):
            _fail("lifecycle_binding")
        return response

    def pipeline_health(self) -> Mapping[str, Any]:
        return self._monitor("submission_pipeline_health")

    def pipeline_readiness(self) -> Mapping[str, Any]:
        return self._monitor("submission_pipeline_readiness")

    def _monitor(self, name: str) -> Mapping[str, Any]:
        contract, capabilities = self._contract(name)
        _result, response = self._request(
            contract["method"], contract["path"], contract["scope"], None,
            {200}, {},
        )
        code = "health_binding" if name.endswith("health") else "readiness_binding"
        if (
            set(response) != {
                "schema", "api_schema", "result", "content_free",
                "publication_authority",
            }
            or response.get("schema") != contract["response_schema"]
            or response.get("api_schema") != _HTTP.API_SCHEMA
            or response.get("content_free") is not True
            or response.get("publication_authority") is not False
            or not isinstance(response.get("result"), Mapping)
        ):
            _fail(code)
        try:
            validator = (
                _HTTP._pipeline_health_payload
                if name.endswith("health")
                else _HTTP._pipeline_readiness_payload
            )
            normalized = validator(_PayloadView(response["result"]))
        except Exception:
            _fail(code)
        if (
            normalized != response["result"]
            or normalized["website_capability_binding"]
            != capabilities["website_capability_binding"]
            or normalized["sidecar_capabilities_sha256"]
            != capabilities["sidecar_capabilities_sha256"]
            or normalized["source_capabilities_sha256"]
            != capabilities["source_capabilities_sha256"]
        ):
            _fail(code)
        return response
