#!/usr/bin/env python3
"""Durable HTTPS/WSGI receiver for source-CMS terminal notifications.

The receiver stores the exact content-free notification before returning its
acknowledgement. Replays converge on the stored row; changed data under either
the event or notification identity fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping


NOTIFICATION_SCHEMA = "blun.cms-source-terminal-notification.v1"
ACK_SCHEMA = "blun.cms-source-terminal-notification-ack.v1"
AUTH_SCHEMA = "blun.cms-source-terminal-notification-http-auth.v1"
PRINCIPAL_SCHEMA = "blun.cms-source-terminal-notification-principal.v1"
ERROR_SCHEMA = "blun.cms-source-terminal-notification-http-error.v1"
WRITE_SCOPE = "terminal-notification:write"
DEFAULT_PATH = "/v1/localization/terminal-notifications"
MAX_BODY_BYTES = 16_384
MAX_HEADERS = 64
MAX_HEADER_VALUE_LENGTH = 4_096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
NOTIFICATION_ID = re.compile(r"^terminal-[a-f0-9]{64}$")
TERMINAL_STATUSES = {
    "cancelled", "deleted", "deletion_failed", "localization_failed",
    "publication_blocked", "publication_failed", "published", "superseded",
}
NOTIFICATION_FIELDS = {
    "schema", "notification_id", "event_id", "site_id", "plan_id",
    "website_version", "source_sequence", "job_count", "change_sha256",
    "lifecycle_binding_sha256", "terminal_status", "lifecycle_sha256",
}
PRINCIPAL_FIELDS = {
    "schema", "principal_id", "credential_id", "credential_version", "scope",
    "site_id",
}
_COLUMNS = (
    "notification_id", "event_id", "site_id", "terminal_status",
    "payload_sha256", "payload_json", "received_at",
)


class TerminalNotificationReceiverBlocked(RuntimeError):
    """Stable, content-free receiver failure."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ReceivedTerminalNotification:
    notification_id: str
    event_id: str
    site_id: str
    terminal_status: str
    payload_sha256: str
    received_at: float


def _blocked(code: str, status: int) -> None:
    raise TerminalNotificationReceiverBlocked("notification_receiver." + code, status)


def _canonical(value: Any) -> bytes:
    try:
        result = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _blocked("request_invalid", 400)
    if not result or len(result) > MAX_BODY_BYTES:
        _blocked("request_invalid", 400)
    return result


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


def _time(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        _blocked("clock_invalid", 503)
    return float(value)


def _origin(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > 2_048
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("origin must be one exact HTTPS origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("origin must be one exact HTTPS origin") from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or value != f"https://{parsed.netloc}"
    ):
        raise ValueError("origin must be one exact HTTPS origin")
    return value


def _parse_notification(body: bytes) -> tuple[dict[str, Any], str]:
    if not isinstance(body, bytes) or not body or len(body) > MAX_BODY_BYTES:
        _blocked("body_invalid", 400)
    try:
        text = body.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("BOM rejected")
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _blocked("json_invalid", 400)
    if not isinstance(value, dict) or set(value) != NOTIFICATION_FIELDS:
        _blocked("request_invalid", 400)
    if _canonical(value) != body:
        _blocked("canonical_body_required", 400)
    lifecycle_sha = value.get("lifecycle_sha256")
    status = value.get("terminal_status")
    if not (
        value.get("schema") == NOTIFICATION_SCHEMA
        and all(
            _token(value.get(name))
            for name in ("event_id", "site_id", "plan_id", "website_version")
        )
        and all(
            isinstance(value.get(name), int)
            and not isinstance(value.get(name), bool)
            and value[name] > 0
            for name in ("source_sequence", "job_count")
        )
        and all(
            isinstance(value.get(name), str)
            and SHA256.fullmatch(value[name]) is not None
            for name in ("change_sha256", "lifecycle_binding_sha256")
        )
        and status in TERMINAL_STATUSES
        and (
            status in {"cancelled", "superseded"} and lifecycle_sha is None
            or status not in {"cancelled", "superseded"}
            and isinstance(lifecycle_sha, str)
            and SHA256.fullmatch(lifecycle_sha) is not None
        )
    ):
        _blocked("request_invalid", 400)
    identity_source = dict(value)
    notification_id = identity_source.pop("notification_id", None)
    expected_id = "terminal-" + hashlib.sha256(_canonical(identity_source)).hexdigest()
    if (
        not isinstance(notification_id, str)
        or NOTIFICATION_ID.fullmatch(notification_id) is None
        or notification_id != expected_id
    ):
        _blocked("request_invalid", 400)
    return value, hashlib.sha256(body).hexdigest()


def _principal(value: Any, site_id: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != PRINCIPAL_FIELDS:
        _blocked("authentication_failed", 401)
    if not (
        value.get("schema") == PRINCIPAL_SCHEMA
        and value.get("scope") == WRITE_SCOPE
        and value.get("site_id") == site_id
        and all(
            _token(value.get(name))
            for name in (
                "principal_id", "credential_id", "credential_version", "site_id",
            )
        )
    ):
        _blocked("authorization_failed", 403)
    return dict(value)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        _blocked("transaction_nested", 503)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableCMSTerminalNotificationInbox:
    """One process-owned, serialized SQLite terminal-notification ledger."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._initialize()

    def _owner(self) -> None:
        if os.getpid() != self._owner_pid:
            _blocked("foreign_process", 503)

    def _initialize(self) -> None:
        with self._lock, _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_terminal_notification_inbox_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_terminal_notification_inbox_meta
                VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_terminal_notification_inbox (
                    notification_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    site_id TEXT NOT NULL,
                    terminal_status TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    received_at REAL NOT NULL
                )
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_terminal_notification_inbox_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_terminal_notification_inbox)"
        ).fetchall())
        meta = self.connection.execute(
            "SELECT singleton, schema_version "
            "FROM cms_terminal_notification_inbox_meta"
        ).fetchall()
        if (
            meta_columns != ("singleton", "schema_version")
            or columns != _COLUMNS
            or len(meta) != 1
            or tuple(meta[0]) != (1, 1)
        ):
            _blocked("schema_altered", 503)

    @staticmethod
    def _ack(payload: Mapping[str, Any], payload_sha256: str) -> dict[str, Any]:
        return {
            "schema": ACK_SCHEMA,
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "status": "accepted",
            "notification_sha256": payload_sha256,
        }

    def accept(
        self,
        payload: Mapping[str, Any],
        body: bytes,
        payload_sha256: str,
        *,
        now: float | int,
    ) -> dict[str, Any]:
        self._owner()
        now = _time(now)
        parsed, actual_sha256 = _parse_notification(body)
        if not isinstance(payload, Mapping) or not isinstance(payload_sha256, str):
            _blocked("binding_invalid", 400)
        try:
            copied = dict(payload)
        except Exception:
            _blocked("binding_invalid", 400)
        if (
            copied != parsed
            or payload_sha256 != actual_sha256
            or SHA256.fullmatch(payload_sha256) is None
        ):
            _blocked("binding_invalid", 400)
        rendered = body.decode("utf-8")
        with self._lock:
            self._validate_schema()
            try:
                with _transaction(self.connection):
                    rows = self.connection.execute("""
                        SELECT * FROM cms_terminal_notification_inbox
                        WHERE notification_id = ? OR event_id = ?
                           OR payload_sha256 = ?
                    """, (
                        parsed["notification_id"], parsed["event_id"], payload_sha256,
                    )).fetchall()
                    if rows:
                        if len(rows) != 1 or not all(
                            rows[0][name] == expected
                            for name, expected in {
                                "notification_id": parsed["notification_id"],
                                "event_id": parsed["event_id"],
                                "site_id": parsed["site_id"],
                                "terminal_status": parsed["terminal_status"],
                                "payload_sha256": payload_sha256,
                                "payload_json": rendered,
                            }.items()
                        ):
                            _blocked("idempotency_collision", 409)
                    else:
                        self.connection.execute("""
                            INSERT INTO cms_terminal_notification_inbox (
                                notification_id, event_id, site_id, terminal_status,
                                payload_sha256, payload_json, received_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (
                            parsed["notification_id"], parsed["event_id"],
                            parsed["site_id"], parsed["terminal_status"],
                            payload_sha256, rendered, now,
                        ))
            except TerminalNotificationReceiverBlocked:
                raise
            except sqlite3.Error:
                _blocked("storage_unavailable", 503)
        return self._ack(parsed, payload_sha256)

    def status(self, event_id: str) -> ReceivedTerminalNotification:
        self._owner()
        if not _token(event_id):
            _blocked("request_invalid", 400)
        with self._lock:
            self._validate_schema()
            try:
                row = self.connection.execute(
                    "SELECT * FROM cms_terminal_notification_inbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
            except sqlite3.Error:
                _blocked("storage_unavailable", 503)
            if row is None:
                _blocked("not_found", 404)
            parsed, payload_sha256 = _parse_notification(
                row["payload_json"].encode("utf-8")
            )
            if not all(
                row[name] == expected
                for name, expected in {
                    "notification_id": parsed["notification_id"],
                    "event_id": parsed["event_id"],
                    "site_id": parsed["site_id"],
                    "terminal_status": parsed["terminal_status"],
                    "payload_sha256": payload_sha256,
                }.items()
            ):
                _blocked("stored_binding_invalid", 503)
            return ReceivedTerminalNotification(
                row["notification_id"], row["event_id"], row["site_id"],
                row["terminal_status"], row["payload_sha256"],
                _time(row["received_at"]),
            )


class CMSTerminalNotificationReceiverApplication:
    """Authenticate, durably store, and then acknowledge one notification."""

    def __init__(
        self,
        inbox: DurableCMSTerminalNotificationInbox,
        authenticate: Callable[[Mapping[str, Any], Mapping[str, str]], Any],
        *,
        origin: str,
        clock: Callable[[], float | int],
        path: str = DEFAULT_PATH,
        require_https: bool = True,
    ):
        if not isinstance(inbox, DurableCMSTerminalNotificationInbox):
            raise TypeError("inbox is invalid")
        if not callable(authenticate) or not callable(clock):
            raise TypeError("authenticate and clock must be callable")
        origin = _origin(origin)
        if (
            not isinstance(path, str)
            or not path.isascii()
            or not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or len(path) > 256
        ):
            raise ValueError("path is invalid")
        if not isinstance(require_https, bool):
            raise TypeError("require_https must be boolean")
        self.inbox = inbox
        self.authenticate = authenticate
        self.origin = origin
        self.clock = clock
        self.path = path
        self.require_https = require_https

    @staticmethod
    def _headers(environ: Mapping[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in environ.items():
            if key == "CONTENT_TYPE":
                name = "content-type"
            elif key == "CONTENT_LENGTH":
                name = "content-length"
            elif isinstance(key, str) and key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").lower()
            else:
                continue
            if (
                name in result
                or not isinstance(value, str)
                or not value
                or len(value) > MAX_HEADER_VALUE_LENGTH
                or "\r" in value
                or "\n" in value
            ):
                _blocked("headers_invalid", 400)
            result[name] = value
        if len(result) > MAX_HEADERS:
            _blocked("headers_invalid", 400)
        return result

    @staticmethod
    def _send(start_response: Callable[..., Any], status: int, value: Any):
        body = _canonical(value)
        phrases = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 411: "Length Required", 413: "Content Too Large",
            415: "Unsupported Media Type", 503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ))
        return [body]

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            if not isinstance(environ, Mapping):
                _blocked("environment_invalid", 400)
            if environ.get("PATH_INFO") != self.path:
                _blocked("path_not_found", 404)
            if environ.get("REQUEST_METHOD") != "POST":
                _blocked("method_not_allowed", 405)
            if self.require_https and environ.get("wsgi.url_scheme") != "https":
                _blocked("https_required", 400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                _blocked("query_rejected", 400)
            if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
                _blocked("framing_invalid", 400)
            headers = self._headers(environ)
            if headers.get("content-type", "").lower().replace(" ", "") != (
                "application/json;charset=utf-8"
            ):
                _blocked("content_type", 415)
            length = headers.get("content-length")
            if not isinstance(length, str) or not length.isascii() or not length.isdecimal():
                _blocked("content_length_required", 411)
            size = int(length)
            if size <= 0:
                _blocked("body_invalid", 400)
            if size > MAX_BODY_BYTES:
                _blocked("body_too_large", 413)
            try:
                body = environ["wsgi.input"].read(size)
            except Exception:
                body = None
            if not isinstance(body, bytes) or len(body) != size:
                _blocked("body_invalid", 400)
            payload, body_sha256 = _parse_notification(body)
            expected_headers = {
                "idempotency-key": payload["notification_id"],
                "x-localization-terminal-notification-id": payload["notification_id"],
                "x-localization-terminal-notification-sha256": body_sha256,
            }
            if any(headers.get(name) != value for name, value in expected_headers.items()):
                _blocked("header_binding_invalid", 400)
            request = {
                "schema": AUTH_SCHEMA,
                "method": "POST",
                "origin": self.origin,
                "path": self.path,
                "notification_id": payload["notification_id"],
                "event_id": payload["event_id"],
                "site_id": payload["site_id"],
                "body_sha256": body_sha256,
            }
            try:
                principal = self.authenticate(dict(request), dict(headers))
            except Exception:
                _blocked("authentication_unavailable", 503)
            _principal(principal, payload["site_id"])
            acknowledgement = self.inbox.accept(
                payload, body, body_sha256, now=self.clock(),
            )
            return self._send(start_response, 200, acknowledgement)
        except TerminalNotificationReceiverBlocked as failure:
            return self._send(start_response, failure.status, {
                "schema": ERROR_SCHEMA,
                "status": "BLOCK",
                "error_code": failure.code,
            })
        except Exception:
            return self._send(start_response, 503, {
                "schema": ERROR_SCHEMA,
                "status": "BLOCK",
                "error_code": "notification_receiver.internal",
            })
