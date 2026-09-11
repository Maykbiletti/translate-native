#!/usr/bin/env python3
"""Contract-pinned HTTPS client for one terminal receiver."""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


AUTH_SCHEMA = "blun.cms-source-terminal-notification-http-auth.v1"
API_SCHEMA = "blun.cms-terminal-receiver-api.v1"
CAPABILITIES_SCHEMA = "blun.cms-terminal-receiver-capabilities.v1"
CAPABILITIES_RESPONSE_SCHEMA = (
    "blun.cms-terminal-receiver-capabilities-response.v1"
)
HEALTH_SCHEMA = "blun.cms-terminal-receiver-health.v1"
READINESS_SCHEMA = "blun.cms-terminal-receiver-readiness.v1"
STATUS_REQUEST_SCHEMA = "blun.cms-terminal-receiver-status-request.v1"
STATUS_RESPONSE_SCHEMA = "blun.cms-terminal-receiver-status-response.v1"
ERROR_SCHEMA = "blun.cms-source-terminal-notification-http-error.v1"
PRINCIPAL_SCHEMA = "blun.cms-source-terminal-notification-principal.v1"
NOTIFICATION_SCHEMA = "blun.cms-source-terminal-notification.v1"
ACK_SCHEMA = "blun.cms-source-terminal-notification-ack.v1"
BASE_PATH = "/v1/localization/terminal-notifications"
CAPABILITIES_PATH = BASE_PATH + "/capabilities"
HEALTH_PATH = BASE_PATH + "/health"
READINESS_PATH = BASE_PATH + "/readiness"
STATUS_PATH = BASE_PATH + "/status"
MAX_RESPONSE_BYTES = 16_384
MAX_ENDPOINT_LENGTH = 2_048
MAX_HEADER_VALUE_LENGTH = 4_096
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SHA256 = re.compile(r"^[a-f0-9]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
NOTIFICATION_ID = re.compile(r"^terminal-[a-f0-9]{64}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
PROCESSING_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
TERMINAL_STATUSES = {
    "cancelled", "deleted", "deletion_failed", "localization_failed",
    "publication_blocked", "publication_failed", "published", "superseded",
}
WORKER_STATES = {
    "unmanaged", "starting", "running", "stopping", "stopped", "failed",
}
RESERVED_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host",
    "idempotency-key", "transfer-encoding",
    "x-localization-terminal-notification-id",
    "x-localization-terminal-notification-sha256",
    "x-localization-terminal-status-sha256",
}
NOTIFICATION_FIELDS = {
    "schema", "notification_id", "event_id", "site_id", "plan_id",
    "website_version", "source_sequence", "job_count", "change_sha256",
    "lifecycle_binding_sha256", "terminal_status", "lifecycle_sha256",
}
ACK_FIELDS = {
    "schema", "notification_id", "event_id", "site_id", "status",
    "notification_sha256",
}


class TerminalReceiverClientBlocked(RuntimeError):
    """Stable, content-free control-plane failure."""

    cms_notification_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool):
            raise ValueError("terminal receiver client failure is invalid")
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
    raise TerminalReceiverClientBlocked(
        "terminal_receiver_client." + code, retryable=retryable,
    )


def _canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _fail("request_invalid")
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
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
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _validated_notification(value: Any) -> tuple[dict[str, Any], bytes, str]:
    if not isinstance(value, Mapping):
        _fail("request_invalid")
    try:
        copied = json.loads(_canonical(dict(value)).decode("utf-8"))
    except TerminalReceiverClientBlocked:
        raise
    except Exception:
        _fail("request_invalid")
    lifecycle_sha = copied.get("lifecycle_sha256")
    terminal_status = copied.get("terminal_status")
    semantic = (
        copied.get("schema") == NOTIFICATION_SCHEMA
        and set(copied) == NOTIFICATION_FIELDS
        and all(
            _token(copied.get(name))
            for name in ("event_id", "site_id", "plan_id", "website_version")
        )
        and all(
            isinstance(copied.get(name), int)
            and not isinstance(copied.get(name), bool)
            and copied[name] > 0
            for name in ("source_sequence", "job_count")
        )
        and all(
            isinstance(copied.get(name), str)
            and SHA256.fullmatch(copied[name]) is not None
            for name in ("change_sha256", "lifecycle_binding_sha256")
        )
        and terminal_status in TERMINAL_STATUSES
        and (
            terminal_status in {"cancelled", "superseded"}
            and lifecycle_sha is None
            or terminal_status not in {"cancelled", "superseded"}
            and isinstance(lifecycle_sha, str)
            and SHA256.fullmatch(lifecycle_sha) is not None
        )
    )
    identity_source = dict(copied)
    notification_id = identity_source.pop("notification_id", None)
    expected_id = "terminal-" + hashlib.sha256(
        _canonical(identity_source),
    ).hexdigest()
    if (
        not semantic
        or not isinstance(notification_id, str)
        or NOTIFICATION_ID.fullmatch(notification_id) is None
        or notification_id != expected_id
    ):
        _fail("request_invalid")
    body = _canonical(copied)
    return copied, body, hashlib.sha256(body).hexdigest()


def _number(value: Any, *, nullable: bool = False) -> bool:
    return (
        nullable and value is None
        or isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _endpoint(value: Any, allow_loopback_http: bool) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or value != value.strip()
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
    return value, parsed.scheme


def _authentication_headers(provider, context) -> dict[str, str]:
    try:
        supplied = provider(dict(context))
        if not isinstance(supplied, Mapping):
            raise TypeError
        items = tuple(supplied.items())
    except Exception:
        _fail("authentication")
    result: dict[str, str] = {}
    names: set[str] = set()
    for name, value in items:
        normalized = name.lower() if isinstance(name, str) else ""
        if (
            not isinstance(name, str)
            or HEADER_NAME.fullmatch(name) is None
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


def _operation(name, method, path, scope, request_schema, request_fields,
               response_schema, response_fields):
    return {
        "method": method,
        "path": path,
        "scope": scope,
        "request_schema": request_schema,
        "request_fields": request_fields,
        "response_schema": response_schema,
        "response_fields": response_fields,
        "success_status": 200,
    }


def _valid_capabilities(value: Any, expected_sha256: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema", "api_schema", "authentication_request_schema",
        "principal_schema", "error_schema", "limits", "processing_statuses",
        "terminal_statuses", "operations", "sha256",
    }:
        return False
    digest = value.get("sha256")
    unsigned = dict(value)
    unsigned.pop("sha256", None)
    if (
        digest != expected_sha256
        or hashlib.sha256(_canonical(unsigned)).hexdigest() != digest
        or value.get("schema") != CAPABILITIES_SCHEMA
        or value.get("api_schema") != API_SCHEMA
        or value.get("authentication_request_schema") != AUTH_SCHEMA
        or value.get("principal_schema") != PRINCIPAL_SCHEMA
        or value.get("error_schema") != ERROR_SCHEMA
        or value.get("processing_statuses") != list(PROCESSING_STATUSES)
        or value.get("terminal_statuses") != sorted(TERMINAL_STATUSES)
    ):
        return False
    limits = value.get("limits")
    if (
        not isinstance(limits, dict)
        or set(limits) != {
            "max_body_bytes", "max_headers", "max_header_value_bytes",
            "processing_max_attempts_min", "processing_max_attempts_max",
        }
        or limits != {
            "max_body_bytes": 16_384,
            "max_headers": 64,
            "max_header_value_bytes": 4_096,
            "processing_max_attempts_min": 1,
            "processing_max_attempts_max": 20,
        }
    ):
        return False
    operations = value.get("operations")
    if not isinstance(operations, dict) or set(operations) != {
        "capabilities", "health", "notification", "readiness", "status",
    }:
        return False
    expected = {
        "capabilities": _operation(
            "capabilities", "GET", CAPABILITIES_PATH,
            "terminal-notification-capabilities:read", None, [],
            CAPABILITIES_RESPONSE_SCHEMA, ["schema", "capabilities"],
        ),
        "health": _operation(
            "health", "GET", HEALTH_PATH, "terminal-notification-health:read",
            None, [], HEALTH_SCHEMA,
            [
                "schema", "status", "runtime_state", "worker_state",
                "inbox_status", "received", "processing_counts",
                "processing_due", "expired_leases", "failed", "error_code",
                "capabilities_sha256",
            ],
        ),
        "readiness": _operation(
            "readiness", "GET", READINESS_PATH,
            "terminal-notification-readiness:read", None, [], READINESS_SCHEMA,
            [
                "schema", "status", "worker_state", "inbox_status",
                "error_code", "capabilities_sha256",
            ],
        ),
        "status": _operation(
            "status", "POST", STATUS_PATH, "terminal-notification-status:read",
            STATUS_REQUEST_SCHEMA, ["schema", "event_id", "site_id"],
            STATUS_RESPONSE_SCHEMA,
            [
                "schema", "notification_id", "event_id", "site_id",
                "terminal_status", "notification_sha256", "processing_status",
                "attempts", "max_attempts", "next_attempt_at",
                "lease_expires_at", "lease_expired", "last_error_code",
                "processed_at",
                "capabilities_sha256",
            ],
        ),
    }
    notification = operations.get("notification")
    if not isinstance(notification, dict):
        return False
    path = notification.get("path")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or path in {CAPABILITIES_PATH, HEALTH_PATH, READINESS_PATH, STATUS_PATH}
    ):
        return False
    expected["notification"] = _operation(
        "notification", "POST", path, "terminal-notification:write",
        NOTIFICATION_SCHEMA,
        [
            "schema", "notification_id", "event_id", "site_id", "plan_id",
            "website_version", "source_sequence", "job_count", "change_sha256",
            "lifecycle_binding_sha256", "terminal_status", "lifecycle_sha256",
        ],
        ACK_SCHEMA,
        [
            "schema", "notification_id", "event_id", "site_id", "status",
            "notification_sha256",
        ],
    )
    return operations == expected


class HTTPTerminalReceiverClient:
    """Operate one receiver only after verifying its pinned live contract."""

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
        self.origin, _scheme = _endpoint(origin, allow_loopback_http)
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
        return "HTTPTerminalReceiverClient(configured=True)"

    def _request(self, method, path, body, allowed_statuses, context):
        body_sha256 = hashlib.sha256(body or b"").hexdigest()
        authentication = {
            "schema": AUTH_SCHEMA,
            "method": method,
            "origin": self.origin,
            "path": path,
            "body_sha256": body_sha256,
            **context,
        }
        headers = _authentication_headers(
            self.authentication_headers, authentication,
        )
        headers["Accept"] = "application/json"
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["X-Localization-Terminal-Status-SHA256"] = body_sha256
        try:
            result = self.transport.request(
                method, self.origin + path, headers, body,
                timeout=self.timeout,
            )
        except TerminalReceiverClientBlocked:
            raise
        except Exception:
            _fail("network", retryable=True)
        return result, _json_response(result, allowed_statuses)

    def capabilities(self) -> Mapping[str, Any]:
        _result, response = self._request(
            "GET", CAPABILITIES_PATH, None, {200}, {},
        )
        if (
            set(response) != {"schema", "capabilities"}
            or response.get("schema") != CAPABILITIES_RESPONSE_SCHEMA
            or not _valid_capabilities(
                response.get("capabilities"),
                self.expected_capabilities_sha256,
            )
        ):
            _fail("capabilities_binding")
        return response

    def _verify_contract(self) -> None:
        self.capabilities()

    def notify(self, notification: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one immutable notification through its freshly pinned route."""

        payload, body, body_sha256 = _validated_notification(notification)
        capabilities = self.capabilities()["capabilities"]
        path = capabilities["operations"]["notification"]["path"]
        context = {
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
        }
        authentication = {
            "schema": AUTH_SCHEMA,
            "method": "POST",
            "origin": self.origin,
            "path": path,
            "body_sha256": body_sha256,
            **context,
        }
        headers = _authentication_headers(
            self.authentication_headers, authentication,
        )
        headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": payload["notification_id"],
            "X-Localization-Terminal-Notification-Id": (
                payload["notification_id"]
            ),
            "X-Localization-Terminal-Notification-Sha256": body_sha256,
        })
        try:
            result = self.transport.request(
                "POST", self.origin + path, headers, body,
                timeout=self.timeout,
            )
        except TerminalReceiverClientBlocked:
            raise
        except Exception:
            _fail("network", retryable=True)
        acknowledgement = _json_response(result, {200})
        expected = {
            "schema": ACK_SCHEMA,
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "status": "accepted",
            "notification_sha256": body_sha256,
        }
        if set(acknowledgement) != ACK_FIELDS or acknowledgement != expected:
            _fail("notification_binding")
        return acknowledgement

    def __call__(self, notification: Mapping[str, Any]) -> Mapping[str, Any]:
        """Allow direct use as a durable source-service terminal notifier."""

        return self.notify(notification)

    def readiness(self) -> Mapping[str, Any]:
        self._verify_contract()
        result, response = self._request(
            "GET", READINESS_PATH, None, {200, 503}, {},
        )
        if not self._valid_readiness(response, result.status):
            _fail("readiness_binding")
        return response

    def health(self) -> Mapping[str, Any]:
        self._verify_contract()
        result, response = self._request(
            "GET", HEALTH_PATH, None, {200, 503}, {},
        )
        if not self._valid_health(response, result.status):
            _fail("health_binding")
        return response

    def status(self, event_id: str, site_id: str) -> Mapping[str, Any]:
        if not _token(event_id) or not _token(site_id):
            _fail("request_invalid")
        self._verify_contract()
        request = {
            "schema": STATUS_REQUEST_SCHEMA,
            "event_id": event_id,
            "site_id": site_id,
        }
        body = _canonical(request)
        _result, response = self._request(
            "POST", STATUS_PATH, body, {200},
            {"event_id": event_id, "site_id": site_id},
        )
        if not self._valid_status(response, event_id, site_id):
            _fail("status_binding")
        return response

    def _valid_readiness(
        self, value: Mapping[str, Any], http_status: int,
    ) -> bool:
        if set(value) != {
            "schema", "status", "worker_state", "inbox_status", "error_code",
            "capabilities_sha256",
        }:
            return False
        ready = value.get("status") == "ready"
        error = value.get("error_code")
        return (
            value.get("schema") == READINESS_SCHEMA
            and value.get("capabilities_sha256")
            == self.expected_capabilities_sha256
            and value.get("status") in {"ready", "not_ready"}
            and value.get("worker_state") in WORKER_STATES | {"closed"}
            and value.get("inbox_status") in {None, "ok", "blocked"}
            and (error is None or isinstance(error, str) and ERROR_CODE.fullmatch(error))
            and ready == (http_status == 200)
            and ready == (
                value.get("worker_state") == "running"
                and value.get("inbox_status") == "ok"
                and error is None
            )
        )

    def _valid_health(
        self, value: Mapping[str, Any], http_status: int,
    ) -> bool:
        fields = {
            "schema", "status", "runtime_state", "worker_state", "inbox_status",
            "received", "processing_counts", "processing_due", "expired_leases",
            "failed", "error_code",
            "capabilities_sha256",
        }
        if set(value) != fields:
            return False
        counts = value.get("processing_counts")
        metrics = [
            value.get("received"), value.get("processing_due"),
            value.get("expired_leases"), value.get("failed"),
        ]
        available = isinstance(counts, dict)
        valid_counts = (
            available
            and set(counts) == set(PROCESSING_STATUSES)
            and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0
                    for item in counts.values())
        )
        healthy = value.get("status") == "ok"
        error = value.get("error_code")
        return (
            value.get("schema") == HEALTH_SCHEMA
            and value.get("capabilities_sha256")
            == self.expected_capabilities_sha256
            and value.get("status") in {"ok", "blocked"}
            and value.get("runtime_state") == "open"
            and value.get("worker_state") in WORKER_STATES
            and value.get("inbox_status") in {"ok", "blocked"}
            and (error is None or isinstance(error, str) and ERROR_CODE.fullmatch(error))
            and healthy == (http_status == 200)
            and healthy == (
                value.get("worker_state") in {"unmanaged", "running"}
                and value.get("inbox_status") == "ok"
                and error is None
            )
            and (
                valid_counts
                and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0
                        for item in metrics)
                and sum(counts.values()) == value.get("received")
                and counts["failed"] == value.get("failed")
                and value.get("processing_due") <= counts["pending"] + counts["retry_wait"]
                and value.get("expired_leases") <= counts["leased"]
                and (value.get("inbox_status") == "ok") == (
                    value.get("failed") == 0
                    and value.get("expired_leases") == 0
                )
                or not available
                and all(item is None for item in metrics)
                and not healthy
                and value.get("inbox_status") == "blocked"
            )
        )

    def _valid_status(
        self, value: Mapping[str, Any], event_id: str, site_id: str,
    ) -> bool:
        fields = {
            "schema", "notification_id", "event_id", "site_id",
            "terminal_status", "notification_sha256", "processing_status",
            "attempts", "max_attempts", "next_attempt_at", "lease_expires_at",
            "lease_expired", "last_error_code", "processed_at",
            "capabilities_sha256",
        }
        error = value.get("last_error_code")
        return (
            set(value) == fields
            and value.get("schema") == STATUS_RESPONSE_SCHEMA
            and value.get("capabilities_sha256")
            == self.expected_capabilities_sha256
            and isinstance(value.get("notification_id"), str)
            and NOTIFICATION_ID.fullmatch(value["notification_id"]) is not None
            and value.get("event_id") == event_id
            and value.get("site_id") == site_id
            and value.get("terminal_status") in TERMINAL_STATUSES
            and isinstance(value.get("notification_sha256"), str)
            and SHA256.fullmatch(value["notification_sha256"]) is not None
            and value.get("processing_status") in PROCESSING_STATUSES
            and all(
                isinstance(value.get(name), int)
                and not isinstance(value.get(name), bool)
                and value[name] >= 0
                for name in ("attempts", "max_attempts")
            )
            and 1 <= value.get("max_attempts", 0) <= 20
            and value.get("attempts", 0) <= value.get("max_attempts", 0)
            and _number(value.get("next_attempt_at"))
            and _number(value.get("lease_expires_at"), nullable=True)
            and isinstance(value.get("lease_expired"), bool)
            and (error is None or isinstance(error, str) and ERROR_CODE.fullmatch(error))
            and _number(value.get("processed_at"), nullable=True)
            and (error is None or value.get("processing_status") in {
                "retry_wait", "failed",
            })
            and (value.get("processing_status") == "succeeded") == (
                value.get("processed_at") is not None
            )
            and (value.get("processing_status") == "leased") == (
                value.get("lease_expires_at") is not None
            )
            and (
                not value.get("lease_expired")
                or value.get("processing_status") == "leased"
            )
        )
