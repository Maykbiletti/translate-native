#!/usr/bin/env python3
"""One-attempt HTTPS adapter for durable source-CMS terminal notifications."""

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


NOTIFICATION_SCHEMA = "blun.cms-source-terminal-notification.v1"
ACK_SCHEMA = "blun.cms-source-terminal-notification-ack.v1"
AUTH_SCHEMA = "blun.cms-source-terminal-notification-http-auth.v1"
MAX_BODY_BYTES = 16_384
MAX_RESPONSE_BYTES = 8_192
MAX_ENDPOINT_LENGTH = 2_048
MAX_HEADER_VALUE_LENGTH = 4_096
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
NOTIFICATION_ID = re.compile(r"^terminal-[a-f0-9]{64}$")
TERMINAL_STATUSES = {
    "cancelled", "deleted", "deletion_failed", "localization_failed",
    "publication_blocked", "publication_failed", "published", "superseded",
}
RESERVED_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host",
    "idempotency-key", "transfer-encoding",
    "x-localization-terminal-notification-id",
    "x-localization-terminal-notification-sha256",
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


class HTTPTerminalNotificationFailed(RuntimeError):
    """Content-free failure consumed by the durable notification outbox."""

    cms_notification_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("notification error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("notification retryability must be boolean")
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
    """Perform exactly one bounded HTTPS request without redirects."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

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
            response_headers = (
                tuple(error.headers.items()) if error.headers is not None else ()
            )
            return HTTPResult(int(error.code), response_headers, b"")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise HTTPTerminalNotificationFailed(
                "notification_http.network", retryable=True,
            ) from None
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), raw,
            )
        except (TimeoutError, socket.timeout, OSError):
            raise HTTPTerminalNotificationFailed(
                "notification_http.network", retryable=True,
            ) from None
        finally:
            response.close()


def _fail(code: str, *, retryable: bool = False) -> None:
    raise HTTPTerminalNotificationFailed(
        "notification_http." + code, retryable=retryable,
    )


def _canonical(value: Any, *, maximum: int = MAX_BODY_BYTES) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _fail("request_invalid")
    if not raw or len(raw) > maximum:
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


def _token(value: Any, *, limit: int = 256) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and all(
            "A" <= character <= "Z"
            or "a" <= character <= "z"
            or "0" <= character <= "9"
            or character in "_.:-"
            for character in value
        )
    )


def _endpoint(value: Any, allow_loopback_http: bool) -> tuple[str, str, str]:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > MAX_ENDPOINT_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint is invalid") from None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not hostname
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ValueError("endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and allow_loopback_http
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        pass
    else:
        raise ValueError("endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("endpoint is invalid")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return value, origin, parsed.path


def _authentication_headers(
    provider: Callable[[Mapping[str, Any]], Mapping[str, str]],
    context: Mapping[str, Any],
) -> dict[str, str]:
    try:
        supplied = provider(dict(context))
        if not isinstance(supplied, Mapping):
            raise TypeError("authentication headers must be a mapping")
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


def _validated_notification(value: Any) -> tuple[dict[str, Any], bytes, str]:
    if not isinstance(value, Mapping):
        _fail("request_invalid")
    try:
        copied = json.loads(_canonical(dict(value)).decode("utf-8"))
    except HTTPTerminalNotificationFailed:
        raise
    except Exception:
        _fail("request_invalid")
    lifecycle_sha = copied.get("lifecycle_sha256")
    terminal_status = copied.get("terminal_status")
    positive_integers = all(
        isinstance(copied.get(name), int)
        and not isinstance(copied.get(name), bool)
        and copied[name] > 0
        for name in ("source_sequence", "job_count")
    )
    semantic = (
        copied.get("schema") == NOTIFICATION_SCHEMA
        and set(copied) == NOTIFICATION_FIELDS
        and all(
            _token(copied.get(name))
            for name in ("event_id", "site_id", "plan_id", "website_version")
        )
        and positive_integers
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


class HTTPTerminalNotifierAdapter:
    """Send one immutable terminal notification through one HTTPS attempt."""

    def __init__(
        self,
        endpoint: str,
        authentication_headers: Callable[
            [Mapping[str, Any]], Mapping[str, str]
        ],
        *,
        transport: HTTPTransport | None = None,
        timeout: float | int = 30,
        allow_loopback_http: bool = False,
    ):
        if not isinstance(allow_loopback_http, bool):
            raise TypeError("allow_loopback_http must be boolean")
        self.endpoint, self.origin, self.path = _endpoint(
            endpoint, allow_loopback_http,
        )
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.timeout = float(timeout)

    def __repr__(self) -> str:
        return "HTTPTerminalNotifierAdapter(configured=True)"

    def __call__(self, notification: Mapping[str, Any]) -> Mapping[str, Any]:
        payload, body, body_sha256 = _validated_notification(notification)
        authentication = {
            "schema": AUTH_SCHEMA,
            "method": "POST",
            "origin": self.origin,
            "path": self.path,
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "body_sha256": body_sha256,
        }
        headers = _authentication_headers(
            self.authentication_headers, authentication,
        )
        headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": payload["notification_id"],
            "X-Localization-Terminal-Notification-Id": payload["notification_id"],
            "X-Localization-Terminal-Notification-Sha256": body_sha256,
        })
        try:
            result = self.transport.post(
                self.endpoint, headers, body, timeout=self.timeout,
            )
        except HTTPTerminalNotificationFailed:
            raise
        except Exception:
            _fail("network", retryable=True)
        if (
            not isinstance(result, HTTPResult)
            or isinstance(result.status, bool)
            or not isinstance(result.status, int)
            or not 100 <= result.status <= 599
        ):
            _fail("transport_invalid", retryable=True)
        if result.status != 200:
            if 300 <= result.status <= 399:
                _fail("redirect")
            retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            _fail("http_status", retryable=retryable)
        response_headers = _response_headers(result.headers)
        content_type = response_headers.get("content-type", "").lower().replace(" ", "")
        if content_type not in {
            "application/json", "application/json;charset=utf-8",
        }:
            _fail("response_content_type")
        if (
            not isinstance(result.body, bytes)
            or not result.body
            or len(result.body) > MAX_RESPONSE_BYTES
        ):
            _fail("response_size")
        declared = response_headers.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdecimal()
            or int(declared) != len(result.body)
        ):
            _fail("response_size")
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            acknowledgement = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            _fail("response_json")
        expected = {
            "schema": ACK_SCHEMA,
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "status": "accepted",
            "notification_sha256": body_sha256,
        }
        if (
            not isinstance(acknowledgement, dict)
            or set(acknowledgement) != ACK_FIELDS
            or acknowledgement != expected
        ):
            _fail("response_binding")
        return acknowledgement
