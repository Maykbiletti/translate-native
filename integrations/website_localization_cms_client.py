#!/usr/bin/env python3
"""Secure source-side HTTPS client for the public CMS localization API.

The caller supplies already-versioned change, cancellation, and tombstone
objects. This adapter signs their canonical UTF-8 bytes, performs exactly one
HTTP attempt against a fixed origin, and binds every response to the operation
and request that produced it. Retry scheduling remains a host responsibility.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


MAX_ORIGIN_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 4096
MAX_RESPONSE_BYTES = 4_000_000
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SIGNATURE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+=-]{1,4096}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
RESERVED_HEADERS = {
    "accept", "accept-encoding", "connection", "content-length",
    "content-type", "host", "transfer-encoding",
    "x-localization-key-id", "x-localization-signature",
    "x-localization-signature-algorithm",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required CMS client dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_API = _load_module(
    "blun_website_localization_cms_client_api",
    _ROOT / "integrations" / "website_localization_api.py",
)
_CMS = _load_module(
    "blun_website_localization_cms_client_cms",
    _ROOT / "integrations" / "website_localization_cms.py",
)


class CMSClientFailed(RuntimeError):
    """Stable, content-free client failure with an explicit retry decision."""

    cms_client_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("CMS client error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("CMS client retryability must be boolean")
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
    """Perform one bounded stdlib POST while refusing redirects."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    @staticmethod
    def _read(response: Any) -> bytes:
        try:
            return response.read(MAX_RESPONSE_BYTES + 1)
        except (TimeoutError, socket.timeout, OSError):
            raise CMSClientFailed("network", retryable=True) from None

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
            try:
                return HTTPResult(
                    int(error.code),
                    tuple(error.headers.items()) if error.headers is not None else (),
                    self._read(error),
                )
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise CMSClientFailed("network", retryable=True) from None
        try:
            return HTTPResult(
                int(response.status), tuple(response.headers.items()),
                self._read(response),
            )
        finally:
            response.close()


def _canonical_json(value: Any, *, code: str) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CMSClientFailed(code, retryable=False) from None
    if not encoded or len(encoded) > _API.MAX_MESSAGE_BYTES:
        raise CMSClientFailed(code, retryable=False)
    return encoded


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _token(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise CMSClientFailed(code, retryable=False)
    return value


def _valid_token(value: Any) -> bool:
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _valid_sha256(value: Any, *, optional: bool = False) -> bool:
    return (optional and value is None) or (
        isinstance(value, str) and SHA256.fullmatch(value) is not None
    )


def _capability_pin(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _valid_nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and value >= 0
    )


def _timestamp(value: Any, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CMSClientFailed(code, retryable=False)
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise CMSClientFailed(code, retryable=False)
    return result


def _origin(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or not value.isascii()
        or len(value) > MAX_ORIGIN_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("origin is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("origin is invalid") from None
    if (
        not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("origin must contain only scheme and authority")
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and allow_loopback_http
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        pass
    else:
        raise ValueError("origin must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("origin is invalid")
    return value.rstrip("/")


def _authentication_headers(
    provider: Callable[[], Mapping[str, str]],
) -> dict[str, str]:
    try:
        supplied = provider()
    except Exception:
        raise CMSClientFailed("authentication", retryable=False) from None
    if not isinstance(supplied, Mapping):
        raise CMSClientFailed("authentication", retryable=False)
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
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise CMSClientFailed("authentication", retryable=False)
        normalized_names.add(normalized)
        result[name] = value
    return result


def _signature(value: Any) -> tuple[str, str, str]:
    try:
        algorithm = value.algorithm
        key_id = value.key_id
        signature = value.signature
    except Exception:
        raise CMSClientFailed("signing", retryable=False) from None
    if (
        not isinstance(algorithm, str)
        or TOKEN.fullmatch(algorithm) is None
        or not isinstance(key_id, str)
        or TOKEN.fullmatch(key_id) is None
        or not isinstance(signature, str)
        or SIGNATURE_VALUE.fullmatch(signature) is None
    ):
        raise CMSClientFailed("signing", retryable=False)
    return algorithm, key_id, signature


def _response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise CMSClientFailed("transport_invalid", retryable=True)
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise CMSClientFailed("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                raise CMSClientFailed("response_headers", retryable=True)
            result[name] = content
    return result


def _copy_request(value: Any, *, schema: str, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CMSClientFailed(code, retryable=False)
    raw = _canonical_json(dict(value), code=code)
    try:
        copied = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise CMSClientFailed(code, retryable=False) from None
    if not isinstance(copied, dict) or copied.get("schema") != schema:
        raise CMSClientFailed(code, retryable=False)
    return copied


class CMSLocalizationHTTPClient:
    """Call every public CMS API operation through one exact signed boundary."""

    _OPERATIONS = {
        "change": (_API.CHANGE_PATH, _CMS.CHANGE_SCHEMA),
        "cancellation": (_API.CANCELLATION_PATH, _CMS.CANCELLATION_SCHEMA),
        "tombstone": (_API.TOMBSTONE_PATH, _CMS.TOMBSTONE_SCHEMA),
        "status": (_API.STATUS_PATH, _API.STATUS_REQUEST_SCHEMA),
        "lifecycle": (_API.LIFECYCLE_PATH, _API.LIFECYCLE_REQUEST_SCHEMA),
        "capabilities": (
            _API.CAPABILITIES_PATH, _API.CAPABILITIES_REQUEST_SCHEMA,
        ),
    }

    def __init__(
        self,
        origin: str,
        authentication_headers: Callable[[], Mapping[str, str]],
        signing_authority: Any,
        *,
        transport: HTTPTransport | None = None,
        timeout: float = 30.0,
        allow_loopback_http: bool = False,
        clock: Callable[[], float | int] = time.time,
        request_id_factory: Callable[[], str] | None = None,
        capabilities_sha256: str | None = None,
        commercial_rendering_registry_sha256: str | None = None,
    ):
        if not isinstance(allow_loopback_http, bool):
            raise TypeError("allow_loopback_http must be boolean")
        self.origin = _origin(origin, allow_loopback_http)
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if not callable(getattr(signing_authority, "sign", None)):
            raise TypeError("signing_authority must provide sign")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        self.authentication_headers = authentication_headers
        self.signing_authority = signing_authority
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.timeout = float(timeout)
        self.clock = clock
        self._capabilities_sha256 = _capability_pin(
            capabilities_sha256,
            "capabilities_sha256",
        )
        self._commercial_rendering_registry_sha256 = _capability_pin(
            commercial_rendering_registry_sha256,
            "commercial_rendering_registry_sha256",
        )
        self.request_id_factory = (
            (lambda: secrets.token_hex(16))
            if request_id_factory is None else request_id_factory
        )
        if not callable(self.request_id_factory):
            raise TypeError("request_id_factory must be callable")

    @property
    def capabilities_sha256(self) -> str | None:
        """Return the constructor-fixed complete capability deployment pin."""
        return self._capabilities_sha256

    @property
    def commercial_rendering_registry_sha256(self) -> str | None:
        """Return the constructor-fixed commercial rendering registry pin."""
        return self._commercial_rendering_registry_sha256

    def submit_change(self, change: Mapping[str, Any]) -> Mapping[str, Any]:
        request = _copy_request(
            change, schema=_CMS.CHANGE_SCHEMA, code="change_invalid",
        )
        response = self._post("change", request, {200, 202})
        expected = {
            "schema", "event_id", "plan_id", "job_count", "inserted_jobs",
            "status",
        }
        if (
            set(response) != expected
            or response.get("schema") != _API.API_SCHEMA
            or response.get("event_id") != request.get("event_id")
            or not _valid_token(response.get("plan_id"))
            or isinstance(response.get("job_count"), bool)
            or not isinstance(response.get("job_count"), int)
            or response["job_count"] <= 0
            or isinstance(response.get("inserted_jobs"), bool)
            or not isinstance(response.get("inserted_jobs"), int)
            or not 0 <= response["inserted_jobs"] <= response["job_count"]
            or response.get("status") not in {"enqueued", "superseded", "cancelled"}
        ):
            raise CMSClientFailed("response_binding", retryable=True)
        return response

    def cancel(self, cancellation: Mapping[str, Any]) -> Mapping[str, Any]:
        request = _copy_request(
            cancellation,
            schema=_CMS.CANCELLATION_SCHEMA,
            code="cancellation_invalid",
        )
        response = self._post("cancellation", request, {200, 202})
        if (
            set(response) != {
                "schema", "cancellation_id", "event_id", "status",
                "newly_cancelled",
            }
            or response.get("schema") != _API.API_SCHEMA
            or response.get("cancellation_id") != request.get("cancellation_id")
            or response.get("event_id") != request.get("event_id")
            or response.get("status") != "cancelled"
            or not isinstance(response.get("newly_cancelled"), bool)
        ):
            raise CMSClientFailed("response_binding", retryable=True)
        return response

    def request_tombstone(self, tombstone: Mapping[str, Any]) -> Mapping[str, Any]:
        request = _copy_request(
            tombstone, schema=_CMS.TOMBSTONE_SCHEMA, code="tombstone_invalid",
        )
        response = self._post("tombstone", request, {200, 202})
        if (
            set(response) != {
                "schema", "tombstone_id", "event_id", "delivery_id",
                "status", "newly_requested",
            }
            or response.get("schema") != _API.API_SCHEMA
            or response.get("tombstone_id") != request.get("tombstone_id")
            or response.get("event_id") != request.get("event_id")
            or not _valid_token(response.get("delivery_id"))
            or response.get("status") not in {
                "pending", "leased", "retry_wait", "succeeded", "failed",
            }
            or not isinstance(response.get("newly_requested"), bool)
        ):
            raise CMSClientFailed("response_binding", retryable=True)
        return response

    def status(
        self, event_id: str, site_id: str, *, request_id: str | None = None,
    ) -> Mapping[str, Any]:
        request = self._read_request(
            _API.STATUS_REQUEST_SCHEMA, event_id, site_id, request_id,
        )
        response = self._post("status", request, {200})
        expected = {
            "schema", "status", "request_id", "event_id", "site_id",
            "plan_id", "website_version", "source_sequence", "job_count",
            "counts", "locales", "cancelled", "queue_recovery_pending",
        }
        if (
            set(response) != expected
            or response.get("schema") != _API.API_SCHEMA
            or response.get("status") != "PROGRESS"
            or response.get("request_id") != request["request_id"]
            or response.get("event_id") != event_id
            or response.get("site_id") != site_id
            or not self._valid_progress(response)
        ):
            raise CMSClientFailed("response_binding", retryable=True)
        return response

    def lifecycle(
        self, event_id: str, site_id: str, *, request_id: str | None = None,
    ) -> Mapping[str, Any]:
        request = self._read_request(
            _API.LIFECYCLE_REQUEST_SCHEMA, event_id, site_id, request_id,
        )
        response = self._post("lifecycle", request, {200})
        expected = {
            "schema", "request_id", "event_id", "site_id", "plan_id",
            "website_version", "source_sequence", "status",
            "required_locales", "approved_locales", "blocked_locales",
            "queue_counts", "delivery", "tombstone",
        }
        if (
            set(response) != expected
            or response.get("schema") != _API.LIFECYCLE_RESPONSE_SCHEMA
            or response.get("request_id") != request["request_id"]
            or response.get("event_id") != event_id
            or response.get("site_id") != site_id
            or not self._valid_lifecycle(response)
        ):
            raise CMSClientFailed("response_binding", retryable=True)
        return response

    def capabilities(self, *, request_id: str | None = None) -> Mapping[str, Any]:
        request = {
            "schema": _API.CAPABILITIES_REQUEST_SCHEMA,
            "request_id": self._request_id(request_id),
            "requested_at": _timestamp(self.clock(), code="clock_invalid"),
        }
        response = self._post("capabilities", request, {200})
        if (
            set(response) != {
                "schema", "status", "request_id", "capabilities", "api_contract",
            }
            or response.get("schema") != _API.API_SCHEMA
            or response.get("status") != "CAPABILITIES"
            or response.get("request_id") != request["request_id"]
            or not self._valid_capabilities(response)
        ):
            raise CMSClientFailed("response_binding", retryable=True)
        capabilities = response["capabilities"]
        if (
            self.capabilities_sha256 is not None
            and capabilities["sha256"] != self.capabilities_sha256
        ):
            raise CMSClientFailed(
                "capabilities_pin_mismatch",
                retryable=False,
            )
        registry = capabilities["commercial_rendering_registry"]
        if (
            self.commercial_rendering_registry_sha256 is not None
            and registry["sha256"]
            != self.commercial_rendering_registry_sha256
        ):
            raise CMSClientFailed(
                "commercial_rendering_registry_pin_mismatch",
                retryable=False,
            )
        return response

    def _read_request(
        self,
        schema: str,
        event_id: Any,
        site_id: Any,
        request_id: str | None,
    ) -> dict[str, Any]:
        return {
            "schema": schema,
            "request_id": self._request_id(request_id),
            "event_id": _token(event_id, code="request_invalid"),
            "site_id": _token(site_id, code="request_invalid"),
            "requested_at": _timestamp(self.clock(), code="clock_invalid"),
        }

    def _request_id(self, supplied: str | None) -> str:
        if supplied is None:
            try:
                supplied = self.request_id_factory()
            except Exception:
                raise CMSClientFailed("request_id_invalid", retryable=False) from None
        return _token(supplied, code="request_id_invalid")

    def _post(
        self, operation: str, request: Mapping[str, Any], success: set[int],
    ) -> dict[str, Any]:
        path, schema = self._OPERATIONS[operation]
        if request.get("schema") != schema:
            raise CMSClientFailed("request_invalid", retryable=False)
        body = _canonical_json(dict(request), code="request_invalid")
        try:
            raw_signature = self.signing_authority.sign(body)
        except Exception:
            raise CMSClientFailed("signing", retryable=False) from None
        algorithm, key_id, signature = _signature(raw_signature)
        headers = _authentication_headers(self.authentication_headers)
        headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            "X-Localization-Signature-Algorithm": algorithm,
            "X-Localization-Key-Id": key_id,
            "X-Localization-Signature": signature,
        })
        try:
            result = self.transport.post(
                self.origin + path, headers, body, timeout=self.timeout,
            )
        except CMSClientFailed:
            raise
        except Exception:
            raise CMSClientFailed("network", retryable=True) from None
        return self._parse_response(result, success)

    @staticmethod
    def _parse_response(result: Any, success: set[int]) -> dict[str, Any]:
        try:
            status = result.status
            raw_headers = result.headers
            body = result.body
        except Exception:
            raise CMSClientFailed("transport_invalid", retryable=True) from None
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            raise CMSClientFailed("transport_invalid", retryable=True)
        if 300 <= status <= 399:
            raise CMSClientFailed("redirect", retryable=False)
        headers = _response_headers(raw_headers)
        content_type = headers.get("content-type", "").lower().replace(" ", "")
        if content_type not in {"application/json", "application/json;charset=utf-8"}:
            raise CMSClientFailed("response_content_type", retryable=True)
        if (
            not isinstance(body, bytes)
            or not body
            or len(body) > MAX_RESPONSE_BYTES
        ):
            raise CMSClientFailed("response_size", retryable=True)
        declared = headers.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdecimal()
            or int(declared) != len(body)
        ):
            raise CMSClientFailed("response_size", retryable=True)
        try:
            text = body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            value = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise CMSClientFailed("response_json", retryable=True) from None
        if (
            not isinstance(value, dict)
            or _canonical_json(value, code="response_json") != body
        ):
            raise CMSClientFailed("response_json", retryable=True)
        if status not in success:
            if (
                set(value) == {"schema", "status", "error"}
                and value.get("schema") == _API.API_SCHEMA
                and value.get("status") == "BLOCK"
                and isinstance(value.get("error"), str)
                and ERROR_CODE.fullmatch(value["error"]) is not None
            ):
                retryable = status in {408, 425, 429} or 500 <= status <= 599
                raise CMSClientFailed(value["error"], retryable=retryable)
            raise CMSClientFailed("http_status", retryable=500 <= status <= 599)
        return value

    @staticmethod
    def _valid_progress(value: Mapping[str, Any]) -> bool:
        counts = value.get("counts")
        locales = value.get("locales")
        recovery = value.get("queue_recovery_pending")
        locale_keys = {
            "job_id", "target_locale", "status", "attempts", "max_attempts",
            "next_attempt_at", "lease_expires_at", "lease_expired",
            "last_error_code", "last_error_detail_hash", "result_sha256",
        }
        queue_statuses = {"pending", "leased", "retry_wait", "succeeded", "failed"}
        synthetic_statuses = {"cancelled", "awaiting_queue_resume"}

        def valid_locale(item: Any) -> bool:
            if not isinstance(item, dict) or set(item) != locale_keys:
                return False
            status = item.get("status")
            attempts = item.get("attempts")
            maximum = item.get("max_attempts")
            lease = item.get("lease_expires_at")
            error = item.get("last_error_code")
            synthetic = status in synthetic_statuses
            return (
                _valid_token(item.get("job_id"))
                and _valid_token(item.get("target_locale"))
                and status in queue_statuses | synthetic_statuses
                and isinstance(attempts, int)
                and not isinstance(attempts, bool)
                and isinstance(maximum, int)
                and not isinstance(maximum, bool)
                and 0 <= attempts <= maximum
                and ((synthetic and maximum == 0) or (not synthetic and maximum > 0))
                and _valid_nonnegative_number(item.get("next_attempt_at"))
                and (lease is None or _valid_nonnegative_number(lease))
                and isinstance(item.get("lease_expired"), bool)
                and (status == "leased") == (lease is not None)
                and (error is None or (
                    isinstance(error, str) and ERROR_CODE.fullmatch(error) is not None
                ))
                and _valid_sha256(
                    item.get("last_error_detail_hash"), optional=True,
                )
                and _valid_sha256(item.get("result_sha256"), optional=True)
                and (status == "succeeded")
                == (item.get("result_sha256") is not None)
            )

        return (
            _valid_token(value.get("plan_id"))
            and _valid_token(value.get("website_version"))
            and isinstance(value.get("source_sequence"), int)
            and not isinstance(value.get("source_sequence"), bool)
            and value["source_sequence"] > 0
            and isinstance(value.get("job_count"), int)
            and not isinstance(value.get("job_count"), bool)
            and value["job_count"] >= 0
            and isinstance(counts, dict)
            and set(counts) in (queue_statuses, queue_statuses | {"cancelled"})
            and ("cancelled" not in counts or value.get("cancelled") is True)
            and all(
                isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
                for count in counts.values()
            )
            and isinstance(recovery, bool)
            and sum(counts.values()) == (0 if recovery else value["job_count"])
            and isinstance(locales, list)
            and len(locales) == value["job_count"]
            and all(valid_locale(item) for item in locales)
            and [item["target_locale"] for item in locales]
            == sorted({item["target_locale"] for item in locales})
            and isinstance(value.get("cancelled"), bool)
            and not (value["cancelled"] and recovery)
            and all(
                item["status"] == "awaiting_queue_resume" for item in locales
            ) == recovery
            and (
                all(item["status"] != "cancelled" for item in locales)
                or value["cancelled"]
            )
        )

    @staticmethod
    def _valid_lifecycle(value: Mapping[str, Any]) -> bool:
        required = value.get("required_locales")
        approved = value.get("approved_locales")
        blocked = value.get("blocked_locales")
        counts = value.get("queue_counts")
        statuses = {
            "queue_recovery", "cancelled", "deleted", "deletion_failed",
            "deleting", "published", "publication_failed",
            "publication_blocked", "publishing", "ready",
            "localization_failed", "awaiting_approval", "processing",
        }
        queue_statuses = {"pending", "leased", "retry_wait", "succeeded", "failed"}

        def valid_attempt_state(item: Any, *, tombstone: bool) -> bool:
            keys = {
                "delivery_id", "status", "attempts", "max_attempts",
                "next_attempt_at", "lease_expires_at", "lease_expired",
                "last_error_code", "last_error_detail_hash",
            }
            if tombstone:
                keys.add("tombstone_id")
            if not isinstance(item, dict) or set(item) != keys:
                return False
            status = item.get("status")
            attempts = item.get("attempts")
            maximum = item.get("max_attempts")
            lease = item.get("lease_expires_at")
            error = item.get("last_error_code")
            return (
                _valid_token(item.get("delivery_id"))
                and (not tombstone or _valid_token(item.get("tombstone_id")))
                and status in queue_statuses
                and isinstance(attempts, int)
                and not isinstance(attempts, bool)
                and isinstance(maximum, int)
                and not isinstance(maximum, bool)
                and maximum >= 1
                and 0 <= attempts <= maximum
                and _valid_nonnegative_number(item.get("next_attempt_at"))
                and (lease is None or _valid_nonnegative_number(lease))
                and isinstance(item.get("lease_expired"), bool)
                and (status == "leased") == (lease is not None)
                and (error is None or (
                    isinstance(error, str) and ERROR_CODE.fullmatch(error) is not None
                ))
                and _valid_sha256(
                    item.get("last_error_detail_hash"), optional=True,
                )
                and (status in {"retry_wait", "failed"}) == (error is not None)
            )

        return (
            _valid_token(value.get("plan_id"))
            and _valid_token(value.get("website_version"))
            and isinstance(value.get("source_sequence"), int)
            and not isinstance(value.get("source_sequence"), bool)
            and value["source_sequence"] > 0
            and value.get("status") in statuses
            and isinstance(required, list)
            and all(_valid_token(item) for item in required)
            and required == sorted(set(required))
            and isinstance(approved, list)
            and all(_valid_token(item) for item in approved)
            and approved == sorted(set(approved))
            and set(approved).issubset(required)
            and isinstance(blocked, list)
            and all(
                isinstance(item, list)
                and len(item) == 2
                and item[0] in required
                and _valid_token(item[0])
                and isinstance(item[1], str)
                and ERROR_CODE.fullmatch(item[1]) is not None
                for item in blocked
            )
            and blocked == [list(item) for item in sorted({tuple(item) for item in blocked})]
            and isinstance(counts, dict)
            and set(counts) in (queue_statuses, queue_statuses | {"cancelled"})
            and ("cancelled" not in counts or value.get("status") == "cancelled")
            and all(
                isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
                for count in counts.values()
            )
            and (
                (value.get("status") == "queue_recovery" and sum(counts.values()) == 0)
                or (value.get("status") != "queue_recovery" and sum(counts.values()) == len(required))
            )
            and (value.get("delivery") is None or valid_attempt_state(
                value["delivery"], tombstone=False,
            ))
            and (value.get("tombstone") is None or valid_attempt_state(
                value["tombstone"], tombstone=True,
            ))
        )

    @classmethod
    def _valid_capabilities(cls, value: Mapping[str, Any]) -> bool:
        capabilities = value.get("capabilities")
        contract = value.get("api_contract")
        if not isinstance(capabilities, dict) or not isinstance(contract, dict):
            return False
        unsigned_capabilities = dict(capabilities)
        digest = unsigned_capabilities.pop("sha256", None)
        unsigned_contract = dict(contract)
        contract_digest = unsigned_contract.pop("sha256", None)
        commercial = capabilities.get("commercial_profile")
        rendering_registry = capabilities.get("commercial_rendering_registry")
        expected_locales = []
        try:
            for profile in _CMS._PLANNER.EU_OFFICIAL_LOCALES:
                commercial_quality = (
                    _CMS._PLANNER.commercial_quality_profile_for(profile.locale)
                )
                expected_locales.append({
                    "locale": profile.locale,
                    "eu_code": profile.eu_code,
                    "language": profile.language,
                    "native_name": profile.native_name,
                    "script": profile.script,
                    "direction": profile.direction,
                    "quality_profile_version": profile.quality_profile_version,
                    "quality_profile_sha256": profile.quality_profile_sha256,
                    "commercial_quality_profile_version": (
                        commercial_quality["version"]
                    ),
                    "commercial_quality_profile_sha256": (
                        commercial_quality["sha256"]
                    ),
                })
            expected_commercial = _CMS._COMMERCIAL.public_profile(
                _CMS._PLANNER.COMMERCIAL_PROFILE,
            )
            expected_rendering_registry = (
                _CMS._PLANNER.commercial_rendering_registry()
            )
            expected_publication_http = (
                _CMS.WebsiteLocalizationCMSBridge
                ._publication_http_capabilities()
            )
        except Exception:
            return False
        if (
            not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
            or digest
            != hashlib.sha256(
                _canonical_json(unsigned_capabilities, code="response_binding")
            ).hexdigest()
            or not isinstance(contract_digest, str)
            or SHA256.fullmatch(contract_digest) is None
            or contract_digest
            != hashlib.sha256(
                _canonical_json(unsigned_contract, code="response_binding")
            ).hexdigest()
            or set(contract) != {
                "schema", "api_schema", "error_schema", "operations", "sha256",
            }
            or contract.get("schema") != _API.API_CAPABILITIES_SCHEMA
            or contract.get("api_schema") != _API.API_SCHEMA
            or contract.get("error_schema") != _API.API_SCHEMA
            or set(capabilities) != {
                "schema", "change_schema", "cancellation_schema",
                "tombstone_schema", "publication_schema",
                "tombstone_delivery_schema", "plan_schema", "job_schema",
                "eu_language_source", "default_target_policy",
                "content_types", "quality_passes", "commercial_profile",
                "commercial_rendering_registry", "publication_http",
                "locales", "sha256",
            }
            or capabilities.get("schema") != _CMS.CAPABILITIES_SCHEMA
            or capabilities.get("change_schema") != _CMS.CHANGE_SCHEMA
            or capabilities.get("cancellation_schema")
            != _CMS.CANCELLATION_SCHEMA
            or capabilities.get("tombstone_schema") != _CMS.TOMBSTONE_SCHEMA
            or capabilities.get("publication_schema")
            != _CMS.PUBLICATION_SCHEMA
            or capabilities.get("tombstone_delivery_schema")
            != _CMS.TOMBSTONE_DELIVERY_SCHEMA
            or capabilities.get("plan_schema") != _CMS._PLANNER.SCHEMA
            or capabilities.get("job_schema") != _CMS._PLANNER.JOB_SCHEMA
            or capabilities.get("eu_language_source")
            != _CMS._PLANNER.EU_LANGUAGE_SOURCE
            or capabilities.get("default_target_policy")
            != _CMS._DEFAULT_TARGET_POLICY
            or capabilities.get("content_types")
            != sorted(_CMS._PLANNER.CONTENT_TYPES)
            or capabilities.get("quality_passes")
            != list(_CMS._PLANNER.QUALITY_PASSES)
            or commercial != expected_commercial
            or rendering_registry != expected_rendering_registry
            or capabilities.get("publication_http")
            != expected_publication_http
            or capabilities.get("locales") != expected_locales
        ):
            return False
        operations = contract.get("operations")
        if not isinstance(operations, list) or len(operations) != len(cls._OPERATIONS):
            return False
        for item, (name, (path, request_schema)) in zip(
            operations, cls._OPERATIONS.items(),
        ):
            if (
                not isinstance(item, dict)
                or set(item) != {
                    "name", "method", "path", "request_schema",
                    "response_schema", "enabled",
                }
                or item.get("name") != name
                or item.get("method") != "POST"
                or item.get("path") != path
                or item.get("request_schema") != request_schema
                or item.get("response_schema")
                != (
                    _API.LIFECYCLE_RESPONSE_SCHEMA
                    if name == "lifecycle" else _API.API_SCHEMA
                )
                or not isinstance(item.get("enabled"), bool)
            ):
                return False
        return True
