#!/usr/bin/env python3
"""Durably observe CMS processing after a terminal notification was accepted."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping


SCHEMA_VERSION = 1
STATUS_SCHEMA = "blun.cms-terminal-receiver-status-response.v1"
NOTIFICATION_SCHEMA = "blun.cms-source-terminal-notification.v1"
NOTIFICATION_FIELDS = {
    "schema", "notification_id", "event_id", "site_id", "plan_id",
    "website_version", "source_sequence", "job_count", "change_sha256",
    "lifecycle_binding_sha256", "terminal_status", "lifecycle_sha256",
}
STATUSES = ("pending", "leased", "watching", "retry_wait", "succeeded", "failed")
REMOTE_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
TERMINAL_STATUSES = {
    "cancelled", "deleted", "deletion_failed", "localization_failed",
    "publication_blocked", "publication_failed", "published", "superseded",
}
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_COLUMNS = (
    "event_id", "notification_id", "site_id", "terminal_status",
    "notification_sha256", "status", "poll_attempts", "failures",
    "max_failures", "next_poll_at", "lease_owner", "lease_token",
    "lease_expires_at", "last_error_code", "receiver_status",
    "receiver_attempts", "receiver_max_attempts", "receiver_next_attempt_at",
    "receiver_lease_expires_at", "receiver_lease_expired",
    "receiver_error_code", "receiver_processed_at", "created_at", "updated_at",
)


class TerminalProcessingMonitorBlocked(RuntimeError):
    """Stable, content-free processing-observation failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TerminalProcessingStatus:
    event_id: str
    notification_id: str
    site_id: str
    notification_sha256: str
    status: str
    poll_attempts: int
    failures: int
    max_failures: int
    next_poll_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    receiver_status: str | None
    receiver_attempts: int | None
    receiver_max_attempts: int | None
    receiver_error_code: str | None
    receiver_processed_at: float | None


@dataclass(frozen=True)
class TerminalProcessingOutcome:
    event_id: str
    notification_id: str
    status: str
    attempt: int
    error_code: str | None


@dataclass(frozen=True)
class TerminalProcessingHealth:
    status: str
    counts: dict[str, int]
    due: int
    expired_leases: int
    failed: int


def _blocked(code: str) -> None:
    raise TerminalProcessingMonitorBlocked("terminal_processing_monitor." + code)


def _token(value: Any) -> bool:
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _sha(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _error(value: Any) -> bool:
    return isinstance(value, str) and ERROR_CODE.fullmatch(value) is not None


def _canonical_sha256(value: Any) -> str:
    try:
        rendered = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        _blocked("notification_invalid")
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _time(value: Any, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(float(value)) or float(value) < 0
    ):
        _blocked("time_invalid")
    return float(value)


def _valid_time(value: Any, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return (
        not isinstance(value, bool) and isinstance(value, (int, float))
        and math.isfinite(float(value)) and float(value) >= 0
    )


def _duration(value: Any, code: str) -> float:
    result = _time(value)
    if result is None or not 0 < result <= 86_400:
        _blocked(code)
    return result


def _positive_integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        _blocked(code)
    return value


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        _blocked("transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableTerminalProcessingMonitor:
    """Poll exact receiver status with durable leases and bounded failures."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        poll_interval_seconds: float | int = 30,
        base_delay_seconds: float | int = 5,
        max_delay_seconds: float | int = 300,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.poll_interval_seconds = _duration(
            poll_interval_seconds, "poll_interval_invalid"
        )
        self.base_delay_seconds = _duration(base_delay_seconds, "delay_invalid")
        self.max_delay_seconds = _duration(max_delay_seconds, "delay_invalid")
        if self.base_delay_seconds > self.max_delay_seconds:
            _blocked("delay_invalid")
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_terminal_processing_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_source_terminal_processing_meta VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_terminal_processing (
                    event_id TEXT PRIMARY KEY, notification_id TEXT NOT NULL UNIQUE,
                    site_id TEXT NOT NULL, terminal_status TEXT NOT NULL,
                    notification_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'pending','leased','watching','retry_wait','succeeded','failed'
                    )), poll_attempts INTEGER NOT NULL CHECK (poll_attempts >= 0),
                    failures INTEGER NOT NULL CHECK (failures >= 0),
                    max_failures INTEGER NOT NULL CHECK (max_failures BETWEEN 1 AND 20),
                    next_poll_at REAL NOT NULL, lease_owner TEXT, lease_token TEXT,
                    lease_expires_at REAL, last_error_code TEXT, receiver_status TEXT,
                    receiver_attempts INTEGER, receiver_max_attempts INTEGER,
                    receiver_next_attempt_at REAL, receiver_lease_expires_at REAL,
                    receiver_lease_expired INTEGER, receiver_error_code TEXT,
                    receiver_processed_at REAL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK ((status = 'leased' AND lease_owner IS NOT NULL
                            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                           OR (status <> 'leased' AND lease_owner IS NULL
                               AND lease_token IS NULL AND lease_expires_at IS NULL))
                )
            """)
            self.connection.execute("""
                CREATE INDEX IF NOT EXISTS cms_source_terminal_processing_due
                ON cms_source_terminal_processing
                    (status, next_poll_at, created_at, event_id)
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_source_terminal_processing_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_source_terminal_processing)"
        ).fetchall())
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM cms_source_terminal_processing_meta"
        ).fetchall()
        if (
            meta_columns != ("singleton", "schema_version")
            or columns != _COLUMNS or len(meta) != 1
            or tuple(meta[0]) != (1, SCHEMA_VERSION)
        ):
            _blocked("schema_altered")

    def register(
        self,
        notification: Mapping[str, Any],
        notification_sha256: str,
        *,
        max_failures: int = 5,
        now: float | int,
    ) -> TerminalProcessingStatus:
        self._validate_schema()
        now = _time(now)
        max_failures = _positive_integer(max_failures, "max_failures_invalid")
        try:
            event_id = notification["event_id"]
            notification_id = notification["notification_id"]
            site_id = notification["site_id"]
            terminal_status = notification["terminal_status"]
        except (KeyError, TypeError):
            _blocked("notification_invalid")
        if (
            set(notification) != NOTIFICATION_FIELDS
            or notification.get("schema") != NOTIFICATION_SCHEMA
            or not all(
                _token(item) for item in (event_id, notification_id, site_id)
            )
            or terminal_status not in TERMINAL_STATUSES
            or not _sha(notification_sha256)
            or _canonical_sha256(notification) != notification_sha256
        ):
            _blocked("notification_invalid")
        values = (
            event_id, notification_id, site_id, terminal_status,
            notification_sha256, max_failures,
        )
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT notification_id, site_id, terminal_status, "
                "notification_sha256, max_failures "
                "FROM cms_source_terminal_processing WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                self.connection.execute("""
                    INSERT INTO cms_source_terminal_processing (
                        event_id, notification_id, site_id, terminal_status,
                        notification_sha256, status, poll_attempts, failures,
                        max_failures, next_poll_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?, ?, ?)
                """, (*values, now, now, now))
            elif tuple(row) != values[1:]:
                _blocked("idempotency_collision")
        return self.status(event_id, now=now)

    def claim(
        self,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> sqlite3.Row | None:
        self._validate_schema()
        if not _token(worker_id) or len(worker_id) > 128:
            _blocked("worker_invalid")
        now = _time(now)
        lease_seconds = _duration(lease_seconds, "lease_invalid")
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_source_terminal_processing
                SET status = CASE WHEN failures + 1 >= max_failures
                        THEN 'failed' ELSE 'retry_wait' END,
                    failures = failures + 1, next_poll_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'terminal_processing_monitor.lease_expired',
                    updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_source_terminal_processing
                WHERE status IN ('pending','watching','retry_wait')
                  AND next_poll_at <= ? AND failures < max_failures
                ORDER BY created_at, event_id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            self._validated_row(row)
            token = secrets.token_urlsafe(32)
            expires_at = now + lease_seconds
            updated = self.connection.execute("""
                UPDATE cms_source_terminal_processing
                SET status = 'leased', poll_attempts = poll_attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE event_id = ? AND status IN ('pending','watching','retry_wait')
            """, (worker_id, token, expires_at, now, row["event_id"]))
            if updated.rowcount != 1:
                _blocked("claim_lost")
            return self.connection.execute(
                "SELECT * FROM cms_source_terminal_processing WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone()

    def run_once(
        self,
        reader: Callable[[str, str], Any],
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> TerminalProcessingOutcome | None:
        if not callable(reader):
            _blocked("reader_invalid")
        now = _time(now)
        claim = self.claim(worker_id, now=now, lease_seconds=lease_seconds)
        if claim is None:
            return None
        try:
            response = reader(claim["event_id"], claim["site_id"])
            self._complete(claim, response, now)
        except TerminalProcessingMonitorBlocked:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            retryable = (
                getattr(error, "cms_notification_failure", False) is True
                and getattr(error, "retryable", None) is True
            )
            if not _error(code):
                code, retryable = "terminal_processing_monitor.reader_failure", False
            self._fail(claim, code, retryable, now)
        result = self.status(claim["event_id"], now=now)
        return TerminalProcessingOutcome(
            result.event_id, result.notification_id, result.status,
            result.poll_attempts, result.last_error_code,
        )

    def _complete(self, claim: sqlite3.Row, response: Any, now: float) -> None:
        if not isinstance(response, Mapping):
            self._fail(
                claim, "terminal_processing_monitor.response_invalid", False, now
            )
            return
        value = dict(response)
        fields = {
            "schema", "notification_id", "event_id", "site_id",
            "terminal_status", "notification_sha256", "processing_status",
            "attempts", "max_attempts", "next_attempt_at", "lease_expires_at",
            "lease_expired", "last_error_code", "processed_at",
            "capabilities_sha256",
        }
        remote = value.get("processing_status")
        attempts = value.get("attempts")
        maximum = value.get("max_attempts")
        error = value.get("last_error_code")
        valid = (
            set(value) == fields and value.get("schema") == STATUS_SCHEMA
            and value.get("notification_id") == claim["notification_id"]
            and value.get("event_id") == claim["event_id"]
            and value.get("site_id") == claim["site_id"]
            and value.get("terminal_status") == claim["terminal_status"]
            and value.get("notification_sha256") == claim["notification_sha256"]
            and remote in REMOTE_STATUSES
            and isinstance(attempts, int) and not isinstance(attempts, bool)
            and isinstance(maximum, int) and not isinstance(maximum, bool)
            and 0 <= attempts <= maximum <= 20 and maximum >= 1
            and _valid_time(value.get("next_attempt_at"))
            and _valid_time(value.get("lease_expires_at"), nullable=True)
        )
        valid = bool(valid) and (
            isinstance(value.get("lease_expired"), bool)
            and (error is None or _error(error))
            and _valid_time(value.get("processed_at"), nullable=True)
            and _sha(value.get("capabilities_sha256"))
            and (remote == "leased") == (value.get("lease_expires_at") is not None)
            and (remote == "succeeded") == (value.get("processed_at") is not None)
            and (error is None or remote in {"retry_wait", "failed"})
            and (not value.get("lease_expired") or remote == "leased")
        )
        if not valid:
            self._fail(
                claim, "terminal_processing_monitor.response_invalid", False, now
            )
            return
        status = "succeeded" if remote == "succeeded" else (
            "failed" if remote == "failed" else "watching"
        )
        local_error = (
            "terminal_processing_monitor.remote_failed"
            if remote == "failed" else None
        )
        with _transaction(self.connection):
            self._require_claim(claim, now)
            updated = self.connection.execute("""
                UPDATE cms_source_terminal_processing
                SET status = ?, failures = 0, next_poll_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, receiver_status = ?, receiver_attempts = ?,
                    receiver_max_attempts = ?, receiver_next_attempt_at = ?,
                    receiver_lease_expires_at = ?, receiver_lease_expired = ?,
                    receiver_error_code = ?, receiver_processed_at = ?, updated_at = ?
                WHERE event_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                status, now if status != "watching" else now + self.poll_interval_seconds,
                local_error, remote, attempts, maximum, value["next_attempt_at"],
                value["lease_expires_at"], int(value["lease_expired"]), error,
                value["processed_at"], now, claim["event_id"],
                claim["lease_owner"], claim["lease_token"],
            ))
            if updated.rowcount != 1:
                _blocked("completion_lost")

    def _fail(
        self, claim: sqlite3.Row, code: str, retryable: bool, now: float
    ) -> None:
        if not _error(code):
            code, retryable = "terminal_processing_monitor.reader_failure", False
        with _transaction(self.connection):
            row = self._require_claim(claim, now)
            failures = int(row["failures"]) + 1
            terminal = not retryable or failures >= int(row["max_failures"])
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** max(0, failures - 1)),
            )
            updated = self.connection.execute("""
                UPDATE cms_source_terminal_processing
                SET status = ?, failures = ?, next_poll_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, updated_at = ?
                WHERE event_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                "failed" if terminal else "retry_wait", failures,
                now if terminal else now + delay, code, now, claim["event_id"],
                claim["lease_owner"], claim["lease_token"],
            ))
            if updated.rowcount != 1:
                _blocked("failure_lost")

    def status(self, event_id: str, *, now: float | int) -> TerminalProcessingStatus:
        self._validate_schema()
        if not _token(event_id):
            _blocked("event_invalid")
        now = _time(now)
        row = self.connection.execute(
            "SELECT * FROM cms_source_terminal_processing WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            _blocked("missing")
        self._validated_row(row)
        lease = row["lease_expires_at"]
        return TerminalProcessingStatus(
            row["event_id"], row["notification_id"], row["site_id"],
            row["notification_sha256"], row["status"], int(row["poll_attempts"]),
            int(row["failures"]), int(row["max_failures"]),
            float(row["next_poll_at"]), None if lease is None else float(lease),
            row["status"] == "leased" and float(lease) <= now,
            row["last_error_code"], row["receiver_status"],
            row["receiver_attempts"], row["receiver_max_attempts"],
            row["receiver_error_code"], row["receiver_processed_at"],
        )

    def health(self, *, now: float | int) -> TerminalProcessingHealth:
        now = _time(now)
        self._validate_schema()
        if tuple(self.connection.execute("PRAGMA quick_check").fetchone()) != ("ok",):
            _blocked("database_integrity")
        rows = self.connection.execute(
            "SELECT * FROM cms_source_terminal_processing ORDER BY event_id"
        ).fetchall()
        for row in rows:
            self._validated_row(row)
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] += 1
        due = sum(
            row["status"] in {"pending", "watching", "retry_wait"}
            and row["next_poll_at"] <= now for row in rows
        )
        expired = sum(
            row["status"] == "leased" and row["lease_expires_at"] <= now
            for row in rows
        )
        return TerminalProcessingHealth(
            "blocked" if expired or counts["failed"] else "ok",
            counts, due, expired, counts["failed"],
        )

    def _require_claim(self, claim: sqlite3.Row, now: float) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM cms_source_terminal_processing WHERE event_id = ?",
            (claim["event_id"],),
        ).fetchone()
        if (
            row is None or row["status"] != "leased"
            or row["lease_owner"] != claim["lease_owner"]
            or row["lease_token"] != claim["lease_token"]
        ):
            _blocked("claim_lost")
        self._validated_row(row)
        if float(row["lease_expires_at"]) <= now:
            _blocked("lease_expired")
        return row

    @staticmethod
    def _validated_row(row: sqlite3.Row) -> None:
        status = row["status"]
        leased = status == "leased"
        receiver = row["receiver_status"]
        receiver_bundle = (
            row["receiver_attempts"], row["receiver_max_attempts"],
            row["receiver_next_attempt_at"], row["receiver_lease_expired"],
        )
        has_receiver = receiver is not None
        receiver_attempts_valid = (
            has_receiver
            and all(item is not None for item in receiver_bundle)
            and isinstance(row["receiver_attempts"], int)
            and not isinstance(row["receiver_attempts"], bool)
            and isinstance(row["receiver_max_attempts"], int)
            and not isinstance(row["receiver_max_attempts"], bool)
            and 0 <= row["receiver_attempts"] <= row["receiver_max_attempts"] <= 20
            and row["receiver_max_attempts"] >= 1
            and _valid_time(row["receiver_next_attempt_at"])
            and row["receiver_lease_expired"] in {0, 1}
        )
        if (
            not all(_token(row[name]) for name in (
                "event_id", "notification_id", "site_id"
            ))
            or row["terminal_status"] not in TERMINAL_STATUSES
            or not _sha(row["notification_sha256"])
            or status not in STATUSES
            or not all(
                isinstance(row[name], int) and not isinstance(row[name], bool)
                for name in ("poll_attempts", "failures", "max_failures")
            )
            or not 0 <= row["failures"] <= row["max_failures"] <= 20
            or row["max_failures"] < 1
            or row["poll_attempts"] < row["failures"]
            or leased != all(row[name] is not None for name in (
                "lease_owner", "lease_token", "lease_expires_at"
            ))
            or (not leased and any(row[name] is not None for name in (
                "lease_owner", "lease_token", "lease_expires_at"
            )))
            or (row["last_error_code"] is not None and not _error(
                row["last_error_code"]
            ))
            or (status in {"retry_wait", "failed"}) != (
                row["last_error_code"] is not None
            )
            or receiver is not None and receiver not in REMOTE_STATUSES
            or has_receiver != receiver_attempts_valid
            or (receiver == "leased") != (
                row["receiver_lease_expires_at"] is not None
            )
            or not _valid_time(
                row["receiver_lease_expires_at"], nullable=True,
            )
            or not _valid_time(row["receiver_processed_at"], nullable=True)
            or (row["receiver_error_code"] is not None and not _error(
                row["receiver_error_code"]
            ))
            or (receiver in {"retry_wait", "failed"}) != (
                row["receiver_error_code"] is not None
            )
            or (receiver == "succeeded") != (
                row["receiver_processed_at"] is not None
            )
            or (
                row["receiver_lease_expired"] == 1
                and receiver != "leased"
            )
            or (status == "succeeded") != (receiver == "succeeded")
            or (
                status == "watching"
                and receiver not in {"pending", "leased", "retry_wait"}
            )
            or (status == "failed" and row["last_error_code"] is None)
            or not _valid_time(row["next_poll_at"])
            or not _valid_time(row["created_at"])
            or not _valid_time(row["updated_at"])
            or row["updated_at"] < row["created_at"]
        ):
            _blocked("state_invalid")
