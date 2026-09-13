#!/usr/bin/env python3
"""Authenticated public write ingress for the owned website submission edge.

The WSGI boundary accepts one exact website change or removal per request.
Authentication sees request metadata and the body digest, while the principal
is bound to the payload site before any runtime or durable outbox access.
HTTP 202 means only that the website outbox durably accepted the request; it
never implies source acceptance, localization quality, approval, or publication.
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

API_SCHEMA = "blun.cms-source-delivery-submission-http.v1"
ERROR_SCHEMA = "blun.cms-source-delivery-submission-http-error.v1"
AUTH_REQUEST_SCHEMA = _SUBMISSION.WRITE_HTTP_AUTH_REQUEST_SCHEMA
PRINCIPAL_SCHEMA = _SUBMISSION.WRITE_HTTP_PRINCIPAL_SCHEMA
CHANGE_REQUEST_SCHEMA = _SUBMISSION.CHANGE_HTTP_REQUEST_SCHEMA
REMOVAL_REQUEST_SCHEMA = _SUBMISSION.REMOVAL_HTTP_REQUEST_SCHEMA
CHANGE_RESPONSE_SCHEMA = _SUBMISSION.CHANGE_HTTP_RESPONSE_SCHEMA
REMOVAL_RESPONSE_SCHEMA = _SUBMISSION.REMOVAL_HTTP_RESPONSE_SCHEMA
CHANGE_PATH = _SUBMISSION.CHANGE_HTTP_PATH
REMOVAL_PATH = _SUBMISSION.REMOVAL_HTTP_PATH
CAPABILITIES_PATH = _SUBMISSION.CAPABILITIES_HTTP_PATH
CHANGE_SCOPE = _SUBMISSION.CHANGE_HTTP_SCOPE
REMOVAL_SCOPE = _SUBMISSION.REMOVAL_HTTP_SCOPE

MAX_BODY_BYTES = 4_000_000
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
STATUSES = {"pending", "leased", "retry_wait", "succeeded", "failed"}
SCOPES = {CHANGE_PATH: CHANGE_SCOPE, REMOVAL_PATH: REMOVAL_SCOPE}
REQUEST_SCHEMAS = {
    CHANGE_PATH: CHANGE_REQUEST_SCHEMA,
    REMOVAL_PATH: REMOVAL_REQUEST_SCHEMA,
}
RESPONSE_SCHEMAS = {
    CHANGE_PATH: CHANGE_RESPONSE_SCHEMA,
    REMOVAL_PATH: REMOVAL_RESPONSE_SCHEMA,
}


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


def _principal(value: Any, scope: str) -> dict[str, str]:
    fields = {
        "schema", "principal_id", "credential_id", "credential_version",
        "scope", "site_id",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _blocked("authentication_failed", 401)
    try:
        if value["schema"] != PRINCIPAL_SCHEMA:
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


class SubmissionHTTPApplication:
    """Strict public WSGI adapter over one hosted website submission runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if not all(callable(getattr(runtime, name, None)) for name in (
            "submission_capabilities", "website_capability_binding",
            "worker_readiness", "enqueue_change", "enqueue_removal",
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
            202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
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
                "schema": AUTH_REQUEST_SCHEMA,
                "method": "POST",
                "path": path,
                "headers": [list(item) for item in headers],
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
            try:
                principal = _principal(self.authenticator(auth_request), SCOPES[path])
            except SubmissionHTTPBlocked:
                raise
            except Exception:
                raise _blocked("authentication_unavailable", 503) from None

            request = _request(body)
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
    """Build the public capability and durable-write website boundary."""

    return SubmissionHTTPApplication(runtime, authenticator)
