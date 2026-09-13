#!/usr/bin/env python3
"""Authenticated public ingress and progress API for the website edge.

The WSGI boundary accepts one exact website change or removal per request.
Authentication sees request metadata and the body digest, while the principal
is bound to the payload site before any runtime or durable outbox access.
HTTP 202 means only that the website outbox durably accepted the request; it
never implies source acceptance, localization quality, approval, or publication.
Separate site-bound reads expose acceptance and source lifecycle without
collapsing either state into publication authority.
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


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load submission HTTP dependency")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_SUBMISSION = _load_module(
    "blun_website_localization_submission_http_runtime",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_runtime.py",
)
_CAPABILITIES = _load_module(
    "blun_website_localization_submission_http_capabilities",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_capabilities_http.py",
)

API_SCHEMA = "blun.cms-source-delivery-submission-http.v3"
ERROR_SCHEMA = "blun.cms-source-delivery-submission-http-error.v1"
AUTH_REQUEST_SCHEMA = _SUBMISSION.WRITE_HTTP_AUTH_REQUEST_SCHEMA
PRINCIPAL_SCHEMA = _SUBMISSION.WRITE_HTTP_PRINCIPAL_SCHEMA
READ_AUTH_REQUEST_SCHEMA = _SUBMISSION.READ_HTTP_AUTH_REQUEST_SCHEMA
READ_PRINCIPAL_SCHEMA = _SUBMISSION.READ_HTTP_PRINCIPAL_SCHEMA
CHANGE_REQUEST_SCHEMA = _SUBMISSION.CHANGE_HTTP_REQUEST_SCHEMA
REMOVAL_REQUEST_SCHEMA = _SUBMISSION.REMOVAL_HTTP_REQUEST_SCHEMA
CHANGE_RESPONSE_SCHEMA = _SUBMISSION.CHANGE_HTTP_RESPONSE_SCHEMA
REMOVAL_RESPONSE_SCHEMA = _SUBMISSION.REMOVAL_HTTP_RESPONSE_SCHEMA
CHANGE_PATH = _SUBMISSION.CHANGE_HTTP_PATH
REMOVAL_PATH = _SUBMISSION.REMOVAL_HTTP_PATH
STATUS_PATH = _SUBMISSION.STATUS_HTTP_PATH
LIFECYCLE_PATH = _SUBMISSION.LIFECYCLE_HTTP_PATH
PIPELINE_HEALTH_PATH = _SUBMISSION.PIPELINE_HEALTH_HTTP_PATH
PIPELINE_READINESS_PATH = _SUBMISSION.PIPELINE_READINESS_HTTP_PATH
CAPABILITIES_PATH = _SUBMISSION.CAPABILITIES_HTTP_PATH
CHANGE_SCOPE = _SUBMISSION.CHANGE_HTTP_SCOPE
REMOVAL_SCOPE = _SUBMISSION.REMOVAL_HTTP_SCOPE
STATUS_SCOPE = _SUBMISSION.STATUS_HTTP_SCOPE
LIFECYCLE_SCOPE = _SUBMISSION.LIFECYCLE_HTTP_SCOPE
PIPELINE_HEALTH_SCOPE = _SUBMISSION.PIPELINE_HEALTH_HTTP_SCOPE
PIPELINE_READINESS_SCOPE = _SUBMISSION.PIPELINE_READINESS_HTTP_SCOPE
OPERATOR_AUTH_REQUEST_SCHEMA = _SUBMISSION.OPERATOR_HTTP_AUTH_REQUEST_SCHEMA
OPERATOR_PRINCIPAL_SCHEMA = _SUBMISSION.OPERATOR_HTTP_PRINCIPAL_SCHEMA

MAX_BODY_BYTES = 4_000_000
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
STATUSES = {"pending", "leased", "retry_wait", "succeeded", "failed"}
SCOPES = {
    CHANGE_PATH: CHANGE_SCOPE,
    REMOVAL_PATH: REMOVAL_SCOPE,
    STATUS_PATH: STATUS_SCOPE,
    LIFECYCLE_PATH: LIFECYCLE_SCOPE,
}
REQUEST_SCHEMAS = {
    CHANGE_PATH: CHANGE_REQUEST_SCHEMA,
    REMOVAL_PATH: REMOVAL_REQUEST_SCHEMA,
    STATUS_PATH: _SUBMISSION.STATUS_HTTP_REQUEST_SCHEMA,
    LIFECYCLE_PATH: _SUBMISSION.LIFECYCLE_HTTP_REQUEST_SCHEMA,
}
RESPONSE_SCHEMAS = {
    CHANGE_PATH: CHANGE_RESPONSE_SCHEMA,
    REMOVAL_PATH: REMOVAL_RESPONSE_SCHEMA,
    STATUS_PATH: _SUBMISSION.STATUS_HTTP_RESPONSE_SCHEMA,
    LIFECYCLE_PATH: _SUBMISSION.LIFECYCLE_HTTP_RESPONSE_SCHEMA,
}
READ_PATHS = {STATUS_PATH, LIFECYCLE_PATH}
MONITOR_PATHS = {PIPELINE_HEALTH_PATH, PIPELINE_READINESS_PATH}
MONITOR_SCOPES = {
    PIPELINE_HEALTH_PATH: PIPELINE_HEALTH_SCOPE,
    PIPELINE_READINESS_PATH: PIPELINE_READINESS_SCOPE,
}
MONITOR_RESPONSE_SCHEMAS = {
    PIPELINE_HEALTH_PATH: _SUBMISSION.PIPELINE_HEALTH_HTTP_RESPONSE_SCHEMA,
    PIPELINE_READINESS_PATH: (
        _SUBMISSION.PIPELINE_READINESS_HTTP_RESPONSE_SCHEMA
    ),
}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class SubmissionHTTPBlocked(RuntimeError):
    """Stable, content-free failure at the public website boundary."""

    def __init__(self, code: str, status: int):
        if ERROR_CODE.fullmatch(code) is None or status not in {
            400, 401, 403, 404, 405, 409, 411, 413, 415, 503,
        }:
            raise ValueError("invalid submission HTTP failure")
        super().__init__(code)
        self.code = code
        self.status = status


def _blocked(code: str, status: int) -> SubmissionHTTPBlocked:
    return SubmissionHTTPBlocked("submission_http." + code, status)


def _canonical(value: Any, *, response: bool = False) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise _blocked(
            "response_invalid" if response else "request_invalid",
            503 if response else 400,
        ) from None
    limit = 1_000_000 if response else MAX_BODY_BYTES
    if not raw or len(raw) > limit:
        raise _blocked(
            "response_invalid" if response else "request_invalid",
            503 if response else 400,
        )
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


def _content_type(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = [item.strip().lower() for item in value.split(";")]
    return parts in (["application/json"], ["application/json", "charset=utf-8"])


def _body(environ: Mapping[str, Any]) -> bytes:
    if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
        raise _blocked("transfer_encoding_rejected", 400)
    length = environ.get("CONTENT_LENGTH")
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
    stream = environ.get("wsgi.input")
    try:
        body = stream.read(size)
    except Exception:
        body = None
    if not isinstance(body, bytes) or len(body) != size:
        raise _blocked("body_invalid", 400)
    return body


def _request(body: bytes) -> Mapping[str, Any]:
    try:
        text = body.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise _blocked("json_invalid", 400) from None
    if not isinstance(value, Mapping):
        raise _blocked("request_invalid", 400)
    return value


def _principal(
    value: Any, scope: str, *, schema: str = PRINCIPAL_SCHEMA,
) -> dict[str, str]:
    fields = {
        "schema", "principal_id", "credential_id", "credential_version",
        "scope", "site_id",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _blocked("authentication_failed", 401)
    try:
        if value["schema"] != schema:
            raise ValueError
        for name in fields - {"schema"}:
            _token(value[name])
    except (KeyError, TypeError, ValueError):
        raise _blocked("authentication_failed", 401) from None
    if value["scope"] != scope:
        raise _blocked("scope_rejected", 403)
    return dict(value)


def _operator_principal(value: Any, scope: str) -> dict[str, str]:
    fields = {
        "schema", "principal_id", "credential_id", "credential_version",
        "scope",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _blocked("authentication_failed", 401)
    try:
        if value["schema"] != OPERATOR_PRINCIPAL_SCHEMA:
            raise ValueError
        for name in fields - {"schema"}:
            _token(value[name])
    except (KeyError, TypeError, ValueError):
        raise _blocked("authentication_failed", 401) from None
    if value["scope"] != scope:
        raise _blocked("scope_rejected", 403)
    return dict(value)


def _attempts(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise _blocked("request_invalid", 400)
    return value


def _identity(payload: Any, *, change: bool) -> tuple[str, str, str, str]:
    if not isinstance(payload, Mapping):
        raise _blocked("request_invalid", 400)
    schema = payload.get("schema")
    event_id = payload.get("event_id")
    site_id = payload.get("site_id")
    if change and schema == _SUBMISSION._ADAPTER.CHANGE_SCHEMA:
        operation, request_id = "change", event_id
    elif not change and schema == _SUBMISSION._ADAPTER.CANCELLATION_SCHEMA:
        operation, request_id = "cancellation", payload.get("cancellation_id")
    elif not change and schema == _SUBMISSION._ADAPTER.TOMBSTONE_SCHEMA:
        operation, request_id = "tombstone", payload.get("tombstone_id")
    else:
        raise _blocked("request_invalid", 400)
    try:
        for value in (request_id, event_id, site_id):
            _token(value)
    except ValueError:
        raise _blocked("request_invalid", 400) from None
    return operation, request_id, event_id, site_id


def _binding(value: Any) -> dict[str, Any]:
    try:
        return _CAPABILITIES._binding(value)
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _status(
    value: Any,
    *,
    operation: str,
    request_id: str,
    event_id: str,
    site_id: str,
    payload_sha256: str,
    source_max_attempts: int,
    delivery_max_attempts: int,
    capabilities_sha256: str,
) -> dict[str, Any]:
    try:
        fields = {
            "operation", "request_id", "event_id", "site_id",
            "payload_sha256", "capabilities_sha256", "status", "attempts",
            "delivery_max_attempts", "source_max_attempts", "next_attempt_at",
            "lease_expires_at", "lease_expired", "last_error_code",
            "response_sha256",
        }
        payload = dataclasses.asdict(value)
        if set(payload) != fields:
            raise ValueError
        expected = {
            "operation": operation,
            "request_id": request_id,
            "event_id": event_id,
            "site_id": site_id,
            "payload_sha256": payload_sha256,
            "capabilities_sha256": capabilities_sha256,
            "delivery_max_attempts": delivery_max_attempts,
            "source_max_attempts": source_max_attempts,
        }
        if any(payload[name] != expected_value for name, expected_value in expected.items()):
            raise ValueError
        if payload["status"] not in STATUSES:
            raise ValueError
        attempts = payload["attempts"]
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 0 <= attempts <= delivery_max_attempts
            or not isinstance(payload["lease_expired"], bool)
        ):
            raise ValueError
        for name in ("next_attempt_at",):
            number = payload[name]
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
                or float(number) < 0
            ):
                raise ValueError
        lease = payload["lease_expires_at"]
        if lease is not None and (
            isinstance(lease, bool)
            or not isinstance(lease, (int, float))
            or not math.isfinite(float(lease))
            or float(lease) < 0
        ):
            raise ValueError
        if (payload["status"] == "leased") != (lease is not None):
            raise ValueError
        error = payload["last_error_code"]
        if error is not None and (
            not isinstance(error, str)
            or ERROR_CODE.fullmatch(error) is None
        ):
            raise ValueError
        if payload["status"] in {"retry_wait", "failed"} and error is None:
            raise ValueError
        response_hash = payload["response_sha256"]
        if response_hash is not None:
            _sha256(response_hash)
        if (payload["status"] == "succeeded") != (response_hash is not None):
            raise ValueError
        return {
            "operation": operation,
            "request_id": request_id,
            "event_id": event_id,
            "site_id": site_id,
            "payload_sha256": payload_sha256,
            "status": payload["status"],
            "attempts": attempts,
            "delivery_max_attempts": delivery_max_attempts,
            "source_max_attempts": source_max_attempts,
            "capabilities_sha256": capabilities_sha256,
        }
    except SubmissionHTTPBlocked:
        raise
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


class _PayloadView:
    def __init__(self, value: Mapping[str, Any]):
        self.value = value

    def as_payload(self) -> Mapping[str, Any]:
        return self.value


def _read_identity(value: Any, *, schema: str) -> dict[str, str]:
    fields = {
        "schema", "operation", "request_id", "event_id", "site_id",
        "payload_sha256",
    }
    try:
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError
        if value["schema"] != schema:
            raise ValueError
        result = {name: _token(value[name]) for name in (
            "operation", "request_id", "event_id", "site_id",
        )}
        if result["operation"] not in {"change", "cancellation", "tombstone"}:
            raise ValueError
        if result["operation"] == "change" and result["request_id"] != result["event_id"]:
            raise ValueError
        result["payload_sha256"] = _sha256(value["payload_sha256"])
        return result
    except Exception:
        raise _blocked("request_invalid", 400) from None


def _submission_status_payload(
    value: Any, identity: Mapping[str, str],
) -> dict[str, Any]:
    fields = {
        "schema", "operation", "request_id", "event_id", "site_id",
        "payload_sha256", "status", "stage", "website_status",
        "website_attempts", "website_delivery_max_attempts",
        "sidecar_status", "sidecar_attempts",
        "sidecar_delivery_max_attempts", "source_max_attempts",
        "next_attempt_at", "lease_expired", "error_code",
        "website_capability_binding",
    }
    try:
        payload = value.as_payload()
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError
        if payload["schema"] != _SUBMISSION.STATUS_SCHEMA:
            raise ValueError
        if payload.get("site_id") != identity["site_id"]:
            raise _blocked("site_not_found", 404)
        for name in ("operation", "request_id", "event_id", "payload_sha256"):
            if payload.get(name) != identity[name]:
                raise ValueError
        if payload["status"] not in {"pending", "accepted", "failed"}:
            raise ValueError
        if payload["stage"] not in {
            "website_acceptance", "sidecar_delivery", "source_acceptance",
        }:
            raise ValueError
        website_max = payload["website_delivery_max_attempts"]
        sidecar_max = payload["sidecar_delivery_max_attempts"]
        source_max = payload["source_max_attempts"]
        for maximum in (website_max, sidecar_max, source_max):
            if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 20:
                raise ValueError
        website_attempts = payload["website_attempts"]
        if (
            isinstance(website_attempts, bool)
            or not isinstance(website_attempts, int)
            or not 0 <= website_attempts <= website_max
            or payload["website_status"] not in STATUSES
        ):
            raise ValueError
        sidecar_attempts = payload["sidecar_attempts"]
        sidecar_status = payload["sidecar_status"]
        if (sidecar_status is None) != (sidecar_attempts is None):
            raise ValueError
        if sidecar_status is not None and (
            sidecar_status not in STATUSES
            or isinstance(sidecar_attempts, bool)
            or not isinstance(sidecar_attempts, int)
            or not 0 <= sidecar_attempts <= sidecar_max
        ):
            raise ValueError
        next_attempt = payload["next_attempt_at"]
        if (
            isinstance(next_attempt, bool)
            or not isinstance(next_attempt, (int, float))
            or not math.isfinite(float(next_attempt))
            or float(next_attempt) < 0
            or not isinstance(payload["lease_expired"], bool)
        ):
            raise ValueError
        error = payload["error_code"]
        if error is not None and (
            not isinstance(error, str) or ERROR_CODE.fullmatch(error) is None
        ):
            raise ValueError
        if payload["stage"] == "website_acceptance":
            if (
                sidecar_status is not None
                or payload["website_status"] == "succeeded"
                or payload["status"]
                != ("failed" if payload["website_status"] == "failed" else "pending")
            ):
                raise ValueError
        elif payload["stage"] == "sidecar_delivery":
            if (
                payload["website_status"] != "succeeded"
                or sidecar_status in {None, "succeeded"}
                or payload["status"]
                != ("failed" if sidecar_status == "failed" else "pending")
            ):
                raise ValueError
        elif (
            payload["status"] != "accepted"
            or payload["website_status"] != "succeeded"
            or sidecar_status != "succeeded"
        ):
            raise ValueError
        result = dict(payload)
        result["website_capability_binding"] = _binding(
            payload["website_capability_binding"]
        )
        _canonical(result, response=True)
        return result
    except SubmissionHTTPBlocked:
        raise
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _submission_lifecycle_payload(
    value: Any, identity: Mapping[str, str],
) -> dict[str, Any]:
    fields = {
        "schema", "status", "stage", "submission", "source_status",
        "source_capability_binding", "website_capability_binding",
        "sidecar_capabilities_sha256", "source_capabilities_sha256",
    }
    try:
        payload = value.as_payload()
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError
        if payload["schema"] != _SUBMISSION.LIFECYCLE_SCHEMA:
            raise ValueError
        submission = _submission_status_payload(
            _PayloadView(payload["submission"]), identity,
        )
        website_binding = _binding(payload["website_capability_binding"])
        if website_binding != submission["website_capability_binding"]:
            raise ValueError
        sidecar_hash = _sha256(payload["sidecar_capabilities_sha256"])
        source_hash = _sha256(payload["source_capabilities_sha256"])
        source_status = payload["source_status"]
        source_binding = payload["source_capability_binding"]
        if source_status is None or source_binding is None:
            if source_status is not None or source_binding is not None:
                raise ValueError
            if (
                payload["status"] != submission["status"]
                or payload["stage"] != submission["stage"]
            ):
                raise ValueError
        else:
            if submission["status"] != "accepted":
                raise ValueError
            normalized = _SUBMISSION._AUTH._HTTP._SOURCE_HTTP._source_status_payload(
                _PayloadView(source_status),
                expected_event_id=identity["event_id"],
                expected_site_id=identity["site_id"],
            )
            if normalized != source_status:
                raise ValueError
            normalized_binding = (
                _SUBMISSION._AUTH._HTTP._SOURCE_HTTP._capability_binding_payload(
                    source_binding
                )
            )
            if normalized_binding != source_binding:
                raise ValueError
            if payload["stage"] not in {
                "source_processing", "localization_lifecycle",
            }:
                raise ValueError
        result = dict(payload)
        result.update({
            "submission": submission,
            "website_capability_binding": website_binding,
            "sidecar_capabilities_sha256": sidecar_hash,
            "source_capabilities_sha256": source_hash,
        })
        _canonical(result, response=True)
        return result
    except SubmissionHTTPBlocked:
        raise
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _submission_health_payload(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "status", "website_health", "sidecar_health",
        "sidecar_capabilities_sha256", "source_capabilities_sha256",
        "website_capability_binding",
    }
    try:
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError
        if value["schema"] != _SUBMISSION.HEALTH_SCHEMA:
            raise ValueError
        website = _SUBMISSION._AUTH._HTTP._health_payload(
            _PayloadView(value["website_health"])
        )
        sidecar = _SUBMISSION._AUTH._HTTP._health_payload(
            _PayloadView(value["sidecar_health"])
        )
        if (
            website != value["website_health"]
            or sidecar != value["sidecar_health"]
        ):
            raise ValueError
        status = (
            "ok"
            if website["status"] == sidecar["status"] == "ok"
            else "blocked"
        )
        if value["status"] != status:
            raise ValueError
        binding = _binding(value["website_capability_binding"])
        sidecar_hash = _sha256(value["sidecar_capabilities_sha256"])
        source_hash = _sha256(value["source_capabilities_sha256"])
        result = dict(value)
        result.update({
            "website_health": website,
            "sidecar_health": sidecar,
            "website_capability_binding": binding,
            "sidecar_capabilities_sha256": sidecar_hash,
            "source_capabilities_sha256": source_hash,
        })
        return result
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _submission_readiness_payload(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "status", "website_status", "website_worker_state",
        "website_outbox_status", "website_error_code", "sidecar_status",
        "sidecar_worker_state", "sidecar_outbox_status", "sidecar_error_code",
        "sidecar_capabilities_sha256", "source_capabilities_sha256",
        "website_capability_binding",
    }
    try:
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError
        if value["schema"] != _SUBMISSION.READINESS_SCHEMA:
            raise ValueError
        website = _SUBMISSION._AUTH._HTTP._readiness_payload({
            "schema": "blun.cms-source-delivery-worker-readiness.v1",
            "status": value["website_status"],
            "worker_state": value["website_worker_state"],
            "outbox_status": value["website_outbox_status"],
            "error_code": value["website_error_code"],
        })
        sidecar_values = (
            value["sidecar_status"], value["sidecar_worker_state"],
            value["sidecar_outbox_status"], value["sidecar_error_code"],
        )
        if all(item is None for item in sidecar_values):
            sidecar = None
        elif any(item is None for item in sidecar_values[:2]):
            raise ValueError
        else:
            sidecar = _SUBMISSION._AUTH._HTTP._readiness_payload({
                "schema": "blun.cms-source-delivery-worker-readiness.v1",
                "status": sidecar_values[0],
                "worker_state": sidecar_values[1],
                "outbox_status": sidecar_values[2],
                "error_code": sidecar_values[3],
            })
        ready = (
            website["status"] == "ready"
            and sidecar is not None
            and sidecar["status"] == "ready"
        )
        if value["status"] != ("ready" if ready else "not_ready"):
            raise ValueError
        if website["status"] != "ready" and sidecar is not None:
            raise ValueError
        binding = _binding(value["website_capability_binding"])
        sidecar_hash = _sha256(value["sidecar_capabilities_sha256"])
        source_hash = _sha256(value["source_capabilities_sha256"])
        result = dict(value)
        result.update({
            "website_capability_binding": binding,
            "sidecar_capabilities_sha256": sidecar_hash,
            "source_capabilities_sha256": source_hash,
        })
        return result
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _validate_source_health_semantics(value: Mapping[str, Any]) -> None:
    components = [value[name] for name in ("changes", "removals", "lifecycle")]
    for name in ("notifications", "terminal_processing"):
        if value[name]:
            components.append(value[name])
    for component in components:
        counts = component["counts"]
        blockers = (
            component["failed"]
            + component["expired_leases"]
            + component.get("remote_failures", 0)
        )
        if (
            component["failed"] != counts["failed"]
            or component["due"]
            > (
                counts.get("pending", 0)
                + counts.get("watching", 0)
                + counts.get("retry_wait", 0)
            )
            or component["expired_leases"] > counts.get("leased", 0)
            or (component["status"] == "ok")
            != (blockers == 0)
        ):
            raise ValueError
        if "operations" in component and (
            sum(component["operations"].values()) != sum(counts.values())
        ):
            raise ValueError
    blocked = any(item["status"] == "blocked" for item in components)
    incomplete = (
        value["pending_lifecycle_registrations"]
        or value["pending_terminal_notifications"]
        or value["pending_terminal_processing"]
    )
    expected_status = (
        "blocked" if blocked else "degraded" if incomplete else "ok"
    )
    if value["status"] != expected_status:
        raise ValueError
    expected_error = (
        "source_service.component_blocked" if blocked
        else "source_service.lifecycle_registration_pending"
        if value["pending_lifecycle_registrations"]
        else "source_service.notification_registration_pending"
        if value["pending_terminal_notifications"]
        else "source_service.processing_registration_pending"
        if value["pending_terminal_processing"]
        else None
    )
    if value["error_code"] != expected_error:
        raise ValueError


def _pipeline_health_payload(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "status", "intake_health", "source_health",
        "source_capability_binding", "website_capability_binding",
        "sidecar_capabilities_sha256", "source_capabilities_sha256",
    }
    try:
        payload = value.as_payload()
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError
        if payload["schema"] != _SUBMISSION.PIPELINE_HEALTH_SCHEMA:
            raise ValueError
        intake = _submission_health_payload(payload["intake_health"])
        binding = _binding(payload["website_capability_binding"])
        sidecar_hash = _sha256(payload["sidecar_capabilities_sha256"])
        source_hash = _sha256(payload["source_capabilities_sha256"])
        if (
            binding != intake["website_capability_binding"]
            or sidecar_hash != intake["sidecar_capabilities_sha256"]
            or source_hash != intake["source_capabilities_sha256"]
        ):
            raise ValueError
        if intake["status"] != "ok":
            if (
                payload["status"] != "blocked"
                or payload["source_health"] is not None
                or payload["source_capability_binding"] is not None
            ):
                raise ValueError
            source_health = source_binding = None
        else:
            normalized = _SUBMISSION._AUTH._HTTP._source_health_response({
                "schema": (
                    _SUBMISSION._AUTH._HTTP._SOURCE_HTTP.HEALTH_RESPONSE_SCHEMA
                ),
                "health": payload["source_health"],
                "capability_binding": payload["source_capability_binding"],
                "capabilities_sha256": source_hash,
            }, expected_capabilities_sha256=source_hash)
            source_health = normalized["health"]
            source_binding = normalized["capability_binding"]
            _validate_source_health_semantics(source_health)
            if (
                source_health != payload["source_health"]
                or source_binding != payload["source_capability_binding"]
                or payload["status"] != source_health["status"]
            ):
                raise ValueError
        result = dict(payload)
        result.update({
            "intake_health": intake,
            "source_health": source_health,
            "source_capability_binding": source_binding,
            "website_capability_binding": binding,
            "sidecar_capabilities_sha256": sidecar_hash,
            "source_capabilities_sha256": source_hash,
        })
        _canonical(result, response=True)
        return result
    except SubmissionHTTPBlocked:
        raise
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


def _pipeline_readiness_payload(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "status", "intake_readiness", "source_readiness",
        "source_capability_binding", "website_capability_binding",
        "sidecar_capabilities_sha256", "source_capabilities_sha256",
    }
    try:
        payload = value.as_payload()
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError
        if payload["schema"] != _SUBMISSION.PIPELINE_READINESS_SCHEMA:
            raise ValueError
        intake = _submission_readiness_payload(payload["intake_readiness"])
        binding = _binding(payload["website_capability_binding"])
        sidecar_hash = _sha256(payload["sidecar_capabilities_sha256"])
        source_hash = _sha256(payload["source_capabilities_sha256"])
        if (
            binding != intake["website_capability_binding"]
            or sidecar_hash != intake["sidecar_capabilities_sha256"]
            or source_hash != intake["source_capabilities_sha256"]
        ):
            raise ValueError
        if intake["status"] != "ready":
            if (
                payload["status"] != "not_ready"
                or payload["source_readiness"] is not None
                or payload["source_capability_binding"] is not None
            ):
                raise ValueError
            source_readiness = source_binding = None
        else:
            normalized = _SUBMISSION._AUTH._HTTP._source_readiness_response({
                "schema": (
                    _SUBMISSION._AUTH._HTTP._SOURCE_HTTP.READINESS_RESPONSE_SCHEMA
                ),
                "readiness": payload["source_readiness"],
                "capability_binding": payload["source_capability_binding"],
                "capabilities_sha256": source_hash,
            }, expected_capabilities_sha256=source_hash)
            source_readiness = normalized["readiness"]
            source_binding = normalized["capability_binding"]
            if (
                source_readiness != payload["source_readiness"]
                or source_binding != payload["source_capability_binding"]
                or payload["status"] != (
                    "ready"
                    if source_readiness["status"] == "ready"
                    else "not_ready"
                )
            ):
                raise ValueError
        result = dict(payload)
        result.update({
            "intake_readiness": intake,
            "source_readiness": source_readiness,
            "source_capability_binding": source_binding,
            "website_capability_binding": binding,
            "sidecar_capabilities_sha256": sidecar_hash,
            "source_capabilities_sha256": source_hash,
        })
        _canonical(result, response=True)
        return result
    except SubmissionHTTPBlocked:
        raise
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


class SubmissionHTTPApplication:
    """Strict public WSGI adapter over one hosted website submission runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if not all(callable(getattr(runtime, name, None)) for name in (
            "submission_capabilities", "website_capability_binding",
            "worker_readiness", "enqueue_change", "enqueue_removal",
            "submission_status", "submission_lifecycle",
            "submission_pipeline_health", "submission_pipeline_readiness",
        )):
            raise TypeError("runtime must provide website submission operations")
        if not callable(authenticator):
            raise TypeError("authenticator must be callable")
        self.runtime = runtime
        self.authenticator = authenticator
        self.capabilities = _CAPABILITIES.build_submission_capabilities_http(
            runtime, authenticator,
        )

    @staticmethod
    def _send(start_response, status: int, payload: Mapping[str, Any]):
        body = _canonical(dict(payload), response=True)
        phrases = {
            200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 411: "Length Required", 413: "Content Too Large",
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
    def _error(cls, start_response, failure: SubmissionHTTPBlocked):
        return cls._send(start_response, failure.status, {
            "schema": ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": failure.code,
        })

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        if isinstance(environ, Mapping) and environ.get("PATH_INFO") == CAPABILITIES_PATH:
            return self.capabilities(environ, start_response)
        try:
            if not isinstance(environ, Mapping):
                raise _blocked("environment_invalid", 400)
            path = environ.get("PATH_INFO")
            if path in MONITOR_PATHS:
                if environ.get("REQUEST_METHOD") != "GET":
                    raise _blocked("method_not_allowed", 405)
                if environ.get("wsgi.url_scheme") != "https":
                    raise _blocked("https_required", 400)
                if environ.get("QUERY_STRING") not in {None, ""}:
                    raise _blocked("query_rejected", 400)
                if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
                    raise _blocked("transfer_encoding_rejected", 400)
                if environ.get("CONTENT_LENGTH") not in {None, "", "0"}:
                    raise _blocked("body_not_allowed", 400)
                if environ.get("CONTENT_TYPE") not in {None, ""}:
                    raise _blocked("body_not_allowed", 400)
                auth_request = {
                    "schema": OPERATOR_AUTH_REQUEST_SCHEMA,
                    "method": "GET",
                    "path": path,
                    "headers": [list(item) for item in _headers(environ)],
                    "body_sha256": EMPTY_SHA256,
                }
                try:
                    principal = self.authenticator(auth_request)
                except Exception:
                    raise _blocked("authentication_unavailable", 503) from None
                _operator_principal(principal, MONITOR_SCOPES[path])
                try:
                    if path == PIPELINE_HEALTH_PATH:
                        result = _pipeline_health_payload(
                            self.runtime.submission_pipeline_health()
                        )
                    else:
                        result = _pipeline_readiness_payload(
                            self.runtime.submission_pipeline_readiness()
                        )
                except SubmissionHTTPBlocked:
                    raise
                except Exception:
                    raise _blocked("runtime_blocked", 503) from None
                return self._send(start_response, 200, {
                    "schema": MONITOR_RESPONSE_SCHEMAS[path],
                    "api_schema": API_SCHEMA,
                    "result": result,
                    "content_free": True,
                    "publication_authority": False,
                })
            if path not in SCOPES:
                raise _blocked("route_not_found", 404)
            if environ.get("REQUEST_METHOD") != "POST":
                raise _blocked("method_not_allowed", 405)
            if environ.get("wsgi.url_scheme") != "https":
                raise _blocked("https_required", 400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise _blocked("query_rejected", 400)
            if not _content_type(environ.get("CONTENT_TYPE")):
                raise _blocked("content_type_invalid", 415)
            body = _body(environ)
            headers = _headers(environ)
            auth_request = {
                "schema": (
                    READ_AUTH_REQUEST_SCHEMA
                    if path in READ_PATHS
                    else AUTH_REQUEST_SCHEMA
                ),
                "method": "POST",
                "path": path,
                "headers": [list(item) for item in headers],
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
            try:
                principal = _principal(
                    self.authenticator(auth_request),
                    SCOPES[path],
                    schema=(
                        READ_PRINCIPAL_SCHEMA
                        if path in READ_PATHS
                        else PRINCIPAL_SCHEMA
                    ),
                )
            except SubmissionHTTPBlocked:
                raise
            except Exception:
                raise _blocked("authentication_unavailable", 503) from None

            request = _request(body)
            if path in READ_PATHS:
                identity = _read_identity(
                    request, schema=REQUEST_SCHEMAS[path],
                )
                if identity["site_id"] != principal["site_id"]:
                    raise _blocked("site_not_found", 404)
                try:
                    if path == STATUS_PATH:
                        result = _submission_status_payload(
                            self.runtime.submission_status(
                                identity["operation"], identity["request_id"],
                            ),
                            identity,
                        )
                    else:
                        result = _submission_lifecycle_payload(
                            self.runtime.submission_lifecycle(
                                identity["operation"], identity["request_id"],
                            ),
                            identity,
                        )
                except SubmissionHTTPBlocked:
                    raise
                except Exception as error:
                    code = getattr(error, "code", "")
                    if isinstance(code, str) and code.endswith(".status_not_found"):
                        raise _blocked("submission_not_found", 404) from None
                    raise _blocked("runtime_blocked", 503) from None
                return self._send(start_response, 200, {
                    "schema": RESPONSE_SCHEMAS[path],
                    "api_schema": API_SCHEMA,
                    "result": result,
                    "accepted_implies_publication": False,
                })

            payload_key = "change" if path == CHANGE_PATH else "removal"
            if set(request) != {
                "schema", payload_key, "source_max_attempts",
                "delivery_max_attempts",
            } or request.get("schema") != REQUEST_SCHEMAS[path]:
                raise _blocked("request_invalid", 400)
            source_attempts = _attempts(request["source_max_attempts"])
            delivery_attempts = _attempts(request["delivery_max_attempts"])
            payload = request[payload_key]
            operation, request_id, event_id, site_id = _identity(
                payload, change=path == CHANGE_PATH,
            )
            if site_id != principal["site_id"]:
                raise _blocked("site_not_found", 404)
            header_map = dict(headers)
            if header_map.get("idempotency-key") != request_id:
                raise _blocked("idempotency_key_invalid", 400)
            payload_hash = hashlib.sha256(_canonical(dict(payload))).hexdigest()
            if header_map.get("x-localization-source-payload-sha256") != payload_hash:
                raise _blocked("payload_binding_invalid", 400)

            try:
                readiness = self.runtime.worker_readiness()
                if (
                    not isinstance(readiness, Mapping)
                    or readiness.get("status") != "ready"
                    or readiness.get("worker_state") != "running"
                ):
                    raise _blocked("runtime_not_ready", 503)
                binding = _binding(self.runtime.website_capability_binding())
                if binding["delivery_capabilities_sha256"] is None:
                    raise ValueError
                if path == CHANGE_PATH:
                    status = self.runtime.enqueue_change(
                        payload,
                        source_max_attempts=source_attempts,
                        delivery_max_attempts=delivery_attempts,
                    )
                else:
                    status = self.runtime.enqueue_removal(
                        payload,
                        source_max_attempts=source_attempts,
                        delivery_max_attempts=delivery_attempts,
                    )
            except SubmissionHTTPBlocked:
                raise
            except Exception as error:
                code = getattr(error, "code", None)
                if code == "source_delivery_runtime.request_invalid":
                    raise _blocked("request_invalid", 400) from None
                if code == "source_delivery_runtime.idempotency_collision":
                    raise _blocked("idempotency_collision", 409) from None
                raise _blocked("runtime_blocked", 503) from None

            projection = _status(
                status,
                operation=operation,
                request_id=request_id,
                event_id=event_id,
                site_id=site_id,
                payload_sha256=payload_hash,
                source_max_attempts=source_attempts,
                delivery_max_attempts=delivery_attempts,
                capabilities_sha256=binding["delivery_capabilities_sha256"],
            )
            return self._send(start_response, 202, {
                "schema": RESPONSE_SCHEMAS[path],
                "api_schema": API_SCHEMA,
                **projection,
                "website_capability_binding": binding,
                "accepted_implies_publication": False,
            })
        except SubmissionHTTPBlocked as failure:
            return self._error(start_response, failure)
        except Exception:
            return self._error(start_response, _blocked("internal", 503))


def build_submission_http(
    runtime: Any,
    authenticator: Callable[[dict[str, Any]], Any],
) -> SubmissionHTTPApplication:
    """Build the public capability, durable-write, and progress boundary."""

    return SubmissionHTTPApplication(runtime, authenticator)
