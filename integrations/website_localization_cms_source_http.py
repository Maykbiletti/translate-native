#!/usr/bin/env python3
"""Authenticated WSGI ingress for one durable source-CMS runtime.

The boundary accepts exact change and removal envelopes from the website host
and exposes content-free status, health, and capability data. Authentication
receives the request metadata and body digest, never the website payload itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import asdict
from typing import Any, Callable, Mapping


API_SCHEMA = "blun.cms-source-runtime-api.v1"
ERROR_SCHEMA = "blun.cms-source-runtime-http-error.v1"
AUTH_REQUEST_SCHEMA = "blun.cms-source-runtime-http-auth-request.v1"
PRINCIPAL_SCHEMA = "blun.cms-source-runtime-principal.v1"
STATUS_PRINCIPAL_SCHEMA = "blun.cms-source-status-principal.v1"
CHANGE_REQUEST_SCHEMA = "blun.cms-source-change-enqueue-request.v1"
CHANGE_RESPONSE_SCHEMA = "blun.cms-source-change-enqueue-response.v1"
REMOVAL_REQUEST_SCHEMA = "blun.cms-source-removal-enqueue-request.v1"
REMOVAL_RESPONSE_SCHEMA = "blun.cms-source-removal-enqueue-response.v1"
STATUS_REQUEST_SCHEMA = "blun.cms-source-status-request.v1"
STATUS_RESPONSE_SCHEMA = "blun.cms-source-status-response.v3"
HEALTH_RESPONSE_SCHEMA = "blun.cms-source-health-response.v3"
READINESS_RESPONSE_SCHEMA = "blun.cms-source-readiness-response.v1"
CAPABILITIES_SCHEMA = "blun.cms-source-runtime-capabilities.v1"
CAPABILITIES_RESPONSE_SCHEMA = "blun.cms-source-capabilities-response.v1"

CHANGE_PATH = "/v1/localization/source/changes"
REMOVAL_PATH = "/v1/localization/source/removals"
STATUS_PATH = "/v1/localization/source/status"
HEALTH_PATH = "/v1/localization/source/health"
READINESS_PATH = "/v1/localization/source/readiness"
CAPABILITIES_PATH = "/v1/localization/source/capabilities"

MAX_BODY_BYTES = 4_000_000
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
STATUSES = {"pending", "leased", "retry_wait", "succeeded", "failed"}
REMOTE_QUEUE_STATES = STATUSES | {"cancelled"}
LIFECYCLE_STATES = {
    "pending", "leased", "watching", "retry_wait", "terminal", "failed",
}
REMOTE_STATUSES = {
    "awaiting_approval", "cancelled", "deleted", "deleting",
    "deletion_failed", "localization_failed", "processing",
    "publication_blocked", "publication_failed", "published", "publishing",
    "queue_recovery", "ready", "superseded",
}
HEALTH_STATUSES = {"ok", "degraded", "blocked"}
SCOPES = {
    CHANGE_PATH: "source-change:write",
    REMOVAL_PATH: "source-removal:write",
    STATUS_PATH: "source-status:read",
    HEALTH_PATH: "source-health:read",
    READINESS_PATH: "source-readiness:read",
    CAPABILITIES_PATH: "source-capabilities:read",
}
METHODS = {
    CHANGE_PATH: "POST",
    REMOVAL_PATH: "POST",
    STATUS_PATH: "POST",
    HEALTH_PATH: "GET",
    READINESS_PATH: "GET",
    CAPABILITIES_PATH: "GET",
}


class CMSSourceHTTPBlocked(RuntimeError):
    """Stable content-free HTTP failure."""

    def __init__(self, code: str, status: int):
        if ERROR_CODE.fullmatch(code) is None or status not in {
            400, 401, 403, 404, 405, 411, 413, 415, 409, 503,
        }:
            raise ValueError("invalid source HTTP failure")
        super().__init__(code)
        self.code = code
        self.status = status


def _token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or TOKEN.fullmatch(value) is None
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise ValueError
    return value


def _count(value: Any, *, maximum: int = 1_000_000) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError
    return value


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
        raise CMSSourceHTTPBlocked("source_http.response_invalid", 503) from None
    if not result or len(result) > MAX_BODY_BYTES:
        raise CMSSourceHTTPBlocked("source_http.response_invalid", 503)
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


def _principal(value: Any, scope: str) -> dict[str, str]:
    fields = {
        "schema", "principal_id", "credential_id", "credential_version", "scope",
    }
    status_scope = scope == SCOPES[STATUS_PATH]
    if status_scope:
        fields.add("site_id")
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CMSSourceHTTPBlocked("source_http.authentication_failed", 401)
    try:
        expected_schema = STATUS_PRINCIPAL_SCHEMA if status_scope else PRINCIPAL_SCHEMA
        if value["schema"] != expected_schema:
            raise ValueError
        for name in ("principal_id", "credential_id", "credential_version"):
            _token(value[name])
        _token(value["scope"])
        if status_scope:
            _token(value["site_id"])
    except (KeyError, ValueError, TypeError):
        raise CMSSourceHTTPBlocked("source_http.authentication_failed", 401) from None
    if value["scope"] != scope:
        raise CMSSourceHTTPBlocked("source_http.scope_rejected", 403)
    return dict(value)


def _status_payload(value: Any, *, removal: bool) -> dict[str, Any]:
    try:
        payload = asdict(value)
        required = {
            "event_id", "payload_sha256", "status", "attempts", "max_attempts",
            "next_attempt_at", "lease_expires_at", "lease_expired",
            "last_error_code", "remote_plan_id", "remote_job_count",
            "remote_status", "response_sha256", "last_error_detail_hash",
        }
        if removal:
            required = {
                "operation", "request_id", "event_id", "payload_sha256",
                "status", "attempts", "max_attempts", "next_attempt_at",
                "lease_expires_at", "lease_expired", "last_error_code",
                "remote_delivery_id", "remote_status", "remote_new",
                "response_sha256",
            }
        if set(payload) != required:
            raise ValueError
        _token(payload["event_id"])
        if removal:
            _token(payload["operation"])
            _token(payload["request_id"])
        if SHA256.fullmatch(payload["payload_sha256"]) is None:
            raise ValueError
        if payload["status"] not in STATUSES:
            raise ValueError
        attempts = _count(payload["attempts"], maximum=20)
        maximum = _count(payload["max_attempts"], maximum=20)
        if not 1 <= maximum or attempts > maximum:
            raise ValueError
        return {
            "operation": payload["operation"] if removal else "change",
            "request_id": payload["request_id"] if removal else payload["event_id"],
            "event_id": payload["event_id"],
            "payload_sha256": payload["payload_sha256"],
            "status": payload["status"],
            "attempts": attempts,
            "max_attempts": maximum,
        }
    except Exception:
        raise CMSSourceHTTPBlocked(
            "source_http.runtime_response_invalid", 503,
        ) from None


def _optional_token(value: Any) -> str | None:
    if value is None:
        return None
    return _token(value)


def _optional_error(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or ERROR_CODE.fullmatch(value) is None:
        raise ValueError
    return value


def _timestamp(value: Any) -> float:
    if (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(float(value)) or float(value) < 0
    ):
        raise ValueError
    return float(value)


def _locales(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError
    result = [_token(item) for item in value]
    if len(result) > 24 or len(set(result)) != len(result):
        raise ValueError
    return result


def _source_status_payload(
    value: Any,
    *,
    expected_event_id: str,
    expected_site_id: str,
) -> dict[str, Any]:
    try:
        payload = value.as_payload()
        fields = {
            "schema", "event_id", "site_id", "website_version",
            "source_sequence", "change_sha256", "dispatch_status",
            "dispatch_attempts", "dispatch_max_attempts",
            "dispatch_error_code", "plan_id", "job_count",
            "lifecycle_state", "lifecycle_poll_attempts",
            "lifecycle_error_code", "remote_status", "lifecycle_sha256",
            "required_locales", "approved_locales", "blocked_locales",
            "queue_counts", "notification_state", "notification_id",
            "notification_sha256", "notification_attempts",
            "notification_max_attempts", "notification_error_code",
            "terminal_processing_state", "terminal_processing_poll_attempts",
            "terminal_processing_failures", "terminal_processing_error_code",
            "receiver_processing_state", "receiver_processing_attempts",
            "receiver_processing_max_attempts", "receiver_processing_error_code",
            "receiver_processed_at",
        }
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError
        if payload["schema"] != "blun.cms-source-service-status.v3":
            raise ValueError
        event_id = _token(payload["event_id"])
        site_id = _token(payload["site_id"])
        if event_id != expected_event_id or site_id != expected_site_id:
            raise ValueError
        website_version = _token(payload["website_version"])
        source_sequence = _count(payload["source_sequence"])
        if source_sequence < 1 or SHA256.fullmatch(payload["change_sha256"]) is None:
            raise ValueError
        dispatch_status = payload["dispatch_status"]
        if dispatch_status not in STATUSES:
            raise ValueError
        attempts = _count(payload["dispatch_attempts"], maximum=20)
        maximum = _count(payload["dispatch_max_attempts"], maximum=20)
        if maximum < 1 or attempts > maximum:
            raise ValueError
        dispatch_error = _optional_error(payload["dispatch_error_code"])
        plan_id = _optional_token(payload["plan_id"])
        job_count = payload["job_count"]
        if job_count is not None:
            job_count = _count(job_count)
            if job_count < 1:
                raise ValueError
        lifecycle_state = payload["lifecycle_state"]
        if lifecycle_state is not None and lifecycle_state not in LIFECYCLE_STATES:
            raise ValueError
        poll_attempts = _count(payload["lifecycle_poll_attempts"], maximum=20)
        lifecycle_error = _optional_error(payload["lifecycle_error_code"])
        remote_status = payload["remote_status"]
        if remote_status is not None and remote_status not in REMOTE_STATUSES:
            raise ValueError
        lifecycle_sha256 = payload["lifecycle_sha256"]
        if lifecycle_sha256 is not None and SHA256.fullmatch(lifecycle_sha256) is None:
            raise ValueError
        required = _locales(payload["required_locales"])
        approved = _locales(payload["approved_locales"])
        blocked_value = payload["blocked_locales"]
        if not isinstance(blocked_value, (list, tuple)):
            raise ValueError
        blocked = []
        for item in blocked_value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError
            blocked.append([_token(item[0]), _optional_error(item[1])])
        if any(reason is None for _locale, reason in blocked):
            raise ValueError
        blocked_locales = [locale for locale, _reason in blocked]
        if (
            len(blocked) > 24
            or len(set(blocked_locales)) != len(blocked_locales)
            or required != sorted(required)
            or approved != sorted(approved)
            or blocked != sorted(blocked)
            or not set(approved).issubset(required)
            or not set(blocked_locales).issubset(required)
            or set(approved).intersection(blocked_locales)
        ):
            raise ValueError
        queue_value = payload["queue_counts"]
        if not isinstance(queue_value, Mapping) or set(queue_value) not in (
            set(), STATUSES, REMOTE_QUEUE_STATES,
        ):
            raise ValueError
        queue_counts = {
            name: _count(queue_value[name]) for name in sorted(queue_value)
        }
        notification_state = payload["notification_state"]
        if notification_state not in {
            "disabled", "awaiting_terminal", "awaiting_registration",
            "pending", "leased", "retry_wait", "succeeded", "failed",
        }:
            raise ValueError
        notification_id = _optional_token(payload["notification_id"])
        notification_sha256 = payload["notification_sha256"]
        if (
            notification_sha256 is not None
            and SHA256.fullmatch(notification_sha256) is None
        ):
            raise ValueError
        notification_attempts = _count(
            payload["notification_attempts"], maximum=20,
        )
        notification_max_attempts = payload["notification_max_attempts"]
        if notification_max_attempts is not None:
            notification_max_attempts = _count(
                notification_max_attempts, maximum=20,
            )
        notification_error = _optional_error(
            payload["notification_error_code"]
        )
        durable_notification = notification_state in STATUSES
        if durable_notification != (notification_id is not None):
            raise ValueError
        if durable_notification != (notification_sha256 is not None):
            raise ValueError
        if durable_notification != (notification_max_attempts is not None):
            raise ValueError
        if durable_notification and (
            notification_max_attempts < 1
            or notification_attempts > notification_max_attempts
        ):
            raise ValueError
        if not durable_notification and notification_attempts != 0:
            raise ValueError
        if (notification_state in {"retry_wait", "failed"}) != (
            notification_error is not None
        ):
            raise ValueError
        terminal_processing_state = payload["terminal_processing_state"]
        processing_states = {
            "disabled", "awaiting_notification", "awaiting_registration",
            "pending", "leased", "watching", "retry_wait", "succeeded",
            "failed",
        }
        if terminal_processing_state not in processing_states:
            raise ValueError
        terminal_processing_poll_attempts = _count(
            payload["terminal_processing_poll_attempts"]
        )
        terminal_processing_failures = _count(
            payload["terminal_processing_failures"], maximum=20,
        )
        terminal_processing_error = _optional_error(
            payload["terminal_processing_error_code"]
        )
        receiver_processing_state = payload["receiver_processing_state"]
        if (
            receiver_processing_state is not None
            and receiver_processing_state not in {
                "pending", "leased", "retry_wait", "succeeded", "failed",
            }
        ):
            raise ValueError
        receiver_processing_attempts = payload["receiver_processing_attempts"]
        receiver_processing_max_attempts = payload[
            "receiver_processing_max_attempts"
        ]
        if receiver_processing_attempts is not None:
            receiver_processing_attempts = _count(
                receiver_processing_attempts, maximum=20,
            )
        if receiver_processing_max_attempts is not None:
            receiver_processing_max_attempts = _count(
                receiver_processing_max_attempts, maximum=20,
            )
        receiver_processing_error = _optional_error(
            payload["receiver_processing_error_code"]
        )
        receiver_processed_at = payload["receiver_processed_at"]
        if receiver_processed_at is not None:
            receiver_processed_at = _timestamp(receiver_processed_at)
        durable_processing = terminal_processing_state in {
            "pending", "leased", "watching", "retry_wait", "succeeded",
            "failed",
        }
        if not durable_processing and any((
            terminal_processing_poll_attempts,
            terminal_processing_failures,
            terminal_processing_error,
            receiver_processing_state,
            receiver_processing_attempts,
            receiver_processing_max_attempts,
            receiver_processing_error,
            receiver_processed_at,
        )):
            raise ValueError
        if durable_processing and (
            (terminal_processing_state in {"retry_wait", "failed"})
            != (terminal_processing_error is not None)
        ):
            raise ValueError
        if (receiver_processing_state is None) != (
            receiver_processing_attempts is None
            and receiver_processing_max_attempts is None
        ):
            raise ValueError
        if receiver_processing_state is None and any((
            receiver_processing_error,
            receiver_processed_at,
        )):
            raise ValueError
        if (
            receiver_processing_attempts is not None
            and (
                receiver_processing_max_attempts < 1
                or receiver_processing_attempts
                > receiver_processing_max_attempts
            )
        ):
            raise ValueError
        if (receiver_processing_state == "succeeded") != (
            receiver_processed_at is not None
        ):
            raise ValueError
        if (receiver_processing_state in {"retry_wait", "failed"}) != (
            receiver_processing_error is not None
        ):
            raise ValueError
        if (plan_id is None) != (job_count is None):
            raise ValueError
        if (dispatch_status == "succeeded") != (plan_id is not None):
            raise ValueError
        if (dispatch_status in {"retry_wait", "failed"}) != (
            dispatch_error is not None
        ):
            raise ValueError
        if lifecycle_state is None:
            if any((poll_attempts, lifecycle_error, remote_status, lifecycle_sha256)):
                raise ValueError
            if required or approved or blocked or queue_counts:
                raise ValueError
        elif plan_id is None:
            raise ValueError
        if remote_status is None and (
            lifecycle_sha256 is not None
            or required or approved or blocked or queue_counts
        ):
            raise ValueError
        if remote_status is not None and (
            lifecycle_sha256 is None
            or not required
            or job_count != len(required)
            or set(queue_counts) not in (STATUSES, REMOTE_QUEUE_STATES)
            or (
                remote_status == "queue_recovery"
                and sum(queue_counts.values()) != 0
            )
            or (
                remote_status != "queue_recovery"
                and sum(queue_counts.values()) != len(required)
            )
        ):
            raise ValueError
        return {
            "schema": payload["schema"],
            "event_id": event_id,
            "site_id": site_id,
            "website_version": website_version,
            "source_sequence": source_sequence,
            "change_sha256": payload["change_sha256"],
            "dispatch_status": dispatch_status,
            "dispatch_attempts": attempts,
            "dispatch_max_attempts": maximum,
            "dispatch_error_code": dispatch_error,
            "plan_id": plan_id,
            "job_count": job_count,
            "lifecycle_state": lifecycle_state,
            "lifecycle_poll_attempts": poll_attempts,
            "lifecycle_error_code": lifecycle_error,
            "remote_status": remote_status,
            "lifecycle_sha256": lifecycle_sha256,
            "required_locales": required,
            "approved_locales": approved,
            "blocked_locales": blocked,
            "queue_counts": queue_counts,
            "notification_state": notification_state,
            "notification_id": notification_id,
            "notification_sha256": notification_sha256,
            "notification_attempts": notification_attempts,
            "notification_max_attempts": notification_max_attempts,
            "notification_error_code": notification_error,
            "terminal_processing_state": terminal_processing_state,
            "terminal_processing_poll_attempts": (
                terminal_processing_poll_attempts
            ),
            "terminal_processing_failures": terminal_processing_failures,
            "terminal_processing_error_code": terminal_processing_error,
            "receiver_processing_state": receiver_processing_state,
            "receiver_processing_attempts": receiver_processing_attempts,
            "receiver_processing_max_attempts": (
                receiver_processing_max_attempts
            ),
            "receiver_processing_error_code": receiver_processing_error,
            "receiver_processed_at": receiver_processed_at,
        }
    except Exception:
        raise CMSSourceHTTPBlocked(
            "source_http.runtime_response_invalid", 503,
        ) from None


def _component(
    value: Any,
    states: set[str],
    *,
    operations: bool = False,
    remote_failures: bool = False,
) -> dict[str, Any]:
    fields = {"status", "counts", "due", "expired_leases", "failed"}
    if operations:
        fields.add("operations")
    if remote_failures:
        fields.add("remote_failures")
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError
    if value["status"] not in {"ok", "blocked"}:
        raise ValueError
    counts = value["counts"]
    if not isinstance(counts, Mapping) or set(counts) != states:
        raise ValueError
    normalized = {
        "status": value["status"],
        "counts": {name: _count(counts[name]) for name in sorted(states)},
        "due": _count(value["due"]),
        "expired_leases": _count(value["expired_leases"]),
        "failed": _count(value["failed"]),
    }
    if operations:
        operation_counts = value["operations"]
        if not isinstance(operation_counts, Mapping) or set(operation_counts) != {
            "cancellation", "tombstone",
        }:
            raise ValueError
        normalized["operations"] = {
            name: _count(operation_counts[name])
            for name in ("cancellation", "tombstone")
        }
    if remote_failures:
        normalized["remote_failures"] = _count(value["remote_failures"])
    return normalized


def _health_payload(value: Any) -> dict[str, Any]:
    try:
        payload = value.as_payload()
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema", "status", "pending_lifecycle_registrations",
            "pending_terminal_notifications", "changes", "removals",
            "pending_terminal_processing", "lifecycle", "notifications",
            "terminal_processing", "error_code",
        }:
            raise ValueError
        if payload["schema"] != "blun.cms-source-service-health.v3":
            raise ValueError
        if payload["status"] not in HEALTH_STATUSES:
            raise ValueError
        result = {
            "schema": payload["schema"],
            "status": payload["status"],
            "pending_lifecycle_registrations": _count(
                payload["pending_lifecycle_registrations"]
            ),
            "pending_terminal_notifications": _count(
                payload["pending_terminal_notifications"]
            ),
            "pending_terminal_processing": _count(
                payload["pending_terminal_processing"]
            ),
            "changes": _component(payload["changes"], STATUSES),
            "removals": _component(
                payload["removals"], STATUSES, operations=True,
            ),
            "lifecycle": _component(payload["lifecycle"], {
                "pending", "leased", "watching", "retry_wait",
                "terminal", "failed",
            }, remote_failures=True),
            "notifications": (
                {}
                if payload["notifications"] == {}
                else _component(payload["notifications"], STATUSES)
            ),
            "terminal_processing": (
                {}
                if payload["terminal_processing"] == {}
                else _component(payload["terminal_processing"], {
                    "pending", "leased", "watching", "retry_wait",
                    "succeeded", "failed",
                })
            ),
            "error_code": payload["error_code"],
        }
        if result["error_code"] is not None and ERROR_CODE.fullmatch(
            result["error_code"]
        ) is None:
            raise ValueError
        _canonical_json(result)
        return result
    except CMSSourceHTTPBlocked:
        raise
    except Exception:
        raise CMSSourceHTTPBlocked("source_http.health_invalid", 503) from None


def _readiness_payload(value: Any) -> dict[str, Any]:
    try:
        if not isinstance(value, Mapping) or set(value) != {
            "schema", "status", "worker_state", "service_status", "error_code",
        }:
            raise ValueError
        if value["schema"] != "blun.cms-source-worker-readiness.v1":
            raise ValueError
        if value["status"] not in {"ready", "not_ready"}:
            raise ValueError
        if value["worker_state"] not in {
            "unmanaged", "starting", "running", "stopping", "stopped",
            "failed", "closed",
        }:
            raise ValueError
        if value["service_status"] not in {None, "ok", "degraded", "blocked"}:
            raise ValueError
        error_code = value["error_code"]
        if error_code is not None and ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError
        if value["status"] == "ready":
            if (
                value["worker_state"] != "running"
                or value["service_status"] not in {"ok", "degraded"}
                or error_code is not None
            ):
                raise ValueError
        elif error_code is None:
            raise ValueError
        result = dict(value)
        _canonical_json(result)
        return result
    except CMSSourceHTTPBlocked:
        raise
    except Exception:
        raise CMSSourceHTTPBlocked(
            "source_http.readiness_invalid", 503,
        ) from None


def _capabilities_payload() -> dict[str, Any]:
    """Build the public contract from the exact active HTTP constants."""
    expected_scopes = {
        CHANGE_PATH: "source-change:write",
        REMOVAL_PATH: "source-removal:write",
        STATUS_PATH: "source-status:read",
        HEALTH_PATH: "source-health:read",
        READINESS_PATH: "source-readiness:read",
        CAPABILITIES_PATH: "source-capabilities:read",
    }
    expected_methods = {
        CHANGE_PATH: "POST",
        REMOVAL_PATH: "POST",
        STATUS_PATH: "POST",
        HEALTH_PATH: "GET",
        READINESS_PATH: "GET",
        CAPABILITIES_PATH: "GET",
    }
    if (
        SCOPES != expected_scopes
        or METHODS != expected_methods
        or len(set(SCOPES.values())) != len(SCOPES)
    ):
        raise CMSSourceHTTPBlocked(
            "source_http.capabilities_invalid", 503,
        )

    operations = {
        "capabilities": {
            "method": METHODS[CAPABILITIES_PATH],
            "path": CAPABILITIES_PATH,
            "scope": SCOPES[CAPABILITIES_PATH],
            "principal_schema": PRINCIPAL_SCHEMA,
            "request_schema": None,
            "request_fields": [],
            "response_schema": CAPABILITIES_RESPONSE_SCHEMA,
            "response_fields": ["schema", "capabilities"],
            "success_status": 200,
        },
        "change": {
            "method": METHODS[CHANGE_PATH],
            "path": CHANGE_PATH,
            "scope": SCOPES[CHANGE_PATH],
            "principal_schema": PRINCIPAL_SCHEMA,
            "request_schema": CHANGE_REQUEST_SCHEMA,
            "request_fields": ["schema", "change", "max_attempts"],
            "response_schema": CHANGE_RESPONSE_SCHEMA,
            "response_fields": [
                "schema", "operation", "request_id", "event_id",
                "payload_sha256", "status", "attempts", "max_attempts",
            ],
            "success_status": 202,
        },
        "health": {
            "method": METHODS[HEALTH_PATH],
            "path": HEALTH_PATH,
            "scope": SCOPES[HEALTH_PATH],
            "principal_schema": PRINCIPAL_SCHEMA,
            "request_schema": None,
            "request_fields": [],
            "response_schema": HEALTH_RESPONSE_SCHEMA,
            "response_fields": ["schema", "health"],
            "success_status": 200,
        },
        "removal": {
            "method": METHODS[REMOVAL_PATH],
            "path": REMOVAL_PATH,
            "scope": SCOPES[REMOVAL_PATH],
            "principal_schema": PRINCIPAL_SCHEMA,
            "request_schema": REMOVAL_REQUEST_SCHEMA,
            "request_fields": ["schema", "removal", "max_attempts"],
            "response_schema": REMOVAL_RESPONSE_SCHEMA,
            "response_fields": [
                "schema", "operation", "request_id", "event_id",
                "payload_sha256", "status", "attempts", "max_attempts",
            ],
            "success_status": 202,
        },
        "readiness": {
            "method": METHODS[READINESS_PATH],
            "path": READINESS_PATH,
            "scope": SCOPES[READINESS_PATH],
            "principal_schema": PRINCIPAL_SCHEMA,
            "request_schema": None,
            "request_fields": [],
            "response_schema": READINESS_RESPONSE_SCHEMA,
            "response_fields": ["schema", "readiness"],
            "success_status": 200,
        },
        "status": {
            "method": METHODS[STATUS_PATH],
            "path": STATUS_PATH,
            "scope": SCOPES[STATUS_PATH],
            "principal_schema": STATUS_PRINCIPAL_SCHEMA,
            "request_schema": STATUS_REQUEST_SCHEMA,
            "request_fields": ["schema", "event_id", "site_id"],
            "response_schema": STATUS_RESPONSE_SCHEMA,
            "response_fields": ["schema", "status"],
            "success_status": 200,
        },
    }
    capabilities = {
        "schema": CAPABILITIES_SCHEMA,
        "api_schema": API_SCHEMA,
        "authentication_request_schema": AUTH_REQUEST_SCHEMA,
        "error_schema": ERROR_SCHEMA,
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES,
            "max_headers": MAX_HEADERS,
            "max_header_value_bytes": MAX_HEADER_VALUE,
            "max_attempts_min": 1,
            "max_attempts_max": 20,
        },
        "operations": operations,
    }
    try:
        encoded = _canonical_json(capabilities)
        if set(operations) != {
            "capabilities", "change", "health", "readiness", "removal", "status",
        }:
            raise ValueError
        for name, operation in operations.items():
            if (
                operation["path"] not in SCOPES
                or operation["scope"] != SCOPES[operation["path"]]
                or operation["method"] != METHODS[operation["path"]]
                or operation["principal_schema"] not in {
                    PRINCIPAL_SCHEMA, STATUS_PRINCIPAL_SCHEMA,
                }
                or not isinstance(operation["success_status"], int)
                or name not in {
                    "capabilities", "change", "health", "readiness", "removal",
                    "status",
                }
            ):
                raise ValueError
    except CMSSourceHTTPBlocked:
        raise
    except Exception:
        raise CMSSourceHTTPBlocked(
            "source_http.capabilities_invalid", 503,
        ) from None
    return {
        **capabilities,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


class CMSSourceHTTPApplication:
    """Strict authenticated WSGI adapter over a source-CMS runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if not all(callable(getattr(runtime, name, None)) for name in (
            "enqueue_change", "enqueue_removal", "status", "health",
            "worker_readiness", "require_worker_ready",
        )):
            raise TypeError("runtime must provide source CMS operations")
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
                raise CMSSourceHTTPBlocked("source_http.headers_invalid", 400)
            headers.append((name, value))
        headers.sort()
        if (
            len(headers) > MAX_HEADERS
            or len({name for name, _ in headers}) != len(headers)
        ):
            raise CMSSourceHTTPBlocked("source_http.headers_invalid", 400)
        return tuple(headers)

    @staticmethod
    def _content_type(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        parts = [item.strip().lower() for item in value.split(";")]
        return parts in (["application/json"], ["application/json", "charset=utf-8"])

    @staticmethod
    def _body(environ: Mapping[str, Any], *, required: bool) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
            raise CMSSourceHTTPBlocked("source_http.transfer_encoding_rejected", 400)
        length = environ.get("CONTENT_LENGTH")
        if not required and length in {None, "", "0"}:
            return b""
        if (
            not isinstance(length, str)
            or not length.isascii()
            or not length.isdecimal()
        ):
            raise CMSSourceHTTPBlocked("source_http.content_length_required", 411)
        size = int(length)
        if size <= 0:
            raise CMSSourceHTTPBlocked("source_http.body_invalid", 400)
        if size > MAX_BODY_BYTES:
            raise CMSSourceHTTPBlocked("source_http.body_too_large", 413)
        stream = environ.get("wsgi.input")
        try:
            body = stream.read(size)
        except Exception:
            body = None
        if not isinstance(body, bytes) or len(body) != size:
            raise CMSSourceHTTPBlocked("source_http.body_invalid", 400)
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
            raise CMSSourceHTTPBlocked(
                "source_http.authentication_unavailable", 503
            ) from None
        return _principal(principal, SCOPES[path])

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
    def _error(cls, start_response, failure: CMSSourceHTTPBlocked):
        return cls._send(start_response, failure.status, {
            "schema": ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": failure.code,
        })

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
            raise CMSSourceHTTPBlocked("source_http.json_invalid", 400) from None

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            if not isinstance(environ, Mapping):
                raise CMSSourceHTTPBlocked("source_http.environment_invalid", 400)
            path = environ.get("PATH_INFO")
            if path not in SCOPES:
                raise CMSSourceHTTPBlocked("source_http.route_not_found", 404)
            method = environ.get("REQUEST_METHOD")
            expected_method = METHODS[path]
            if method != expected_method:
                raise CMSSourceHTTPBlocked("source_http.method_not_allowed", 405)
            if environ.get("wsgi.url_scheme") != "https":
                raise CMSSourceHTTPBlocked("source_http.https_required", 400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise CMSSourceHTTPBlocked("source_http.query_rejected", 400)
            if path in {HEALTH_PATH, READINESS_PATH, CAPABILITIES_PATH}:
                body = self._body(environ, required=False)
                if body or environ.get("CONTENT_TYPE") not in {None, ""}:
                    raise CMSSourceHTTPBlocked("source_http.body_not_allowed", 400)
            else:
                if not self._content_type(environ.get("CONTENT_TYPE")):
                    raise CMSSourceHTTPBlocked("source_http.content_type_invalid", 415)
                body = self._body(environ, required=True)
            principal = self._authenticate(environ, path, body)

            if path == CAPABILITIES_PATH:
                return self._send(start_response, 200, {
                    "schema": CAPABILITIES_RESPONSE_SCHEMA,
                    "capabilities": _capabilities_payload(),
                })

            if path == HEALTH_PATH:
                try:
                    health = self.runtime.health()
                except Exception:
                    raise CMSSourceHTTPBlocked(
                        "source_http.runtime_blocked", 503,
                    ) from None
                report = _health_payload(health)
                response_status = 503 if report["status"] == "blocked" else 200
                return self._send(start_response, response_status, {
                    "schema": HEALTH_RESPONSE_SCHEMA,
                    "health": report,
                })

            if path == READINESS_PATH:
                try:
                    readiness = self.runtime.worker_readiness()
                except Exception:
                    raise CMSSourceHTTPBlocked(
                        "source_http.runtime_blocked", 503,
                    ) from None
                report = _readiness_payload(readiness)
                response_status = 200 if report["status"] == "ready" else 503
                return self._send(start_response, response_status, {
                    "schema": READINESS_RESPONSE_SCHEMA,
                    "readiness": report,
                })

            request = self._request(body)
            if path == STATUS_PATH:
                if (
                    not isinstance(request, Mapping)
                    or set(request) != {"schema", "event_id", "site_id"}
                    or request.get("schema") != STATUS_REQUEST_SCHEMA
                ):
                    raise CMSSourceHTTPBlocked("source_http.request_invalid", 400)
                try:
                    event_id = _token(request["event_id"])
                    site_id = _token(request["site_id"])
                except (KeyError, TypeError, ValueError):
                    raise CMSSourceHTTPBlocked(
                        "source_http.request_invalid", 400,
                    ) from None
                if site_id != principal["site_id"]:
                    raise CMSSourceHTTPBlocked(
                        "source_http.status_not_found", 404,
                    )
                try:
                    status = self.runtime.status(event_id, site_id)
                except Exception as error:
                    code = getattr(error, "code", None)
                    if code == "source_runtime.request_invalid":
                        raise CMSSourceHTTPBlocked(
                            "source_http.request_invalid", 400,
                        ) from None
                    if code == "source_runtime.status_not_found":
                        raise CMSSourceHTTPBlocked(
                            "source_http.status_not_found", 404,
                        ) from None
                    raise CMSSourceHTTPBlocked(
                        "source_http.runtime_blocked", 503,
                    ) from None
                return self._send(start_response, 200, {
                    "schema": STATUS_RESPONSE_SCHEMA,
                    "status": _source_status_payload(
                        status,
                        expected_event_id=event_id,
                        expected_site_id=site_id,
                    ),
                })

            envelope_key = "change" if path == CHANGE_PATH else "removal"
            expected_schema = (
                CHANGE_REQUEST_SCHEMA if path == CHANGE_PATH else REMOVAL_REQUEST_SCHEMA
            )
            if (
                not isinstance(request, Mapping)
                or set(request) != {"schema", envelope_key, "max_attempts"}
                or request.get("schema") != expected_schema
            ):
                raise CMSSourceHTTPBlocked("source_http.request_invalid", 400)
            maximum = request["max_attempts"]
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 1 <= maximum <= 20
            ):
                raise CMSSourceHTTPBlocked("source_http.request_invalid", 400)
            try:
                self.runtime.require_worker_ready()
                if path == CHANGE_PATH:
                    status = self.runtime.enqueue_change(
                        request["change"], max_attempts=maximum,
                    )
                else:
                    status = self.runtime.enqueue_removal(
                        request["removal"], max_attempts=maximum,
                    )
            except Exception as error:
                code = getattr(error, "code", None)
                if code == "source_runtime.request_invalid":
                    raise CMSSourceHTTPBlocked(
                        "source_http.request_invalid", 400,
                    ) from None
                if code == "source_runtime.idempotency_collision":
                    raise CMSSourceHTTPBlocked(
                        "source_http.idempotency_collision", 409,
                    ) from None
                if code in {
                    "source_runtime.service_blocked",
                    "source_runtime.database_unsafe",
                    "source_runtime.database_unavailable",
                    "source_runtime.closed",
                    "source_runtime.foreign_process",
                }:
                    raise CMSSourceHTTPBlocked(
                        "source_http.runtime_blocked", 503,
                    ) from None
                raise CMSSourceHTTPBlocked("source_http.runtime_blocked", 503) from None
            payload = _status_payload(status, removal=path == REMOVAL_PATH)
            return self._send(start_response, 202, {
                "schema": (
                    CHANGE_RESPONSE_SCHEMA
                    if path == CHANGE_PATH else REMOVAL_RESPONSE_SCHEMA
                ),
                **payload,
            })
        except CMSSourceHTTPBlocked as failure:
            return self._error(start_response, failure)
        except Exception:
            return self._error(
                start_response,
                CMSSourceHTTPBlocked("source_http.internal", 503),
            )
