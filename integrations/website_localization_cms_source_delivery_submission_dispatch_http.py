#!/usr/bin/env python3
"""Authenticated HTTP sidecar for the caller-owned submission outbox.

Authentication sees transport metadata and the exact body digest before JSON
is parsed. Tenant writes and reads are then bound to the authenticated site,
canonical source-payload hash, request identity, and runtime capability pin.
Responses are content-free and never grant localization or publication.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping


API_SCHEMA = "blun.cms-public-submission-dispatch-http.v5"
ERROR_SCHEMA = "blun.cms-public-submission-dispatch-http-error.v1"
AUTH_REQUEST_SCHEMA = "blun.cms-public-submission-dispatch-auth-request.v1"
TENANT_PRINCIPAL_SCHEMA = (
    "blun.cms-public-submission-dispatch-tenant-principal.v1"
)
OPERATOR_PRINCIPAL_SCHEMA = (
    "blun.cms-public-submission-dispatch-operator-principal.v1"
)
ENQUEUE_REQUEST_SCHEMA = (
    "blun.cms-public-submission-dispatch-enqueue-request.v1"
)
STATUS_REQUEST_SCHEMA = "blun.cms-public-submission-dispatch-status-request.v1"
QUEUE_RESPONSE_SCHEMA = "blun.cms-public-submission-dispatch-queue-response.v1"
STATUS_RESPONSE_SCHEMA = "blun.cms-public-submission-dispatch-status-response.v1"
HEALTH_RESPONSE_SCHEMA = "blun.cms-public-submission-dispatch-health-response.v1"
READINESS_RESPONSE_SCHEMA = (
    "blun.cms-public-submission-dispatch-readiness-response.v1"
)
CAPABILITIES_SCHEMA = "blun.cms-public-submission-dispatch-capabilities.v5"
CAPABILITIES_RESPONSE_SCHEMA = (
    "blun.cms-public-submission-dispatch-capabilities-response.v5"
)
OPENAPI_RESPONSE_SCHEMA = (
    "blun.cms-public-submission-dispatch-openapi-response.v1"
)

ENQUEUE_PATH = "/v1/localization/cms-submission-dispatch/requests"
STATUS_PATH = "/v1/localization/cms-submission-dispatch/status"
HEALTH_PATH = "/v1/localization/cms-submission-dispatch/health"
READINESS_PATH = "/v1/localization/cms-submission-dispatch/readiness"
CAPABILITIES_PATH = "/v1/localization/cms-submission-dispatch/capabilities"
OPENAPI_PATH = "/v1/localization/cms-submission-dispatch/openapi"
SCOPES = {
    ENQUEUE_PATH: "cms-submission-dispatch:write",
    STATUS_PATH: "cms-submission-dispatch-status:read",
    HEALTH_PATH: "cms-submission-dispatch-health:read",
    READINESS_PATH: "cms-submission-dispatch-readiness:read",
    CAPABILITIES_PATH: "cms-submission-dispatch-capabilities:read",
    OPENAPI_PATH: "cms-submission-dispatch-openapi:read",
}
METHODS = {
    ENQUEUE_PATH: "POST",
    STATUS_PATH: "POST",
    HEALTH_PATH: "GET",
    READINESS_PATH: "GET",
    CAPABILITIES_PATH: "GET",
    OPENAPI_PATH: "GET",
}
TENANT_PATHS = {ENQUEUE_PATH, STATUS_PATH}
BODYLESS_PATHS = {HEALTH_PATH, READINESS_PATH, CAPABILITIES_PATH, OPENAPI_PATH}
ERROR_STATUSES = {
    CAPABILITIES_PATH: (400, 401, 403, 405, 503),
    ENQUEUE_PATH: (400, 401, 403, 405, 409, 411, 413, 415, 503),
    HEALTH_PATH: (400, 401, 403, 405, 503),
    OPENAPI_PATH: (400, 401, 403, 405, 503),
    READINESS_PATH: (400, 401, 403, 405, 503),
    STATUS_PATH: (400, 401, 403, 404, 405, 411, 413, 415, 503),
}
MAX_BODY_BYTES = 4_000_000
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
STATUSES = {"pending", "leased", "retry_wait", "accepted", "failed"}
OPERATIONS = {"change", "cancellation", "tombstone"}
WORKER_STATES = {
    "unmanaged", "running", "stopping", "stopped", "failed", "closed",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load submission dispatch HTTP dependency")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_DISPATCH = _load_module(
    "blun_website_localization_submission_dispatch_http_store",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_dispatch.py",
)
_OPENAPI = _load_module(
    "blun_website_localization_submission_dispatch_http_openapi",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_dispatch_openapi.py",
)


class CMSSourceDeliverySubmissionDispatchHTTPBlocked(RuntimeError):
    """Stable content-free HTTP failure."""

    def __init__(self, code: str, status: int):
        if ERROR_CODE.fullmatch(code) is None or status not in {
            400, 401, 403, 404, 405, 409, 411, 413, 415, 503,
        }:
            raise ValueError("invalid submission dispatch HTTP failure")
        super().__init__(code)
        self.code = code
        self.status = status


def _blocked(code: str, status: int):
    return CMSSourceDeliverySubmissionDispatchHTTPBlocked(
        "submission_dispatch_http." + code, status,
    )


def _canonical(value: Any, *, response: bool = False) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise _blocked(
            "response_invalid" if response else "request_invalid",
            503 if response else 400,
        ) from None
    if not raw or len(raw) > (1_000_000 if response else MAX_BODY_BYTES):
        raise _blocked(
            "response_invalid" if response else "request_invalid",
            503 if response else 400,
        )
    return raw


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


def _count(value: Any, *, minimum: int = 0, maximum: int = 20) -> int:
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


def _optional(value: Any, validator: Callable[[Any], Any]) -> Any:
    return None if value is None else validator(value)


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
            raise _blocked("headers_invalid", 400)
        headers.append((name, value))
    headers.sort()
    if (
        len(headers) > MAX_HEADERS
        or len({name for name, _ in headers}) != len(headers)
    ):
        raise _blocked("headers_invalid", 400)
    return tuple(headers)


def _body(environ: Mapping[str, Any], *, required: bool) -> bytes:
    if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
        raise _blocked("transfer_encoding_rejected", 400)
    length = environ.get("CONTENT_LENGTH")
    if not required and length in {None, "", "0"}:
        return b""
    if (
        not isinstance(length, str)
        or not length.isascii()
        or not length.isdecimal()
    ):
        raise _blocked("content_length_required", 411)
    size = int(length)
    if size <= 0:
        raise _blocked("body_invalid", 400)
    if size > MAX_BODY_BYTES:
        raise _blocked("body_too_large", 413)
    try:
        raw = environ["wsgi.input"].read(size)
    except Exception:
        raw = None
    if not isinstance(raw, bytes) or len(raw) != size:
        raise _blocked("body_invalid", 400)
    return raw


def _request(raw: bytes) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise _blocked("json_invalid", 400) from None
    if not isinstance(value, Mapping):
        raise _blocked("request_invalid", 400)
    return value


def _principal(value: Any, scope: str, *, tenant: bool) -> dict[str, str]:
    fields = {
        "schema", "principal_id", "credential_id", "credential_version", "scope",
    }
    if tenant:
        fields.add("site_id")
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _blocked("authentication_failed", 401)
    try:
        expected = TENANT_PRINCIPAL_SCHEMA if tenant else OPERATOR_PRINCIPAL_SCHEMA
        if value["scope"] != scope:
            raise _blocked("scope_rejected", 403)
        if value["schema"] != expected:
            raise ValueError
        for name in fields - {"schema"}:
            _token(value[name])
    except CMSSourceDeliverySubmissionDispatchHTTPBlocked:
        raise
    except Exception:
        raise _blocked("authentication_failed", 401) from None
    return dict(value)


def _status_payload(
    value: Any,
    *,
    operation: str | None = None,
    request_id: str | None = None,
    event_id: str | None = None,
    site_id: str | None = None,
    payload_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        payload = dataclasses.asdict(value)
        fields = {
            "operation", "request_id", "event_id", "site_id", "payload_sha256",
            "source_max_attempts", "delivery_max_attempts", "status", "attempts",
            "client_max_attempts", "next_attempt_at", "lease_expires_at",
            "lease_expired", "last_error_code", "remote_status",
            "remote_attempts", "remote_capabilities_sha256",
            "remote_binding_sha256", "response_sha256",
        }
        if set(payload) != fields or payload["operation"] not in OPERATIONS:
            raise ValueError
        for name in ("request_id", "event_id", "site_id"):
            _token(payload[name])
        _sha256(payload["payload_sha256"])
        for name in (
            "source_max_attempts", "delivery_max_attempts",
            "client_max_attempts",
        ):
            _count(payload[name], minimum=1)
        _count(payload["attempts"])
        _timestamp(payload["next_attempt_at"])
        lease = _optional_timestamp(payload["lease_expires_at"])
        if not isinstance(payload["lease_expired"], bool):
            raise ValueError
        error = _optional(
            payload["last_error_code"],
            lambda item: item if ERROR_CODE.fullmatch(item) else (_ for _ in ()).throw(ValueError()),
        )
        remote_status = payload["remote_status"]
        if remote_status not in {None, "pending", "leased", "retry_wait", "succeeded", "failed"}:
            raise ValueError
        remote_attempts = _optional(
            payload["remote_attempts"], lambda item: _count(item),
        )
        for name in (
            "remote_capabilities_sha256", "remote_binding_sha256",
            "response_sha256",
        ):
            _optional(payload[name], _sha256)
        if payload["status"] not in STATUSES:
            raise ValueError
        if (payload["status"] == "leased") != (lease is not None):
            raise ValueError
        accepted = payload["status"] == "accepted"
        remote_fields = (
            remote_status, remote_attempts,
            payload["remote_capabilities_sha256"],
            payload["remote_binding_sha256"], payload["response_sha256"],
        )
        if accepted != all(item is not None for item in remote_fields):
            raise ValueError
        expected = {
            "operation": operation, "request_id": request_id,
            "event_id": event_id, "site_id": site_id,
            "payload_sha256": payload_sha256,
        }
        if any(
            wanted is not None and payload[name] != wanted
            for name, wanted in expected.items()
        ):
            raise ValueError
        payload["last_error_code"] = error
        return payload
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _health_payload(value: Any) -> dict[str, Any]:
    try:
        payload = dataclasses.asdict(value)
        if set(payload) != {
            "status", "counts", "operations", "due", "expired_leases",
            "failed", "expected_capabilities_sha256",
        }:
            raise ValueError
        if payload["status"] not in {"ok", "blocked"}:
            raise ValueError
        if set(payload["counts"]) != STATUSES or set(payload["operations"]) != OPERATIONS:
            raise ValueError
        counts = {name: _count(payload["counts"][name], maximum=1_000_000) for name in STATUSES}
        operations = {
            name: _count(payload["operations"][name], maximum=1_000_000)
            for name in OPERATIONS
        }
        due = _count(payload["due"], maximum=1_000_000)
        expired = _count(payload["expired_leases"], maximum=1_000_000)
        failed = _count(payload["failed"], maximum=1_000_000)
        digest = _sha256(payload["expected_capabilities_sha256"])
        if (
            sum(counts.values()) != sum(operations.values())
            or due > counts["pending"] + counts["retry_wait"]
            or expired > counts["leased"]
            or failed != counts["failed"]
            or (payload["status"] == "ok") != (expired == 0 and failed == 0)
        ):
            raise ValueError
        return {
            "status": payload["status"], "counts": counts,
            "operations": operations, "due": due,
            "expired_leases": expired, "failed": failed,
            "expected_capabilities_sha256": digest,
        }
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _readiness_payload(value: Any, expected_digest: str) -> dict[str, Any]:
    try:
        fields = {
            "schema", "status", "worker_state", "outbox_status",
            "error_code", "capabilities_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError
        if value["schema"] != "blun.cms-public-submission-worker-readiness.v1":
            raise ValueError
        if value["status"] not in {"ready", "not_ready"}:
            raise ValueError
        if value["worker_state"] not in WORKER_STATES:
            raise ValueError
        if value["outbox_status"] not in {None, "ok", "blocked"}:
            raise ValueError
        error = _optional(
            value["error_code"],
            lambda item: item if ERROR_CODE.fullmatch(item) else (_ for _ in ()).throw(ValueError()),
        )
        if _sha256(value["capabilities_sha256"]) != expected_digest:
            raise ValueError
        ready = (
            value["worker_state"] == "running"
            and value["outbox_status"] == "ok"
            and error is None
        )
        if ready != (value["status"] == "ready"):
            raise ValueError
        if not ready and error is None:
            raise ValueError
        return dict(value)
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _capabilities_payload(runtime_digest: str) -> dict[str, Any]:
    definitions = (
        ("capabilities", CAPABILITIES_PATH, None, CAPABILITIES_RESPONSE_SCHEMA, 200),
        ("enqueue", ENQUEUE_PATH, ENQUEUE_REQUEST_SCHEMA, QUEUE_RESPONSE_SCHEMA, 202),
        ("health", HEALTH_PATH, None, HEALTH_RESPONSE_SCHEMA, 200),
        ("openapi", OPENAPI_PATH, None, OPENAPI_RESPONSE_SCHEMA, 200),
        ("readiness", READINESS_PATH, None, READINESS_RESPONSE_SCHEMA, 200),
        ("status", STATUS_PATH, STATUS_REQUEST_SCHEMA, STATUS_RESPONSE_SCHEMA, 200),
    )
    try:
        operations = {}
        for name, path, request_schema, response_schema, status in definitions:
            operations[name] = {
                "method": METHODS[path], "path": path, "scope": SCOPES[path],
                "principal_schema": (
                    TENANT_PRINCIPAL_SCHEMA if path in TENANT_PATHS
                    else OPERATOR_PRINCIPAL_SCHEMA
                ),
                "request_schema": request_schema,
                "response_schema": response_schema,
                "success_status": status,
                "error_statuses": list(ERROR_STATUSES[path]),
            }
        contract = {
            "schema": CAPABILITIES_SCHEMA,
            "api_schema": API_SCHEMA,
            "operations": operations,
            "limits": {
                "max_body_bytes": MAX_BODY_BYTES,
                "max_headers": MAX_HEADERS,
                "max_header_value_bytes": MAX_HEADER_VALUE,
                "max_attempts_per_stage": 20,
            },
            "retry_ownership": {
                "client_max_attempts": "cms_to_website",
                "delivery_max_attempts": "website_to_sidecar",
                "source_max_attempts": "source_processing",
            },
            "source_payload_schemas": {
                "change": _DISPATCH._CLIENT._CMS.CHANGE_SCHEMA,
                "cancellation": _DISPATCH._CLIENT._CMS.CANCELLATION_SCHEMA,
                "tombstone": _DISPATCH._CLIENT._CMS.TOMBSTONE_SCHEMA,
            },
            "openapi_document_schema": _OPENAPI.DOCUMENT_SCHEMA,
            "semantics": {
                "authentication_precedes_json_parsing": True,
                "authentication_binds_exact_body_sha256": True,
                "tenant_operations_are_site_bound": True,
                "write_requires_ready_managed_worker": True,
                "accepted_means_website_intake_only": True,
                "accepted_implies_publication": False,
                "operational_responses_are_content_free": True,
            },
            "public_submission_capabilities_sha256": _sha256(runtime_digest),
        }
        raw = _canonical(contract, response=True)
        return {**contract, "sha256": hashlib.sha256(raw).hexdigest()}
    except CMSSourceDeliverySubmissionDispatchHTTPBlocked:
        raise
    except Exception:
        raise _blocked("capabilities_invalid", 503) from None


class CMSSourceDeliverySubmissionDispatchHTTPApplication:
    """Strict WSGI boundary over one supervised caller-side runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if not all(callable(getattr(runtime, name, None)) for name in (
            "enqueue", "status", "health", "worker_readiness",
        )):
            raise TypeError("runtime must provide submission dispatch operations")
        if not callable(authenticator):
            raise TypeError("authenticator must be callable")
        self.runtime = runtime
        self.authenticator = authenticator

    def _authenticate(
        self, environ: Mapping[str, Any], path: str, raw: bytes,
    ) -> dict[str, str]:
        request = {
            "schema": AUTH_REQUEST_SCHEMA,
            "method": environ["REQUEST_METHOD"],
            "path": path,
            "headers": [list(item) for item in _headers(environ)],
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        }
        try:
            value = self.authenticator(request)
        except Exception:
            raise _blocked("authentication_unavailable", 503) from None
        return _principal(value, SCOPES[path], tenant=path in TENANT_PATHS)

    @staticmethod
    def _send(start_response, status: int, payload: Mapping[str, Any]):
        raw = _canonical(dict(payload), response=True)
        phrases = {
            200: "OK", 202: "Accepted", 400: "Bad Request",
            401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 409: "Conflict",
            411: "Length Required", 413: "Content Too Large",
            415: "Unsupported Media Type", 503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return [raw]

    @classmethod
    def _error(cls, start_response, error):
        return cls._send(start_response, error.status, {
            "schema": ERROR_SCHEMA, "status": "BLOCK", "error_code": error.code,
        })

    def __call__(self, environ: Mapping[str, Any], start_response):
        try:
            if not isinstance(environ, Mapping):
                raise _blocked("environment_invalid", 400)
            path = environ.get("PATH_INFO")
            if path not in SCOPES:
                raise _blocked("route_not_found", 404)
            if environ.get("REQUEST_METHOD") != METHODS[path]:
                raise _blocked("method_not_allowed", 405)
            if environ.get("wsgi.url_scheme") != "https":
                raise _blocked("https_required", 400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise _blocked("query_rejected", 400)
            bodyless = path in BODYLESS_PATHS
            if bodyless:
                raw = _body(environ, required=False)
                if raw or environ.get("CONTENT_TYPE") not in {None, ""}:
                    raise _blocked("body_not_allowed", 400)
            else:
                content_type = environ.get("CONTENT_TYPE")
                parts = (
                    [item.strip().lower() for item in content_type.split(";")]
                    if isinstance(content_type, str) else []
                )
                if parts not in (
                    ["application/json"],
                    ["application/json", "charset=utf-8"],
                ):
                    raise _blocked("content_type_invalid", 415)
                raw = _body(environ, required=True)
            principal = self._authenticate(environ, path, raw)
            runtime_digest = self.runtime.expected_capabilities_sha256
            capabilities = _capabilities_payload(runtime_digest)
            if path == CAPABILITIES_PATH:
                return self._send(start_response, 200, {
                    "schema": CAPABILITIES_RESPONSE_SCHEMA,
                    "capabilities": capabilities,
                })
            if path == OPENAPI_PATH:
                document = _OPENAPI.build_document(capabilities)
                return self._send(start_response, 200, {
                    "schema": OPENAPI_RESPONSE_SCHEMA,
                    "openapi": document,
                    "openapi_sha256": _OPENAPI.document_sha256(document),
                    "capabilities_sha256": capabilities["sha256"],
                })
            if path == HEALTH_PATH:
                health = _health_payload(self.runtime.health())
                if health["expected_capabilities_sha256"] != runtime_digest:
                    raise _blocked("runtime_response_invalid", 503)
                return self._send(start_response, 200 if health["status"] == "ok" else 503, {
                    "schema": HEALTH_RESPONSE_SCHEMA, "health": health,
                    "capabilities_sha256": capabilities["sha256"],
                })
            if path == READINESS_PATH:
                readiness = _readiness_payload(
                    self.runtime.worker_readiness(), runtime_digest,
                )
                return self._send(
                    start_response, 200 if readiness["status"] == "ready" else 503,
                    {
                        "schema": READINESS_RESPONSE_SCHEMA,
                        "readiness": readiness,
                        "capabilities_sha256": capabilities["sha256"],
                    },
                )
            if path == ENQUEUE_PATH:
                readiness = _readiness_payload(
                    self.runtime.worker_readiness(), runtime_digest,
                )
                if readiness["status"] != "ready":
                    raise _blocked("runtime_not_ready", 503)
            request = _request(raw)
            if path == ENQUEUE_PATH:
                if set(request) != {
                    "schema", "payload", "source_max_attempts",
                    "delivery_max_attempts", "client_max_attempts",
                } or request.get("schema") != ENQUEUE_REQUEST_SCHEMA:
                    raise _blocked("request_invalid", 400)
                try:
                    _copied, identity, payload_json = _DISPATCH._payload(
                        request["payload"]
                    )
                    source_max = _count(request["source_max_attempts"], minimum=1)
                    delivery_max = _count(
                        request["delivery_max_attempts"], minimum=1,
                    )
                    client_max = _count(request["client_max_attempts"], minimum=1)
                except Exception:
                    raise _blocked("request_invalid", 400) from None
                payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                if (
                    identity["site_id"] != principal["site_id"]
                    or environ.get("HTTP_IDEMPOTENCY_KEY")
                    != identity["request_id"]
                    or environ.get("HTTP_X_LOCALIZATION_SOURCE_PAYLOAD_SHA256")
                    != payload_hash
                ):
                    raise _blocked("binding_invalid", 400)
                try:
                    status = self.runtime.enqueue(
                        request["payload"],
                        source_max_attempts=source_max,
                        delivery_max_attempts=delivery_max,
                        client_max_attempts=client_max,
                    )
                except Exception as error:
                    if getattr(error, "code", "").endswith(
                        "idempotency_collision"
                    ):
                        raise _blocked("idempotency_collision", 409) from None
                    raise _blocked("runtime_blocked", 503) from error
                normalized = _status_payload(
                    status, operation=identity["operation"],
                    request_id=identity["request_id"],
                    event_id=identity["event_id"], site_id=identity["site_id"],
                    payload_sha256=identity["payload_sha256"],
                )
                return self._send(start_response, 202, {
                    "schema": QUEUE_RESPONSE_SCHEMA,
                    "api_schema": API_SCHEMA,
                    "status": normalized,
                    "capabilities_sha256": capabilities["sha256"],
                    "accepted_implies_publication": False,
                })
            if set(request) != {
                "schema", "operation", "request_id", "event_id",
                "site_id", "payload_sha256",
            } or request.get("schema") != STATUS_REQUEST_SCHEMA:
                raise _blocked("request_invalid", 400)
            try:
                operation = request["operation"]
                if operation not in OPERATIONS:
                    raise ValueError
                request_id = _token(request["request_id"])
                event_id = _token(request["event_id"])
                site_id = _token(request["site_id"])
                payload_hash = _sha256(request["payload_sha256"])
            except Exception:
                raise _blocked("request_invalid", 400) from None
            if site_id != principal["site_id"]:
                raise _blocked("submission_not_found", 404)
            try:
                status = self.runtime.status(operation, request_id)
            except Exception as error:
                if getattr(error, "code", "").endswith("submission_missing"):
                    raise _blocked("submission_not_found", 404) from None
                raise _blocked("runtime_blocked", 503) from error
            try:
                normalized = _status_payload(
                    status, operation=operation, request_id=request_id,
                    event_id=event_id, site_id=site_id,
                    payload_sha256=payload_hash,
                )
            except CMSSourceDeliverySubmissionDispatchHTTPBlocked:
                raise _blocked("submission_not_found", 404) from None
            return self._send(start_response, 200, {
                "schema": STATUS_RESPONSE_SCHEMA,
                "api_schema": API_SCHEMA,
                "status": normalized,
                "capabilities_sha256": capabilities["sha256"],
                "accepted_implies_publication": False,
            })
        except CMSSourceDeliverySubmissionDispatchHTTPBlocked as error:
            return self._error(start_response, error)
        except Exception:
            return self._error(start_response, _blocked("runtime_blocked", 503))


def build_submission_dispatch_http(runtime: Any, authenticator):
    return CMSSourceDeliverySubmissionDispatchHTTPApplication(
        runtime, authenticator,
    )
