#!/usr/bin/env python3
"""Authenticated WSGI ingress for one durable source-CMS runtime.

The boundary accepts exact change and removal envelopes from the website host
and exposes only aggregate, content-free health. Authentication receives the
request metadata and body digest, never the website payload itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict
from typing import Any, Callable, Mapping


API_SCHEMA = "blun.cms-source-runtime-api.v1"
ERROR_SCHEMA = "blun.cms-source-runtime-http-error.v1"
AUTH_REQUEST_SCHEMA = "blun.cms-source-runtime-http-auth-request.v1"
PRINCIPAL_SCHEMA = "blun.cms-source-runtime-principal.v1"
CHANGE_REQUEST_SCHEMA = "blun.cms-source-change-enqueue-request.v1"
CHANGE_RESPONSE_SCHEMA = "blun.cms-source-change-enqueue-response.v1"
REMOVAL_REQUEST_SCHEMA = "blun.cms-source-removal-enqueue-request.v1"
REMOVAL_RESPONSE_SCHEMA = "blun.cms-source-removal-enqueue-response.v1"
HEALTH_RESPONSE_SCHEMA = "blun.cms-source-health-response.v1"

CHANGE_PATH = "/v1/localization/source/changes"
REMOVAL_PATH = "/v1/localization/source/removals"
HEALTH_PATH = "/v1/localization/source/health"

MAX_BODY_BYTES = 4_000_000
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
STATUSES = {"pending", "leased", "retry_wait", "succeeded", "failed"}
HEALTH_STATUSES = {"ok", "degraded", "blocked"}
SCOPES = {
    CHANGE_PATH: "source-change:write",
    REMOVAL_PATH: "source-removal:write",
    HEALTH_PATH: "source-health:read",
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


def _principal(value: Any, scope: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "schema", "principal_id", "credential_id", "credential_version", "scope",
    }:
        raise CMSSourceHTTPBlocked("source_http.authentication_failed", 401)
    try:
        if value["schema"] != PRINCIPAL_SCHEMA:
            raise ValueError
        for name in ("principal_id", "credential_id", "credential_version"):
            _token(value[name])
        _token(value["scope"])
    except (KeyError, ValueError, TypeError):
        raise CMSSourceHTTPBlocked("source_http.authentication_failed", 401) from None
    if value["scope"] != scope:
        raise CMSSourceHTTPBlocked("source_http.scope_rejected", 403)


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
            "changes", "removals", "lifecycle", "error_code",
        }:
            raise ValueError
        if payload["schema"] != "blun.cms-source-service-health.v1":
            raise ValueError
        if payload["status"] not in HEALTH_STATUSES:
            raise ValueError
        result = {
            "schema": payload["schema"],
            "status": payload["status"],
            "pending_lifecycle_registrations": _count(
                payload["pending_lifecycle_registrations"]
            ),
            "changes": _component(payload["changes"], STATUSES),
            "removals": _component(
                payload["removals"], STATUSES, operations=True,
            ),
            "lifecycle": _component(payload["lifecycle"], {
                "pending", "leased", "watching", "retry_wait",
                "terminal", "failed",
            }, remote_failures=True),
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


class CMSSourceHTTPApplication:
    """Strict authenticated WSGI adapter over a source-CMS runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if not all(callable(getattr(runtime, name, None)) for name in (
            "enqueue_change", "enqueue_removal", "health",
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
    ) -> None:
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
        _principal(principal, SCOPES[path])

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
            expected_method = "GET" if path == HEALTH_PATH else "POST"
            if method != expected_method:
                raise CMSSourceHTTPBlocked("source_http.method_not_allowed", 405)
            if environ.get("wsgi.url_scheme") != "https":
                raise CMSSourceHTTPBlocked("source_http.https_required", 400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise CMSSourceHTTPBlocked("source_http.query_rejected", 400)
            if path == HEALTH_PATH:
                body = self._body(environ, required=False)
                if body or environ.get("CONTENT_TYPE") not in {None, ""}:
                    raise CMSSourceHTTPBlocked("source_http.body_not_allowed", 400)
            else:
                if not self._content_type(environ.get("CONTENT_TYPE")):
                    raise CMSSourceHTTPBlocked("source_http.content_type_invalid", 415)
                body = self._body(environ, required=True)
            self._authenticate(environ, path, body)

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

            request = self._request(body)
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
