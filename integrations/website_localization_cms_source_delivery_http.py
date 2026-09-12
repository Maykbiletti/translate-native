#!/usr/bin/env python3
"""Authenticated WSGI sidecar for durable website-source delivery.

The boundary lets any website backend persist one source change, cancellation,
or tombstone in the protected delivery runtime without importing Python code.
Authentication receives request metadata and the exact body digest, never a
parsed website payload. Operational responses contain only durable identities,
hashes, counters, states, and stable error codes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping


API_SCHEMA = "blun.cms-source-delivery-sidecar-api.v1"
ERROR_SCHEMA = "blun.cms-source-delivery-sidecar-error.v1"
AUTH_REQUEST_SCHEMA = "blun.cms-source-delivery-sidecar-auth-request.v1"
PRINCIPAL_SCHEMA = "blun.cms-source-delivery-sidecar-principal.v1"
TENANT_PRINCIPAL_SCHEMA = (
    "blun.cms-source-delivery-sidecar-tenant-principal.v1"
)
CHANGE_REQUEST_SCHEMA = "blun.cms-source-delivery-change-request.v1"
REMOVAL_REQUEST_SCHEMA = "blun.cms-source-delivery-removal-request.v1"
STATUS_REQUEST_SCHEMA = "blun.cms-source-delivery-status-request.v1"
SOURCE_STATUS_REQUEST_SCHEMA = (
    "blun.cms-source-delivery-source-status-request.v1"
)
QUEUE_RESPONSE_SCHEMA = "blun.cms-source-delivery-queue-response.v1"
STATUS_RESPONSE_SCHEMA = "blun.cms-source-delivery-status-response.v1"
SOURCE_STATUS_RESPONSE_SCHEMA = (
    "blun.cms-source-delivery-source-status-response.v1"
)
SOURCE_READINESS_RESPONSE_SCHEMA = (
    "blun.cms-source-delivery-source-readiness-response.v1"
)
SOURCE_HEALTH_RESPONSE_SCHEMA = (
    "blun.cms-source-delivery-source-health-response.v1"
)
HEALTH_RESPONSE_SCHEMA = "blun.cms-source-delivery-health-response.v1"
READINESS_RESPONSE_SCHEMA = (
    "blun.cms-source-delivery-readiness-response.v1"
)
CAPABILITIES_SCHEMA = "blun.cms-source-delivery-capabilities.v1"
CAPABILITIES_RESPONSE_SCHEMA = (
    "blun.cms-source-delivery-capabilities-response.v1"
)

CHANGE_PATH = "/v1/localization/source-delivery/changes"
REMOVAL_PATH = "/v1/localization/source-delivery/removals"
STATUS_PATH = "/v1/localization/source-delivery/status"
SOURCE_STATUS_PATH = "/v1/localization/source-delivery/source-status"
SOURCE_READINESS_PATH = "/v1/localization/source-delivery/source-readiness"
SOURCE_HEALTH_PATH = "/v1/localization/source-delivery/source-health"
HEALTH_PATH = "/v1/localization/source-delivery/health"
READINESS_PATH = "/v1/localization/source-delivery/readiness"
CAPABILITIES_PATH = "/v1/localization/source-delivery/capabilities"

MAX_BODY_BYTES = 4_000_000
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
OPERATIONS = ("change", "cancellation", "tombstone")
WORKER_STATES = (
    "unmanaged", "running", "stopping", "stopped", "failed", "closed",
)
SCOPES = {
    CHANGE_PATH: "source-delivery-change:write",
    REMOVAL_PATH: "source-delivery-removal:write",
    STATUS_PATH: "source-delivery-status:read",
    SOURCE_STATUS_PATH: "source-delivery-source-status:read",
    SOURCE_READINESS_PATH: "source-delivery-source-readiness:read",
    SOURCE_HEALTH_PATH: "source-delivery-source-health:read",
    HEALTH_PATH: "source-delivery-health:read",
    READINESS_PATH: "source-delivery-readiness:read",
    CAPABILITIES_PATH: "source-delivery-capabilities:read",
}
METHODS = {
    CHANGE_PATH: "POST",
    REMOVAL_PATH: "POST",
    STATUS_PATH: "POST",
    SOURCE_STATUS_PATH: "POST",
    SOURCE_READINESS_PATH: "GET",
    SOURCE_HEALTH_PATH: "GET",
    HEALTH_PATH: "GET",
    READINESS_PATH: "GET",
    CAPABILITIES_PATH: "GET",
}
TENANT_PATHS = {
    CHANGE_PATH, REMOVAL_PATH, STATUS_PATH, SOURCE_STATUS_PATH,
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"cannot load source-delivery HTTP dependency: {path.name}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_HTTP = _load_module(
    "blun_website_localization_cms_source_delivery_http_source",
    _ROOT / "integrations" / "website_localization_cms_source_http.py",
)


class CMSSourceDeliveryHTTPBlocked(RuntimeError):
    """Stable content-free sidecar failure."""

    def __init__(self, code: str, status: int):
        if ERROR_CODE.fullmatch(code) is None or status not in {
            400, 401, 403, 404, 405, 409, 411, 413, 415, 503,
        }:
            raise ValueError("invalid source delivery HTTP failure")
        super().__init__(code)
        self.code = code
        self.status = status


def _canonical_json(value: Any) -> bytes:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.response_invalid", 503,
        ) from None
    if not result or len(result) > MAX_BODY_BYTES:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.response_invalid", 503,
        )
    return result


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def _token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or TOKEN.fullmatch(value) is None
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise ValueError
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ValueError
    return value


def _count(value: Any, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise ValueError
    return result


def _optional_timestamp(value: Any) -> float | None:
    return None if value is None else _timestamp(value)


def _optional_error(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or ERROR_CODE.fullmatch(value) is None:
        raise ValueError
    return value


def _optional_sha256(value: Any) -> str | None:
    return None if value is None else _sha256(value)


def _principal(value: Any, scope: str, *, tenant: bool) -> dict[str, str]:
    fields = {
        "schema", "principal_id", "credential_id", "credential_version",
        "scope",
    }
    if tenant:
        fields.add("site_id")
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.authentication_failed", 401,
        )
    try:
        expected = TENANT_PRINCIPAL_SCHEMA if tenant else PRINCIPAL_SCHEMA
        if value["schema"] != expected or value["scope"] != scope:
            if value.get("scope") != scope:
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.scope_rejected", 403,
                )
            raise ValueError
        for name in (
            "principal_id", "credential_id", "credential_version", "scope",
        ):
            _token(value[name])
        if tenant:
            _token(value["site_id"])
    except CMSSourceDeliveryHTTPBlocked:
        raise
    except (KeyError, TypeError, ValueError):
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.authentication_failed", 401,
        ) from None
    return dict(value)


def _request_identity(value: Any, *, change: bool) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise ValueError
    site_id = _token(value.get("site_id"))
    event_id = _token(value.get("event_id"))
    if change:
        request_id = event_id
    elif value.get("schema") == "blun.cms-content-cancellation.v1":
        request_id = _token(value.get("cancellation_id"))
    elif value.get("schema") == "blun.cms-content-tombstone.v1":
        request_id = _token(value.get("tombstone_id"))
    else:
        raise ValueError
    return site_id, event_id, request_id


def _status_payload(
    value: Any,
    *,
    expected_operation: str | None = None,
    expected_request_id: str | None = None,
    expected_site_id: str | None = None,
) -> dict[str, Any]:
    try:
        payload = asdict(value)
        required = {
            "operation", "request_id", "event_id", "site_id",
            "payload_sha256", "capabilities_sha256", "status", "attempts",
            "delivery_max_attempts", "source_max_attempts", "next_attempt_at",
            "lease_expires_at", "lease_expired", "last_error_code",
            "response_sha256",
        }
        if set(payload) != required:
            raise ValueError
        operation = payload["operation"]
        if operation not in OPERATIONS:
            raise ValueError
        request_id = _token(payload["request_id"])
        event_id = _token(payload["event_id"])
        site_id = _token(payload["site_id"])
        payload_hash = _sha256(payload["payload_sha256"])
        remote_contract_hash = _sha256(payload["capabilities_sha256"])
        status = payload["status"]
        if status not in STATUSES:
            raise ValueError
        attempts = _count(payload["attempts"], maximum=20)
        delivery_max = _count(
            payload["delivery_max_attempts"], minimum=1, maximum=20,
        )
        source_max = _count(
            payload["source_max_attempts"], minimum=1, maximum=20,
        )
        next_attempt = _timestamp(payload["next_attempt_at"])
        lease_expires = _optional_timestamp(payload["lease_expires_at"])
        if not isinstance(payload["lease_expired"], bool):
            raise ValueError
        error = _optional_error(payload["last_error_code"])
        response_hash = _optional_sha256(payload["response_sha256"])
        if attempts > delivery_max:
            raise ValueError
        if status == "leased" and lease_expires is None:
            raise ValueError
        if status != "leased" and lease_expires is not None:
            raise ValueError
        if status == "succeeded" and response_hash is None:
            raise ValueError
        if status != "succeeded" and response_hash is not None:
            raise ValueError
        if status in {"retry_wait", "failed"} and error is None:
            raise ValueError
        if expected_operation is not None and operation != expected_operation:
            raise ValueError
        if expected_request_id is not None and request_id != expected_request_id:
            raise ValueError
        if expected_site_id is not None and site_id != expected_site_id:
            raise ValueError
        return {
            "operation": operation,
            "request_id": request_id,
            "event_id": event_id,
            "site_id": site_id,
            "payload_sha256": payload_hash,
            "remote_capabilities_sha256": remote_contract_hash,
            "status": status,
            "attempts": attempts,
            "delivery_max_attempts": delivery_max,
            "source_max_attempts": source_max,
            "next_attempt_at": next_attempt,
            "lease_expires_at": lease_expires,
            "lease_expired": payload["lease_expired"],
            "last_error_code": error,
            "response_sha256": response_hash,
        }
    except Exception:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.runtime_response_invalid", 503,
        ) from None


class _PayloadView:
    def __init__(self, value: Mapping[str, Any]):
        self.value = value

    def as_payload(self) -> Mapping[str, Any]:
        return self.value


def _source_status_response(
    value: Any,
    *,
    expected_event_id: str,
    expected_site_id: str,
    expected_capabilities_sha256: str,
) -> dict[str, Any]:
    """Validate the complete source-service envelope without dropping fields."""

    try:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema", "status", "capabilities_sha256"}
            or value.get("schema") != _SOURCE_HTTP.STATUS_RESPONSE_SCHEMA
            or value.get("capabilities_sha256")
            != expected_capabilities_sha256
        ):
            raise ValueError
        normalized = _SOURCE_HTTP._source_status_payload(
            _PayloadView(value["status"]),
            expected_event_id=expected_event_id,
            expected_site_id=expected_site_id,
        )
        if normalized != value["status"]:
            raise ValueError
        return normalized
    except Exception:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.runtime_response_invalid", 503,
        ) from None


def _source_readiness_response(
    value: Any,
    *,
    expected_capabilities_sha256: str,
) -> dict[str, Any]:
    """Validate the source-service readiness envelope without weakening it."""

    try:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema", "readiness", "capabilities_sha256"}
            or value.get("schema") != _SOURCE_HTTP.READINESS_RESPONSE_SCHEMA
            or value.get("capabilities_sha256")
            != expected_capabilities_sha256
        ):
            raise ValueError
        normalized = _SOURCE_HTTP._readiness_payload(value["readiness"])
        if normalized != value["readiness"]:
            raise ValueError
        return normalized
    except Exception:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.runtime_response_invalid", 503,
        ) from None


class _SourcePayloadView:
    """Expose an immutable decoded payload to the source HTTP validator."""

    def __init__(self, payload: Mapping[str, Any]):
        self._payload = payload

    def as_payload(self) -> dict[str, Any]:
        return dict(self._payload)


def _source_health_response(
    value: Any,
    *,
    expected_capabilities_sha256: str,
) -> dict[str, Any]:
    """Validate the source-service health envelope without weakening it."""

    try:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema", "health", "capabilities_sha256"}
            or value.get("schema") != _SOURCE_HTTP.HEALTH_RESPONSE_SCHEMA
            or value.get("capabilities_sha256")
            != expected_capabilities_sha256
            or not isinstance(value.get("health"), Mapping)
        ):
            raise ValueError
        normalized = _SOURCE_HTTP._health_payload(
            _SourcePayloadView(value["health"])
        )
        if normalized != value["health"]:
            raise ValueError
        return normalized
    except Exception:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.runtime_response_invalid", 503,
        ) from None


def _health_payload(value: Any) -> dict[str, Any]:
    try:
        payload = value.as_payload()
        required = {
            "schema", "status", "counts", "operations", "due",
            "expired_leases", "failed", "contract_mismatches", "error_code",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise ValueError
        if payload["schema"] != "blun.cms-source-delivery-health.v1":
            raise ValueError
        if payload["status"] not in {"ok", "blocked"}:
            raise ValueError
        due = _count(payload["due"])
        expired = _count(payload["expired_leases"])
        failed = _count(payload["failed"])
        mismatches = _count(payload["contract_mismatches"])
        error = _optional_error(payload["error_code"])
        integrity_block = (
            payload["status"] == "blocked"
            and payload["counts"] == {}
            and payload["operations"] == {}
            and (due, expired, failed, mismatches) == (0, 0, 0, 0)
            and error == "source_delivery.integrity"
        )
        if integrity_block:
            counts = {}
            operations = {}
        else:
            if set(payload["counts"]) != set(STATUSES):
                raise ValueError
            if set(payload["operations"]) != set(OPERATIONS):
                raise ValueError
            counts = {
                name: _count(payload["counts"][name]) for name in STATUSES
            }
            operations = {
                name: _count(payload["operations"][name])
                for name in OPERATIONS
            }
        if (
            not integrity_block
            and (
                sum(counts.values()) != sum(operations.values())
                or due > counts["pending"] + counts["retry_wait"]
                or expired > counts["leased"]
                or failed != counts["failed"]
                or mismatches > (
                    counts["pending"]
                    + counts["leased"]
                    + counts["retry_wait"]
                )
                or (payload["status"] == "ok")
                != (
                    failed == 0
                    and expired == 0
                    and mismatches == 0
                    and error is None
                )
                or (payload["status"] == "blocked" and error is None)
            )
        ):
            raise ValueError
        return {
            "schema": payload["schema"],
            "status": payload["status"],
            "counts": counts,
            "operations": operations,
            "due": due,
            "expired_leases": expired,
            "failed": failed,
            "contract_mismatches": mismatches,
            "error_code": error,
        }
    except Exception:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.runtime_response_invalid", 503,
        ) from None


def _readiness_payload(value: Any) -> dict[str, Any]:
    try:
        required = {
            "schema", "status", "worker_state", "outbox_status", "error_code",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError
        if value["schema"] != "blun.cms-source-delivery-worker-readiness.v1":
            raise ValueError
        if value["status"] not in {"ready", "not_ready"}:
            raise ValueError
        if value["worker_state"] not in WORKER_STATES:
            raise ValueError
        if value["outbox_status"] not in {None, "ok", "blocked"}:
            raise ValueError
        error = _optional_error(value["error_code"])
        ready = (
            value["status"] == "ready"
            and value["worker_state"] == "running"
            and value["outbox_status"] == "ok"
            and error is None
        )
        if ready != (value["status"] == "ready"):
            raise ValueError
        if value["status"] == "not_ready" and error is None:
            raise ValueError
        return {
            "schema": value["schema"],
            "status": value["status"],
            "worker_state": value["worker_state"],
            "outbox_status": value["outbox_status"],
            "error_code": error,
        }
    except Exception:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.runtime_response_invalid", 503,
        ) from None


def _capabilities_payload() -> dict[str, Any]:
    definitions = (
        (
            "capabilities", "GET", CAPABILITIES_PATH,
            "source-delivery-capabilities:read", PRINCIPAL_SCHEMA, None,
            CAPABILITIES_RESPONSE_SCHEMA, 200,
        ),
        (
            "change", "POST", CHANGE_PATH, "source-delivery-change:write",
            TENANT_PRINCIPAL_SCHEMA, CHANGE_REQUEST_SCHEMA,
            QUEUE_RESPONSE_SCHEMA, 202,
        ),
        (
            "health", "GET", HEALTH_PATH, "source-delivery-health:read",
            PRINCIPAL_SCHEMA, None, HEALTH_RESPONSE_SCHEMA, 200,
        ),
        (
            "readiness", "GET", READINESS_PATH,
            "source-delivery-readiness:read", PRINCIPAL_SCHEMA, None,
            READINESS_RESPONSE_SCHEMA, 200,
        ),
        (
            "removal", "POST", REMOVAL_PATH,
            "source-delivery-removal:write", TENANT_PRINCIPAL_SCHEMA,
            REMOVAL_REQUEST_SCHEMA, QUEUE_RESPONSE_SCHEMA, 202,
        ),
        (
            "status", "POST", STATUS_PATH, "source-delivery-status:read",
            TENANT_PRINCIPAL_SCHEMA, STATUS_REQUEST_SCHEMA,
            STATUS_RESPONSE_SCHEMA, 200,
        ),
        (
            "source_status", "POST", SOURCE_STATUS_PATH,
            "source-delivery-source-status:read", TENANT_PRINCIPAL_SCHEMA,
            SOURCE_STATUS_REQUEST_SCHEMA, SOURCE_STATUS_RESPONSE_SCHEMA, 200,
        ),
        (
            "source_readiness", "GET", SOURCE_READINESS_PATH,
            "source-delivery-source-readiness:read", PRINCIPAL_SCHEMA,
            None, SOURCE_READINESS_RESPONSE_SCHEMA, 200,
        ),
        (
            "source_health", "GET", SOURCE_HEALTH_PATH,
            "source-delivery-source-health:read", PRINCIPAL_SCHEMA,
            None, SOURCE_HEALTH_RESPONSE_SCHEMA, 200,
        ),
    )
    try:
        operations = {}
        for (
            name, method, path, scope, principal_schema, request_schema,
            response_schema, success_status,
        ) in definitions:
            if METHODS.get(path) != method or SCOPES.get(path) != scope:
                raise ValueError
            operations[name] = {
                "method": method,
                "path": path,
                "scope": scope,
                "principal_schema": principal_schema,
                "request_schema": request_schema,
                "response_schema": response_schema,
                "success_status": success_status,
            }
        if set(METHODS) != {item[2] for item in definitions}:
            raise ValueError
        if set(SCOPES) != set(METHODS):
            raise ValueError
        contract = {
            "schema": CAPABILITIES_SCHEMA,
            "api_schema": API_SCHEMA,
            "operations": operations,
            "limits": {
                "max_body_bytes": MAX_BODY_BYTES,
                "max_headers": MAX_HEADERS,
                "max_header_value_bytes": MAX_HEADER_VALUE,
                "max_source_attempts": 20,
                "max_delivery_attempts": 20,
            },
            "semantics": {
                "authentication_binds_exact_body_sha256": True,
                "content_free_operational_reads": True,
                "delivery_retries_are_durable": True,
                "status_is_site_bound": True,
                "source_status_requires_accepted_submission": True,
                "source_readiness_is_independently_validated": True,
                "source_health_is_independently_validated": True,
                "write_requires_ready_worker": True,
            },
        }
        encoded = _canonical_json(contract)
    except CMSSourceDeliveryHTTPBlocked:
        raise
    except Exception:
        raise CMSSourceDeliveryHTTPBlocked(
            "source_delivery_http.capabilities_invalid", 503,
        ) from None
    return {**contract, "sha256": hashlib.sha256(encoded).hexdigest()}


class CMSSourceDeliveryHTTPApplication:
    """Strict authenticated WSGI adapter over one hosted delivery runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if not all(callable(getattr(runtime, name, None)) for name in (
            "enqueue_change", "enqueue_removal", "status", "health",
            "source_health", "source_readiness", "worker_readiness",
        )):
            raise TypeError("runtime must provide source delivery operations")
        if not callable(authenticator):
            raise TypeError("authenticator must be callable")
        self.runtime = runtime
        self.authenticator = authenticator

    @staticmethod
    def _headers(environ: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        headers = []
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").lower()
            elif key in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                name = key.replace("_", "-").lower()
            else:
                continue
            if (
                HEADER_NAME.fullmatch(name) is None
                or not isinstance(value, str)
                or len(value) > MAX_HEADER_VALUE
                or "\r" in value
                or "\n" in value
            ):
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.headers_invalid", 400,
                )
            headers.append((name, value))
        headers.sort()
        if (
            len(headers) > MAX_HEADERS
            or len({name for name, _ in headers}) != len(headers)
        ):
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.headers_invalid", 400,
            )
        return tuple(headers)

    @staticmethod
    def _content_type(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        parts = [part.strip().lower() for part in value.split(";")]
        return parts in (
            ["application/json"], ["application/json", "charset=utf-8"],
        )

    @staticmethod
    def _body(environ: Mapping[str, Any], *, required: bool) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.transfer_encoding_rejected", 400,
            )
        length = environ.get("CONTENT_LENGTH")
        if not required and length in {None, "", "0"}:
            return b""
        if (
            not isinstance(length, str)
            or not length.isascii()
            or not length.isdecimal()
        ):
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.content_length_required", 411,
            )
        size = int(length)
        if size <= 0:
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.body_invalid", 400,
            )
        if size > MAX_BODY_BYTES:
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.body_too_large", 413,
            )
        stream = environ.get("wsgi.input")
        try:
            body = stream.read(size)
        except Exception:
            body = None
        if not isinstance(body, bytes) or len(body) != size:
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.body_invalid", 400,
            )
        return body

    def _authenticate(
        self,
        environ: Mapping[str, Any],
        path: str,
        body: bytes,
    ) -> dict[str, str]:
        request = {
            "schema": AUTH_REQUEST_SCHEMA,
            "method": environ["REQUEST_METHOD"],
            "path": path,
            "headers": [list(item) for item in self._headers(environ)],
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        try:
            principal = self.authenticator(request)
        except Exception:
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.authentication_unavailable", 503,
            ) from None
        return _principal(
            principal, SCOPES[path], tenant=path in TENANT_PATHS,
        )

    @staticmethod
    def _request(body: bytes) -> Any:
        try:
            text = body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError
            return json.loads(
                text,
                object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.json_invalid", 400,
            ) from None

    @staticmethod
    def _send(start_response, status: int, payload: Mapping[str, Any]):
        body = _canonical_json(dict(payload))
        phrases = {
            200: "OK", 202: "Accepted", 400: "Bad Request",
            401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 409: "Conflict",
            411: "Length Required", 413: "Content Too Large",
            415: "Unsupported Media Type", 503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return [body]

    @classmethod
    def _error(cls, start_response, failure: CMSSourceDeliveryHTTPBlocked):
        return cls._send(start_response, failure.status, {
            "schema": ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": failure.code,
        })

    @staticmethod
    def _write_binding(
        environ: Mapping[str, Any], payload: Mapping[str, Any], request_id: str,
    ) -> str:
        payload_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if (
            environ.get("HTTP_IDEMPOTENCY_KEY") != request_id
            or environ.get("HTTP_X_LOCALIZATION_SOURCE_PAYLOAD_SHA256")
            != payload_hash
        ):
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.binding_invalid", 400,
            )
        return payload_hash

    def _ready(self) -> dict[str, Any]:
        try:
            readiness = _readiness_payload(self.runtime.worker_readiness())
        except CMSSourceDeliveryHTTPBlocked:
            raise
        except Exception:
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.runtime_blocked", 503,
            ) from None
        if readiness["status"] != "ready":
            raise CMSSourceDeliveryHTTPBlocked(
                "source_delivery_http.runtime_not_ready", 503,
            )
        return readiness

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            if not isinstance(environ, Mapping):
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.environment_invalid", 400,
                )
            path = environ.get("PATH_INFO")
            if path not in SCOPES:
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.route_not_found", 404,
                )
            method = environ.get("REQUEST_METHOD")
            if method != METHODS[path]:
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.method_not_allowed", 405,
                )
            if environ.get("wsgi.url_scheme") != "https":
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.https_required", 400,
                )
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.query_rejected", 400,
                )
            bodyless = path in {
                HEALTH_PATH, READINESS_PATH, SOURCE_HEALTH_PATH,
                SOURCE_READINESS_PATH, CAPABILITIES_PATH,
            }
            if bodyless:
                body = self._body(environ, required=False)
                if body or environ.get("CONTENT_TYPE") not in {None, ""}:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.body_not_allowed", 400,
                    )
            else:
                if not self._content_type(environ.get("CONTENT_TYPE")):
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.content_type_invalid", 415,
                    )
                body = self._body(environ, required=True)
            principal = self._authenticate(environ, path, body)
            capabilities = _capabilities_payload()

            if path == CAPABILITIES_PATH:
                return self._send(start_response, 200, {
                    "schema": CAPABILITIES_RESPONSE_SCHEMA,
                    "capabilities": capabilities,
                })
            if path == HEALTH_PATH:
                try:
                    health = _health_payload(self.runtime.health())
                except CMSSourceDeliveryHTTPBlocked:
                    raise
                except Exception:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.runtime_blocked", 503,
                    ) from None
                status = 200 if health["status"] == "ok" else 503
                return self._send(start_response, status, {
                    "schema": HEALTH_RESPONSE_SCHEMA,
                    "health": health,
                    "capabilities_sha256": capabilities["sha256"],
                })
            if path == READINESS_PATH:
                try:
                    readiness = _readiness_payload(
                        self.runtime.worker_readiness(),
                    )
                except CMSSourceDeliveryHTTPBlocked:
                    raise
                except Exception:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.runtime_blocked", 503,
                    ) from None
                status = 200 if readiness["status"] == "ready" else 503
                return self._send(start_response, status, {
                    "schema": READINESS_RESPONSE_SCHEMA,
                    "readiness": readiness,
                    "capabilities_sha256": capabilities["sha256"],
                })
            if path == SOURCE_READINESS_PATH:
                try:
                    source_response = self.runtime.source_readiness()
                except CMSSourceDeliveryHTTPBlocked:
                    raise
                except Exception:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.runtime_blocked", 503,
                    ) from None
                source_readiness = _source_readiness_response(
                    source_response,
                    expected_capabilities_sha256=(
                        self.runtime.expected_capabilities_sha256
                    ),
                )
                status = (
                    200 if source_readiness["status"] == "ready" else 503
                )
                return self._send(start_response, status, {
                    "schema": SOURCE_READINESS_RESPONSE_SCHEMA,
                    "source_readiness": source_readiness,
                    "source_capabilities_sha256": (
                        self.runtime.expected_capabilities_sha256
                    ),
                    "capabilities_sha256": capabilities["sha256"],
                })
            if path == SOURCE_HEALTH_PATH:
                try:
                    source_response = self.runtime.source_health()
                except CMSSourceDeliveryHTTPBlocked:
                    raise
                except Exception:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.runtime_blocked", 503,
                    ) from None
                source_health = _source_health_response(
                    source_response,
                    expected_capabilities_sha256=(
                        self.runtime.expected_capabilities_sha256
                    ),
                )
                status = 503 if source_health["status"] == "blocked" else 200
                return self._send(start_response, status, {
                    "schema": SOURCE_HEALTH_RESPONSE_SCHEMA,
                    "source_health": source_health,
                    "source_capabilities_sha256": (
                        self.runtime.expected_capabilities_sha256
                    ),
                    "capabilities_sha256": capabilities["sha256"],
                })

            request = self._request(body)
            if path == SOURCE_STATUS_PATH:
                if (
                    not isinstance(request, Mapping)
                    or set(request) != {
                        "schema", "event_id", "site_id", "payload_sha256",
                    }
                    or request.get("schema") != SOURCE_STATUS_REQUEST_SCHEMA
                ):
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.request_invalid", 400,
                    )
                try:
                    event_id = _token(request["event_id"])
                    site_id = _token(request["site_id"])
                    payload_sha256 = _sha256(request["payload_sha256"])
                except (KeyError, TypeError, ValueError):
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.request_invalid", 400,
                    ) from None
                if site_id != principal["site_id"]:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.status_not_found", 404,
                    )
                try:
                    source_response = self.runtime.source_status(
                        event_id,
                        site_id,
                        payload_sha256,
                    )
                except Exception as error:
                    code = getattr(error, "code", None)
                    if code == "source_delivery_runtime.request_invalid":
                        raise CMSSourceDeliveryHTTPBlocked(
                            "source_delivery_http.request_invalid", 400,
                        ) from None
                    if code == "source_delivery_runtime.status_not_found":
                        raise CMSSourceDeliveryHTTPBlocked(
                            "source_delivery_http.status_not_found", 404,
                        ) from None
                    if code == (
                        "source_delivery_runtime.source_status_unavailable"
                    ):
                        raise CMSSourceDeliveryHTTPBlocked(
                            "source_delivery_http.source_status_unavailable",
                            409,
                        ) from None
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.runtime_blocked", 503,
                    ) from None
                source_status = _source_status_response(
                    source_response,
                    expected_event_id=event_id,
                    expected_site_id=site_id,
                    expected_capabilities_sha256=(
                        self.runtime.expected_capabilities_sha256
                    ),
                )
                return self._send(start_response, 200, {
                    "schema": SOURCE_STATUS_RESPONSE_SCHEMA,
                    "source_status": source_status,
                    "source_capabilities_sha256": (
                        self.runtime.expected_capabilities_sha256
                    ),
                    "capabilities_sha256": capabilities["sha256"],
                })

            if path == STATUS_PATH:
                if (
                    not isinstance(request, Mapping)
                    or set(request) != {
                        "schema", "operation", "request_id", "site_id",
                    }
                    or request.get("schema") != STATUS_REQUEST_SCHEMA
                    or request.get("operation") not in OPERATIONS
                ):
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.request_invalid", 400,
                    )
                try:
                    operation = request["operation"]
                    request_id = _token(request["request_id"])
                    site_id = _token(request["site_id"])
                except (KeyError, TypeError, ValueError):
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.request_invalid", 400,
                    ) from None
                if site_id != principal["site_id"]:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.status_not_found", 404,
                    )
                try:
                    status_value = self.runtime.status(operation, request_id)
                except Exception as error:
                    code = getattr(error, "code", None)
                    if code == "source_delivery_runtime.request_invalid":
                        raise CMSSourceDeliveryHTTPBlocked(
                            "source_delivery_http.request_invalid", 400,
                        ) from None
                    if code == "source_delivery_runtime.status_not_found":
                        raise CMSSourceDeliveryHTTPBlocked(
                            "source_delivery_http.status_not_found", 404,
                        ) from None
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.runtime_blocked", 503,
                    ) from None
                status_payload = _status_payload(
                    status_value,
                    expected_operation=operation,
                    expected_request_id=request_id,
                )
                if status_payload["site_id"] != site_id:
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.status_not_found", 404,
                    )
                return self._send(start_response, 200, {
                    "schema": STATUS_RESPONSE_SCHEMA,
                    "status": status_payload,
                    "capabilities_sha256": capabilities["sha256"],
                })

            envelope_key = "change" if path == CHANGE_PATH else "removal"
            expected_schema = (
                CHANGE_REQUEST_SCHEMA if path == CHANGE_PATH
                else REMOVAL_REQUEST_SCHEMA
            )
            if (
                not isinstance(request, Mapping)
                or set(request) != {
                    "schema", envelope_key, "source_max_attempts",
                    "delivery_max_attempts",
                }
                or request.get("schema") != expected_schema
            ):
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.request_invalid", 400,
                )
            try:
                source_max = _count(
                    request["source_max_attempts"], minimum=1, maximum=20,
                )
                delivery_max = _count(
                    request["delivery_max_attempts"], minimum=1, maximum=20,
                )
                payload = request[envelope_key]
                site_id, _event_id, request_id = _request_identity(
                    payload, change=path == CHANGE_PATH,
                )
            except (KeyError, TypeError, ValueError):
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.request_invalid", 400,
                ) from None
            if site_id != principal["site_id"]:
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.site_rejected", 403,
                )
            payload_hash = self._write_binding(environ, payload, request_id)
            self._ready()
            try:
                if path == CHANGE_PATH:
                    queued = self.runtime.enqueue_change(
                        payload,
                        source_max_attempts=source_max,
                        delivery_max_attempts=delivery_max,
                    )
                else:
                    queued = self.runtime.enqueue_removal(
                        payload,
                        source_max_attempts=source_max,
                        delivery_max_attempts=delivery_max,
                    )
            except Exception as error:
                code = getattr(error, "code", None)
                if code == "source_delivery_runtime.request_invalid":
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.request_invalid", 400,
                    ) from None
                if code == "source_delivery_runtime.idempotency_collision":
                    raise CMSSourceDeliveryHTTPBlocked(
                        "source_delivery_http.idempotency_collision", 409,
                    ) from None
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.runtime_blocked", 503,
                ) from None
            response = _status_payload(
                queued,
                expected_operation=("change" if path == CHANGE_PATH else None),
                expected_request_id=request_id,
                expected_site_id=site_id,
            )
            if response["payload_sha256"] != payload_hash:
                raise CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.runtime_response_invalid", 503,
                )
            return self._send(start_response, 202, {
                "schema": QUEUE_RESPONSE_SCHEMA,
                "queue": response,
                "capabilities_sha256": capabilities["sha256"],
            })
        except CMSSourceDeliveryHTTPBlocked as failure:
            return self._error(start_response, failure)
        except Exception:
            return self._error(
                start_response,
                CMSSourceDeliveryHTTPBlocked(
                    "source_delivery_http.internal", 503,
                ),
            )
