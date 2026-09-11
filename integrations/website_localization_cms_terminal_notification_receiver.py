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
import secrets
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping


NOTIFICATION_SCHEMA = "blun.cms-source-terminal-notification.v1"
ACK_SCHEMA = "blun.cms-source-terminal-notification-ack.v1"
PROCESSING_ACK_SCHEMA = "blun.cms-terminal-notification-processing-ack.v1"
AUTH_SCHEMA = "blun.cms-source-terminal-notification-http-auth.v1"
PRINCIPAL_SCHEMA = "blun.cms-source-terminal-notification-principal.v1"
ERROR_SCHEMA = "blun.cms-source-terminal-notification-http-error.v1"
API_SCHEMA = "blun.cms-terminal-receiver-api.v1"
CAPABILITIES_SCHEMA = "blun.cms-terminal-receiver-capabilities.v1"
CAPABILITIES_RESPONSE_SCHEMA = (
    "blun.cms-terminal-receiver-capabilities-response.v1"
)
HEALTH_RESPONSE_SCHEMA = "blun.cms-terminal-receiver-health.v1"
HEALTH_RESPONSE_FIELDS = (
    "schema", "status", "runtime_state", "worker_state", "inbox_status",
    "received", "processing_counts", "processing_due", "expired_leases",
    "failed", "error_code",
)
WRITE_SCOPE = "terminal-notification:write"
STATUS_SCOPE = "terminal-notification-status:read"
READINESS_SCOPE = "terminal-notification-readiness:read"
CAPABILITIES_SCOPE = "terminal-notification-capabilities:read"
HEALTH_SCOPE = "terminal-notification-health:read"
DEFAULT_PATH = "/v1/localization/terminal-notifications"
STATUS_PATH = DEFAULT_PATH + "/status"
READINESS_PATH = DEFAULT_PATH + "/readiness"
CAPABILITIES_PATH = DEFAULT_PATH + "/capabilities"
HEALTH_PATH = DEFAULT_PATH + "/health"
STATUS_REQUEST_SCHEMA = "blun.cms-terminal-receiver-status-request.v1"
STATUS_RESPONSE_SCHEMA = "blun.cms-terminal-receiver-status-response.v1"
READINESS_RESPONSE_SCHEMA = "blun.cms-terminal-receiver-readiness.v1"
MAX_BODY_BYTES = 16_384
MAX_HEADERS = 64
MAX_HEADER_VALUE_LENGTH = 4_096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
NOTIFICATION_ID = re.compile(r"^terminal-[a-f0-9]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SCHEMA_VERSION = 2
PROCESSING_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
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
_PROCESSING_COLUMNS = (
    "notification_id", "status", "attempts", "max_attempts",
    "next_attempt_at", "lease_owner", "lease_token", "lease_expires_at",
    "last_error_code", "processed_at", "created_at", "updated_at",
)


class TerminalNotificationReceiverBlocked(RuntimeError):
    """Stable, content-free receiver failure."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


class TerminalNotificationProcessingFailure(RuntimeError):
    """Public host-handler failure requesting a bounded processing retry."""

    cms_terminal_processing_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool):
            raise ValueError("invalid terminal processing failure")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ReceivedTerminalNotification:
    notification_id: str
    event_id: str
    site_id: str
    terminal_status: str
    payload_sha256: str
    received_at: float


@dataclass(frozen=True)
class TerminalNotificationInboxHealth:
    status: str
    received: int
    counts: dict[str, int]
    due: int
    expired_leases: int
    failed: int


@dataclass(frozen=True)
class TerminalNotificationProcessingClaim:
    notification_id: str
    event_id: str
    site_id: str
    terminal_status: str
    payload_sha256: str
    attempt: int
    max_attempts: int
    worker_id: str
    lease_token: str
    lease_expires_at: float
    payload: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class TerminalNotificationProcessingStatus:
    notification_id: str
    event_id: str
    site_id: str
    terminal_status: str
    payload_sha256: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    processed_at: float | None


@dataclass(frozen=True)
class TerminalNotificationProcessingOutcome:
    notification_id: str
    event_id: str
    status: str
    attempt: int
    error_code: str | None


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


def _duration(value: Any, code: str) -> float:
    result = _time(value)
    if not 0 < result <= 86_400:
        _blocked(code, 503)
    return result


def _error(value: Any) -> bool:
    return isinstance(value, str) and ERROR_CODE.fullmatch(value) is not None


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


def _principal(
    value: Any,
    site_id: str | None,
    scope: str = WRITE_SCOPE,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != PRINCIPAL_FIELDS:
        _blocked("authentication_failed", 401)
    if not (
        value.get("schema") == PRINCIPAL_SCHEMA
        and value.get("scope") == scope
        and (site_id is None or value.get("site_id") == site_id)
        and all(
            _token(value.get(name))
            for name in (
                "principal_id", "credential_id", "credential_version", "site_id",
            )
        )
    ):
        _blocked("authorization_failed", 403)
    return dict(value)


def capabilities_payload(notification_path: str = DEFAULT_PATH) -> dict[str, Any]:
    """Return the exact, content-free receiver contract for integrations."""

    expected_controls = {
        "capabilities": {
            "method": "GET",
            "path": CAPABILITIES_PATH,
            "scope": CAPABILITIES_SCOPE,
            "request_schema": None,
            "request_fields": [],
            "response_schema": CAPABILITIES_RESPONSE_SCHEMA,
            "response_fields": ["schema", "capabilities"],
            "success_status": 200,
        },
        "health": {
            "method": "GET",
            "path": HEALTH_PATH,
            "scope": HEALTH_SCOPE,
            "request_schema": None,
            "request_fields": [],
            "response_schema": HEALTH_RESPONSE_SCHEMA,
            "response_fields": [
                *HEALTH_RESPONSE_FIELDS, "capabilities_sha256",
            ],
            "success_status": 200,
        },
        "readiness": {
            "method": "GET",
            "path": READINESS_PATH,
            "scope": READINESS_SCOPE,
            "request_schema": None,
            "request_fields": [],
            "response_schema": READINESS_RESPONSE_SCHEMA,
            "response_fields": [
                "schema", "status", "worker_state", "inbox_status",
                "error_code", "capabilities_sha256",
            ],
            "success_status": 200,
        },
        "status": {
            "method": "POST",
            "path": STATUS_PATH,
            "scope": STATUS_SCOPE,
            "request_schema": STATUS_REQUEST_SCHEMA,
            "request_fields": ["schema", "event_id", "site_id"],
            "response_schema": STATUS_RESPONSE_SCHEMA,
            "response_fields": [
                "schema", "notification_id", "event_id", "site_id",
                "terminal_status", "notification_sha256",
                "processing_status", "attempts", "max_attempts",
                "next_attempt_at", "lease_expires_at", "lease_expired",
                "last_error_code", "processed_at",
                "capabilities_sha256",
            ],
            "success_status": 200,
        },
    }
    if (
        not isinstance(notification_path, str)
        or notification_path in {
            STATUS_PATH, READINESS_PATH, CAPABILITIES_PATH, HEALTH_PATH,
        }
        or len({
            WRITE_SCOPE, STATUS_SCOPE, READINESS_SCOPE, CAPABILITIES_SCOPE,
            HEALTH_SCOPE,
        }) != 5
    ):
        _blocked("capabilities_invalid", 503)
    operations = {
        **expected_controls,
        "notification": {
            "method": "POST",
            "path": notification_path,
            "scope": WRITE_SCOPE,
            "request_schema": NOTIFICATION_SCHEMA,
            "request_fields": [
                "schema", "notification_id", "event_id", "site_id",
                "plan_id", "website_version", "source_sequence",
                "job_count", "change_sha256", "lifecycle_binding_sha256",
                "terminal_status", "lifecycle_sha256",
            ],
            "response_schema": ACK_SCHEMA,
            "response_fields": [
                "schema", "notification_id", "event_id", "site_id",
                "status", "notification_sha256",
            ],
            "success_status": 200,
        },
    }
    capabilities = {
        "schema": CAPABILITIES_SCHEMA,
        "api_schema": API_SCHEMA,
        "authentication_request_schema": AUTH_SCHEMA,
        "principal_schema": PRINCIPAL_SCHEMA,
        "error_schema": ERROR_SCHEMA,
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES,
            "max_headers": MAX_HEADERS,
            "max_header_value_bytes": MAX_HEADER_VALUE_LENGTH,
            "processing_max_attempts_min": 1,
            "processing_max_attempts_max": 20,
        },
        "processing_statuses": list(PROCESSING_STATUSES),
        "terminal_statuses": sorted(TERMINAL_STATUSES),
        "operations": operations,
    }
    try:
        if set(operations) != {
            "capabilities", "health", "notification", "readiness", "status",
        }:
            raise ValueError
        if set(NOTIFICATION_FIELDS) != set(
            operations["notification"]["request_fields"]
        ):
            raise ValueError
        encoded = json.dumps(
            capabilities, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if not encoded or len(encoded) > MAX_BODY_BYTES:
            raise ValueError
    except (TypeError, ValueError, RecursionError):
        _blocked("capabilities_invalid", 503)
    return {
        **capabilities,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


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

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        database_guard: Callable[[], None] | None = None,
        processing_max_attempts: int = 5,
        processing_base_delay_seconds: float | int = 5,
        processing_max_delay_seconds: float | int = 300,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if database_guard is not None and not callable(database_guard):
            raise TypeError("database_guard must be callable")
        if (
            isinstance(processing_max_attempts, bool)
            or not isinstance(processing_max_attempts, int)
            or not 1 <= processing_max_attempts <= 20
        ):
            raise ValueError("processing_max_attempts must be between 1 and 20")
        processing_base_delay_seconds = _duration(
            processing_base_delay_seconds, "processing_delay_invalid"
        )
        processing_max_delay_seconds = _duration(
            processing_max_delay_seconds, "processing_delay_invalid"
        )
        if processing_base_delay_seconds > processing_max_delay_seconds:
            raise ValueError("processing delay range is invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._database_guard = database_guard or (lambda: None)
        self.processing_max_attempts = processing_max_attempts
        self.processing_base_delay_seconds = processing_base_delay_seconds
        self.processing_max_delay_seconds = processing_max_delay_seconds
        self._initialize()

    def _owner(self) -> None:
        if os.getpid() != self._owner_pid:
            _blocked("foreign_process", 503)

    def _initialize(self) -> None:
        with self._lock:
            self._guard()
            with _transaction(self.connection):
                tables = {
                    row[0] for row in self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name LIKE 'cms_terminal_notification_%'"
                    ).fetchall()
                }
                if not tables:
                    self._create_schema()
                elif tables == {
                    "cms_terminal_notification_inbox_meta",
                    "cms_terminal_notification_inbox",
                }:
                    self._migrate_v1()
            self._validate_schema()

    def _guard(self) -> None:
        try:
            self._database_guard()
        except TerminalNotificationReceiverBlocked:
            raise
        except Exception:
            _blocked("database_unsafe", 503)

    def _create_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS cms_terminal_notification_inbox_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL CHECK (schema_version = 2)
            )
        """)
        self.connection.execute("""
            INSERT OR IGNORE INTO cms_terminal_notification_inbox_meta
            VALUES (1, 2)
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
        self._create_processing_schema()

    def _create_processing_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE cms_terminal_notification_processing (
                notification_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'leased', 'retry_wait',
                               'succeeded', 'failed')
                ),
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                max_attempts INTEGER NOT NULL CHECK (
                    max_attempts BETWEEN 1 AND 20
                ),
                next_attempt_at REAL NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at REAL,
                last_error_code TEXT,
                processed_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (notification_id)
                    REFERENCES cms_terminal_notification_inbox(notification_id)
                    ON DELETE RESTRICT,
                CHECK (
                    (status = 'leased' AND lease_owner IS NOT NULL
                     AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                    OR
                    (status <> 'leased' AND lease_owner IS NULL
                     AND lease_token IS NULL AND lease_expires_at IS NULL)
                )
            )
        """)
        self.connection.execute("""
            CREATE INDEX cms_terminal_notification_processing_due
            ON cms_terminal_notification_processing (
                status, next_attempt_at, created_at, notification_id
            )
        """)

    def _migrate_v1(self) -> None:
        self._validate_v1_schema()
        rows = self.connection.execute(
            "SELECT * FROM cms_terminal_notification_inbox "
            "ORDER BY received_at, event_id"
        ).fetchall()
        for row in rows:
            self._validated_row(row)
        self._create_processing_schema()
        for row in rows:
            self.connection.execute("""
                INSERT INTO cms_terminal_notification_processing (
                    notification_id, status, attempts, max_attempts,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, 'pending', 0, ?, ?, ?, ?)
            """, (
                row["notification_id"], self.processing_max_attempts,
                row["received_at"], row["received_at"], row["received_at"],
            ))
        self.connection.execute(
            "DROP TABLE cms_terminal_notification_inbox_meta"
        )
        self.connection.execute("""
            CREATE TABLE cms_terminal_notification_inbox_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL CHECK (schema_version = 2)
            )
        """)
        self.connection.execute(
            "INSERT INTO cms_terminal_notification_inbox_meta VALUES (1, 2)"
        )

    def _validate_v1_schema(self) -> None:
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

    def _validate_schema(self) -> None:
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_terminal_notification_inbox_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_terminal_notification_inbox)"
        ).fetchall())
        processing_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_terminal_notification_processing)"
            ).fetchall()
        )
        foreign_keys = [
            tuple(row) for row in self.connection.execute(
                "PRAGMA foreign_key_list(cms_terminal_notification_processing)"
            ).fetchall()
        ]
        meta = self.connection.execute(
            "SELECT singleton, schema_version "
            "FROM cms_terminal_notification_inbox_meta"
        ).fetchall()
        if (
            meta_columns != ("singleton", "schema_version")
            or columns != _COLUMNS
            or processing_columns != _PROCESSING_COLUMNS
            or foreign_keys != [(
                0, 0, "cms_terminal_notification_inbox", "notification_id",
                "notification_id", "NO ACTION", "RESTRICT", "NONE",
            )]
            or len(meta) != 1
            or tuple(meta[0]) != (1, SCHEMA_VERSION)
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

    @staticmethod
    def _validated_row(row: sqlite3.Row) -> ReceivedTerminalNotification:
        try:
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
                raise ValueError("binding mismatch")
            received_at = _time(row["received_at"])
        except TerminalNotificationReceiverBlocked:
            _blocked("stored_binding_invalid", 503)
        except Exception:
            _blocked("stored_binding_invalid", 503)
        return ReceivedTerminalNotification(
            row["notification_id"], row["event_id"], row["site_id"],
            row["terminal_status"], row["payload_sha256"], received_at,
        )

    @staticmethod
    def _validated_processing_row(
        row: sqlite3.Row,
        notification_row: sqlite3.Row,
    ) -> None:
        try:
            received = DurableCMSTerminalNotificationInbox._validated_row(
                notification_row
            )
            status = row["status"]
            attempts = row["attempts"]
            max_attempts = row["max_attempts"]
            next_attempt_at = _time(row["next_attempt_at"])
            created_at = _time(row["created_at"])
            updated_at = _time(row["updated_at"])
            lease_values = (
                row["lease_owner"], row["lease_token"], row["lease_expires_at"],
            )
            leased = status == "leased"
            error = row["last_error_code"]
            processed_at = row["processed_at"]
            valid = (
                row["notification_id"] == received.notification_id
                and status in PROCESSING_STATUSES
                and isinstance(attempts, int)
                and not isinstance(attempts, bool)
                and isinstance(max_attempts, int)
                and not isinstance(max_attempts, bool)
                and 0 <= attempts <= max_attempts <= 20
                and max_attempts >= 1
                and leased == all(value is not None for value in lease_values)
                and (not leased or (
                    _token(row["lease_owner"])
                    and _token(row["lease_token"])
                    and _time(row["lease_expires_at"]) > updated_at
                ))
                and (leased or all(value is None for value in lease_values))
                and (status in {"retry_wait", "failed"}) == (error is not None)
                and (error is None or _error(error))
                and (status == "succeeded") == (processed_at is not None)
                and (
                    processed_at is None
                    or _time(processed_at) >= created_at
                )
                and created_at == received.received_at
                and next_attempt_at >= created_at
                and updated_at >= created_at
                and (status == "pending") == (attempts == 0)
                and (status == "pending" or attempts > 0)
            )
        except Exception:
            valid = False
        if not valid:
            _blocked("processing_state_invalid", 503)

    def _processing_rows(self) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
        notifications = {
            row["notification_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM cms_terminal_notification_inbox"
            ).fetchall()
        }
        processing = self.connection.execute(
            "SELECT * FROM cms_terminal_notification_processing"
        ).fetchall()
        if len(notifications) != len(processing):
            _blocked("processing_state_invalid", 503)
        result = []
        for row in processing:
            notification = notifications.get(row["notification_id"])
            if notification is None:
                _blocked("processing_state_invalid", 503)
            self._validated_processing_row(row, notification)
            result.append((row, notification))
        return result

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
            self._guard()
            self._validate_schema()
            try:
                with _transaction(self.connection):
                    self._processing_rows()
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
                        self.connection.execute("""
                            INSERT INTO cms_terminal_notification_processing (
                                notification_id, status, attempts, max_attempts,
                                next_attempt_at, created_at, updated_at
                            ) VALUES (?, 'pending', 0, ?, ?, ?, ?)
                        """, (
                            parsed["notification_id"],
                            self.processing_max_attempts, now, now, now,
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
            self._guard()
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
            return self._validated_row(row)

    @staticmethod
    def _processing_status(
        row: sqlite3.Row,
        notification_row: sqlite3.Row,
        now: float,
    ) -> TerminalNotificationProcessingStatus:
        DurableCMSTerminalNotificationInbox._validated_processing_row(
            row, notification_row
        )
        notification = DurableCMSTerminalNotificationInbox._validated_row(
            notification_row
        )
        lease_expires_at = row["lease_expires_at"]
        return TerminalNotificationProcessingStatus(
            notification.notification_id,
            notification.event_id,
            notification.site_id,
            notification.terminal_status,
            notification.payload_sha256,
            row["status"],
            int(row["attempts"]),
            int(row["max_attempts"]),
            float(row["next_attempt_at"]),
            None if lease_expires_at is None else float(lease_expires_at),
            row["status"] == "leased" and float(lease_expires_at) <= now,
            row["last_error_code"],
            None if row["processed_at"] is None else float(row["processed_at"]),
        )

    def claim_processing(
        self,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> TerminalNotificationProcessingClaim | None:
        self._owner()
        if not _token(worker_id) or len(worker_id) > 128:
            _blocked("processing_worker_invalid", 503)
        now = _time(now)
        lease_seconds = _duration(lease_seconds, "processing_lease_invalid")
        with self._lock:
            self._guard()
            self._validate_schema()
            try:
                with _transaction(self.connection):
                    self._processing_rows()
                    self.connection.execute("""
                        UPDATE cms_terminal_notification_processing
                        SET status = CASE WHEN attempts >= max_attempts
                                THEN 'failed' ELSE 'retry_wait' END,
                            next_attempt_at = ?, lease_owner = NULL,
                            lease_token = NULL, lease_expires_at = NULL,
                            last_error_code = 'processing_lease_expired',
                            updated_at = ?
                        WHERE status = 'leased' AND lease_expires_at <= ?
                    """, (now, now, now))
                    row = self.connection.execute("""
                        SELECT * FROM cms_terminal_notification_processing
                        WHERE status IN ('pending', 'retry_wait')
                          AND next_attempt_at <= ? AND attempts < max_attempts
                        ORDER BY created_at, notification_id LIMIT 1
                    """, (now,)).fetchone()
                    if row is None:
                        return None
                    notification_row = self.connection.execute(
                        "SELECT * FROM cms_terminal_notification_inbox "
                        "WHERE notification_id = ?",
                        (row["notification_id"],),
                    ).fetchone()
                    if notification_row is None:
                        _blocked("processing_state_invalid", 503)
                    self._validated_processing_row(row, notification_row)
                    token = secrets.token_urlsafe(32)
                    expires_at = now + lease_seconds
                    updated = self.connection.execute("""
                        UPDATE cms_terminal_notification_processing
                        SET status = 'leased', attempts = attempts + 1,
                            lease_owner = ?, lease_token = ?,
                            lease_expires_at = ?, last_error_code = NULL,
                            updated_at = ?
                        WHERE notification_id = ?
                          AND status IN ('pending', 'retry_wait')
                    """, (
                        worker_id, token, expires_at, now,
                        row["notification_id"],
                    ))
                    if updated.rowcount != 1:
                        _blocked("processing_claim_lost", 503)
                    payload, _payload_sha256 = _parse_notification(
                        notification_row["payload_json"].encode("utf-8")
                    )
                    received = self._validated_row(notification_row)
                    return TerminalNotificationProcessingClaim(
                        received.notification_id,
                        received.event_id,
                        received.site_id,
                        received.terminal_status,
                        received.payload_sha256,
                        int(row["attempts"]) + 1,
                        int(row["max_attempts"]),
                        worker_id,
                        token,
                        expires_at,
                        MappingProxyType(payload),
                    )
            except TerminalNotificationReceiverBlocked:
                raise
            except sqlite3.Error:
                _blocked("storage_unavailable", 503)

    def processing_status(
        self,
        event_id: str,
        *,
        now: float | int,
    ) -> TerminalNotificationProcessingStatus:
        self._owner()
        if not _token(event_id):
            _blocked("request_invalid", 400)
        now = _time(now)
        with self._lock:
            self._guard()
            self._validate_schema()
            try:
                rows = self._processing_rows()
            except TerminalNotificationReceiverBlocked:
                raise
            except sqlite3.Error:
                _blocked("storage_unavailable", 503)
            for row, notification_row in rows:
                if notification_row["event_id"] == event_id:
                    return self._processing_status(row, notification_row, now)
            _blocked("not_found", 404)

    def _require_processing_claim(
        self,
        claim: TerminalNotificationProcessingClaim,
        now: float,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if not isinstance(claim, TerminalNotificationProcessingClaim):
            _blocked("processing_claim_invalid", 503)
        row = self.connection.execute(
            "SELECT * FROM cms_terminal_notification_processing "
            "WHERE notification_id = ?",
            (claim.notification_id,),
        ).fetchone()
        notification_row = self.connection.execute(
            "SELECT * FROM cms_terminal_notification_inbox "
            "WHERE notification_id = ?",
            (claim.notification_id,),
        ).fetchone()
        if row is None or notification_row is None:
            _blocked("processing_claim_lost", 503)
        self._validated_processing_row(row, notification_row)
        received = self._validated_row(notification_row)
        try:
            claim_payload = _canonical(dict(claim.payload)).decode("utf-8")
        except Exception:
            _blocked("processing_claim_invalid", 503)
        if not (
            row["status"] == "leased"
            and row["lease_owner"] == claim.worker_id
            and row["lease_token"] == claim.lease_token
            and int(row["attempts"]) == claim.attempt
            and int(row["max_attempts"]) == claim.max_attempts
            and float(row["lease_expires_at"]) == claim.lease_expires_at
            and notification_row["payload_json"] == claim_payload
            and (
                received.notification_id,
                received.event_id,
                received.site_id,
                received.terminal_status,
                received.payload_sha256,
            ) == (
                claim.notification_id,
                claim.event_id,
                claim.site_id,
                claim.terminal_status,
                claim.payload_sha256,
            )
        ):
            _blocked("processing_claim_lost", 503)
        if float(row["lease_expires_at"]) <= now:
            _blocked("processing_lease_expired", 503)
        return row, notification_row

    def complete_processing(
        self,
        claim: TerminalNotificationProcessingClaim,
        response: Any,
        *,
        now: float | int,
    ) -> TerminalNotificationProcessingStatus:
        self._owner()
        now = _time(now)
        if not isinstance(response, Mapping):
            _blocked("processing_response_invalid", 503)
        try:
            copied = json.loads(_canonical(dict(response)))
        except Exception:
            _blocked("processing_response_invalid", 503)
        expected = {
            "schema": PROCESSING_ACK_SCHEMA,
            "notification_id": claim.notification_id,
            "event_id": claim.event_id,
            "site_id": claim.site_id,
            "status": "processed",
            "notification_sha256": claim.payload_sha256,
        }
        if copied != expected:
            _blocked("processing_response_invalid", 503)
        with self._lock:
            self._guard()
            self._validate_schema()
            try:
                with _transaction(self.connection):
                    self._processing_rows()
                    self._require_processing_claim(claim, now)
                    updated = self.connection.execute("""
                        UPDATE cms_terminal_notification_processing
                        SET status = 'succeeded', next_attempt_at = ?,
                            lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL, last_error_code = NULL,
                            processed_at = ?, updated_at = ?
                        WHERE notification_id = ? AND status = 'leased'
                          AND lease_owner = ? AND lease_token = ?
                    """, (
                        now, now, now, claim.notification_id,
                        claim.worker_id, claim.lease_token,
                    ))
                    if updated.rowcount != 1:
                        _blocked("processing_completion_lost", 503)
            except TerminalNotificationReceiverBlocked:
                raise
            except sqlite3.Error:
                _blocked("storage_unavailable", 503)
        return self.processing_status(claim.event_id, now=now)

    def retry_processing(
        self,
        claim: TerminalNotificationProcessingClaim,
        code: str,
        *,
        now: float | int,
    ) -> TerminalNotificationProcessingStatus:
        return self._finish_processing(claim, code, retryable=True, now=now)

    def fail_processing(
        self,
        claim: TerminalNotificationProcessingClaim,
        code: str,
        *,
        now: float | int,
    ) -> TerminalNotificationProcessingStatus:
        return self._finish_processing(claim, code, retryable=False, now=now)

    def _finish_processing(
        self,
        claim: TerminalNotificationProcessingClaim,
        code: str,
        *,
        retryable: bool,
        now: float | int,
    ) -> TerminalNotificationProcessingStatus:
        self._owner()
        if not _error(code):
            _blocked("processing_error_invalid", 503)
        now = _time(now)
        with self._lock:
            self._guard()
            self._validate_schema()
            try:
                with _transaction(self.connection):
                    self._processing_rows()
                    row, _notification_row = self._require_processing_claim(
                        claim, now
                    )
                    terminal = not retryable or int(row["attempts"]) >= int(
                        row["max_attempts"]
                    )
                    delay = min(
                        self.processing_max_delay_seconds,
                        self.processing_base_delay_seconds
                        * (2 ** max(0, claim.attempt - 1)),
                    )
                    updated = self.connection.execute("""
                        UPDATE cms_terminal_notification_processing
                        SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                            lease_token = NULL, lease_expires_at = NULL,
                            last_error_code = ?, processed_at = NULL,
                            updated_at = ?
                        WHERE notification_id = ? AND status = 'leased'
                          AND lease_owner = ? AND lease_token = ?
                    """, (
                        "failed" if terminal else "retry_wait",
                        now if terminal else now + delay,
                        code,
                        now,
                        claim.notification_id,
                        claim.worker_id,
                        claim.lease_token,
                    ))
                    if updated.rowcount != 1:
                        _blocked("processing_failure_lost", 503)
            except TerminalNotificationReceiverBlocked:
                raise
            except sqlite3.Error:
                _blocked("storage_unavailable", 503)
        return self.processing_status(claim.event_id, now=now)

    def run_next_processing(
        self,
        callback: Callable[[Mapping[str, Any]], Any],
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> TerminalNotificationProcessingOutcome | None:
        if not callable(callback):
            _blocked("processing_callback_invalid", 503)
        now = _time(now)
        claim = self.claim_processing(
            worker_id, now=now, lease_seconds=lease_seconds
        )
        if claim is None:
            return None
        try:
            response = callback(dict(claim.payload))
            status = self.complete_processing(claim, response, now=now)
        except TerminalNotificationReceiverBlocked as error:
            if error.code != "notification_receiver.processing_response_invalid":
                raise
            status = self.fail_processing(claim, error.code, now=now)
        except Exception as error:
            retryable = (
                getattr(error, "cms_terminal_processing_failure", False) is True
                and getattr(error, "retryable", None) is True
            )
            code = getattr(error, "code", None)
            if not _error(code):
                code = "processing_callback_failure"
                retryable = False
            status = (
                self.retry_processing(claim, code, now=now)
                if retryable
                else self.fail_processing(claim, code, now=now)
            )
        return TerminalNotificationProcessingOutcome(
            status.notification_id,
            status.event_id,
            status.status,
            status.attempts,
            status.last_error_code,
        )

    def health(
        self,
        *,
        now: float | int | None = None,
    ) -> TerminalNotificationInboxHealth:
        self._owner()
        now = _time(time.time() if now is None else now)
        with self._lock:
            self._guard()
            self._validate_schema()
            try:
                integrity = self.connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
                foreign_keys = self.connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                rows = self.connection.execute(
                    "SELECT * FROM cms_terminal_notification_inbox "
                    "ORDER BY received_at, event_id"
                ).fetchall()
                processing_rows = self._processing_rows()
            except sqlite3.Error:
                _blocked("storage_unavailable", 503)
            if [tuple(row) for row in integrity] != [("ok",)] or foreign_keys:
                _blocked("integrity_failed", 503)
            for row in rows:
                self._validated_row(row)
            counts = {status: 0 for status in PROCESSING_STATUSES}
            for row, _notification_row in processing_rows:
                counts[row["status"]] += 1
            due = sum(
                row["status"] in {"pending", "retry_wait"}
                and float(row["next_attempt_at"]) <= now
                for row, _notification_row in processing_rows
            )
            expired = sum(
                row["status"] == "leased"
                and float(row["lease_expires_at"]) <= now
                for row, _notification_row in processing_rows
            )
            failed = counts["failed"]
            return TerminalNotificationInboxHealth(
                "blocked" if expired or failed else "ok",
                len(rows),
                counts,
                due,
                expired,
                failed,
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
            or path in {
                STATUS_PATH, READINESS_PATH, CAPABILITIES_PATH, HEALTH_PATH,
            }
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
