#!/usr/bin/env python3
"""Durable, content-free terminal notifications for a source CMS.

The localization lifecycle remains authoritative. This outbox copies only the
verified terminal binding and delivers it through a host-supplied callback.
Lost responses are replayed with the same notification identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping


SCHEMA_VERSION = 1
NOTIFICATION_SCHEMA = "blun.cms-source-terminal-notification.v1"
ACK_SCHEMA = "blun.cms-source-terminal-notification-ack.v1"
STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
TERMINAL_STATUSES = {
    "cancelled", "deleted", "deletion_failed", "localization_failed",
    "publication_blocked", "publication_failed", "published", "superseded",
}
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_COLUMNS = (
    "event_id", "notification_id", "payload_json", "payload_sha256",
    "status", "attempts", "max_attempts", "next_attempt_at",
    "lease_owner", "lease_token", "lease_expires_at", "last_error_code",
    "ack_sha256", "created_at", "updated_at",
)


class TerminalNotificationBlocked(RuntimeError):
    """Stable notification failure without customer content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TerminalNotificationFailure(RuntimeError):
    """Public callback failure contract used to request a bounded retry."""

    cms_notification_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not _error(code) or not isinstance(retryable, bool):
            raise ValueError("invalid terminal notification failure")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class TerminalNotificationClaim:
    event_id: str
    notification_id: str
    payload_sha256: str
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float
    payload_json: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class TerminalNotificationStatus:
    event_id: str
    notification_id: str
    payload_sha256: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    ack_sha256: str | None


@dataclass(frozen=True)
class TerminalNotificationOutcome:
    event_id: str
    notification_id: str
    status: str
    attempt: int
    error_code: str | None


@dataclass(frozen=True)
class TerminalNotificationHealth:
    status: str
    counts: dict[str, int]
    due: int
    expired_leases: int
    failed: int


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise TerminalNotificationBlocked("terminal_notification.payload_invalid") from None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token(value: Any, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str) or not 1 <= len(value) <= limit
        or any(
            not (
                "A" <= character <= "Z" or "a" <= character <= "z"
                or "0" <= character <= "9" or character in "_.:-"
            )
            for character in value
        )
    ):
        raise TerminalNotificationBlocked("terminal_notification.payload_invalid")
    return value


def _sha(value: Any) -> str:
    if (
        not isinstance(value, str) or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TerminalNotificationBlocked("terminal_notification.payload_invalid")
    return value


def _error(value: Any) -> bool:
    return isinstance(value, str) and ERROR_CODE.fullmatch(value) is not None


def _time(value: Any) -> float:
    if (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(float(value)) or float(value) < 0
    ):
        raise TerminalNotificationBlocked("terminal_notification.time_invalid")
    return float(value)


def _duration(value: Any, code: str) -> float:
    result = _time(value)
    if not 0 < result <= 86_400:
        raise TerminalNotificationBlocked(code)
    return result


def _payload(lifecycle: Any) -> tuple[str, str, str]:
    def value(name: str) -> Any:
        return (
            lifecycle.get(name)
            if isinstance(lifecycle, Mapping)
            else getattr(lifecycle, name, None)
        )

    if (
        value("state") != "terminal"
        or value("remote_status") not in TERMINAL_STATUSES
    ):
        raise TerminalNotificationBlocked("terminal_notification.lifecycle_invalid")
    event_id = _token(value("event_id"))
    lifecycle_sha256 = value("lifecycle_sha256")
    if lifecycle_sha256 is not None:
        lifecycle_sha256 = _sha(lifecycle_sha256)
    if (value("remote_status") in {"cancelled", "superseded"}) != (
        lifecycle_sha256 is None
    ):
        raise TerminalNotificationBlocked("terminal_notification.lifecycle_invalid")
    content = {
        "schema": NOTIFICATION_SCHEMA,
        "event_id": event_id,
        "site_id": _token(value("site_id")),
        "plan_id": _token(value("plan_id")),
        "website_version": _token(value("website_version")),
        "source_sequence": value("source_sequence"),
        "job_count": value("job_count"),
        "change_sha256": _sha(value("change_sha256")),
        "lifecycle_binding_sha256": _sha(value("binding_sha256")),
        "terminal_status": value("remote_status"),
        "lifecycle_sha256": lifecycle_sha256,
    }
    for name in ("source_sequence", "job_count"):
        item = content[name]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise TerminalNotificationBlocked("terminal_notification.lifecycle_invalid")
    identity = _hash(_canonical(content))
    content["notification_id"] = "terminal-" + identity
    rendered = _canonical(content)
    return event_id, rendered, _hash(rendered)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise TerminalNotificationBlocked("terminal_notification.transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableCMSTerminalNotifier:
    """Register and deliver exact terminal lifecycle evidence."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        base_delay_seconds: float | int = 5,
        max_delay_seconds: float | int = 300,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.base_delay_seconds = _duration(
            base_delay_seconds, "terminal_notification.delay_invalid"
        )
        self.max_delay_seconds = _duration(
            max_delay_seconds, "terminal_notification.delay_invalid"
        )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise TerminalNotificationBlocked("terminal_notification.delay_invalid")
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_terminal_notification_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_source_terminal_notification_meta
                VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_terminal_notification (
                    event_id TEXT PRIMARY KEY, notification_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'leased', 'retry_wait',
                                   'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
                    next_attempt_at REAL NOT NULL, lease_owner TEXT, lease_token TEXT,
                    lease_expires_at REAL, last_error_code TEXT, ack_sha256 TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    CHECK (
                        (status = 'leased' AND lease_owner IS NOT NULL
                         AND lease_token IS NOT NULL
                         AND lease_expires_at IS NOT NULL)
                        OR
                        (status <> 'leased' AND lease_owner IS NULL
                         AND lease_token IS NULL
                         AND lease_expires_at IS NULL)
                    )
                )
            """)
            self.connection.execute("""
                CREATE INDEX IF NOT EXISTS cms_source_terminal_notification_due
                ON cms_source_terminal_notification (status, next_attempt_at, created_at, event_id)
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_source_terminal_notification_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_source_terminal_notification)"
        ).fetchall())
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM cms_source_terminal_notification_meta"
        ).fetchall()
        if (
            meta_columns != ("singleton", "schema_version")
            or columns != _COLUMNS or len(meta) != 1
            or tuple(meta[0]) != (1, SCHEMA_VERSION)
        ):
            raise TerminalNotificationBlocked("terminal_notification.schema_altered")

    def register(
        self,
        lifecycle: Any,
        *,
        max_attempts: int = 5,
        now: float | int,
    ) -> TerminalNotificationStatus:
        self._validate_schema()
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 20
        ):
            raise TerminalNotificationBlocked(
                "terminal_notification.max_attempts_invalid"
            )
        now = _time(now)
        event_id, payload_json, payload_sha256 = _payload(lifecycle)
        notification_id = json.loads(payload_json)["notification_id"]
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT payload_sha256, max_attempts "
                "FROM cms_source_terminal_notification WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["payload_sha256"] != payload_sha256
                    or row["max_attempts"] != max_attempts
                ):
                    raise TerminalNotificationBlocked(
                        "terminal_notification.idempotency_collision"
                    )
            else:
                self.connection.execute("""
                    INSERT INTO cms_source_terminal_notification (
                        event_id, notification_id, payload_json, payload_sha256, status,
                        attempts, max_attempts, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (event_id, notification_id, payload_json, payload_sha256,
                      max_attempts, now, now, now))
        return self.status(event_id, now=now)

    def claim(
        self,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> TerminalNotificationClaim | None:
        self._validate_schema()
        worker_id = _token(worker_id, limit=128)
        now = _time(now)
        lease_seconds = _duration(
            lease_seconds, "terminal_notification.lease_invalid"
        )
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_source_terminal_notification
                SET status = CASE WHEN attempts >= max_attempts
                    THEN 'failed' ELSE 'retry_wait' END, next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'lease_expired',
                    updated_at = ? WHERE status = 'leased' AND lease_expires_at <= ?
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_source_terminal_notification
                WHERE status IN ('pending','retry_wait') AND next_attempt_at <= ?
                  AND attempts < max_attempts ORDER BY created_at, event_id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            self._validated_row(row)
            token = secrets.token_urlsafe(32)
            expires = now + lease_seconds
            updated = self.connection.execute("""
                UPDATE cms_source_terminal_notification
                SET status = 'leased', attempts = attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE event_id = ? AND status IN ('pending', 'retry_wait')
            """, (worker_id, token, expires, now, row["event_id"]))
            if updated.rowcount != 1:
                raise TerminalNotificationBlocked(
                    "terminal_notification.claim_lost"
                )
            return TerminalNotificationClaim(
                row["event_id"], row["notification_id"], row["payload_sha256"],
                int(row["attempts"]) + 1, int(row["max_attempts"]), worker_id,
                token, expires, row["payload_json"],
            )

    def run_once(
        self,
        callback: Callable[[Mapping[str, Any]], Any],
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> TerminalNotificationOutcome | None:
        if not callable(callback):
            raise TerminalNotificationBlocked("terminal_notification.callback_invalid")
        claim = self.claim(worker_id, now=now, lease_seconds=lease_seconds)
        if claim is None:
            return None
        try:
            response = callback(json.loads(claim.payload_json))
            status = self.complete(claim, response, now=now)
        except TerminalNotificationBlocked as error:
            if error.code != "terminal_notification.response_invalid":
                raise
            status = self.fail(claim, error.code, now=now)
        except Exception as error:
            retryable = (
                getattr(error, "cms_notification_failure", False) is True
                and getattr(error, "retryable", None) is True
            )
            code = getattr(error, "code", None)
            if not _error(code):
                code, retryable = "terminal_notification.callback_failure", False
            status = (
                self.retry(claim, code, now=now)
                if retryable
                else self.fail(claim, code, now=now)
            )
        return TerminalNotificationOutcome(
            status.event_id, status.notification_id, status.status,
            status.attempts, status.last_error_code,
        )

    def complete(
        self,
        claim: TerminalNotificationClaim,
        response: Any,
        *,
        now: float | int,
    ) -> TerminalNotificationStatus:
        now = _time(now)
        if not isinstance(response, Mapping):
            raise TerminalNotificationBlocked("terminal_notification.response_invalid")
        try:
            copied = json.loads(_canonical(dict(response)))
        except TerminalNotificationBlocked:
            raise TerminalNotificationBlocked(
                "terminal_notification.response_invalid"
            ) from None
        expected = {
            "schema": ACK_SCHEMA, "notification_id": claim.notification_id,
            "event_id": claim.event_id,
            "site_id": json.loads(claim.payload_json)["site_id"],
            "status": "accepted", "notification_sha256": claim.payload_sha256,
        }
        if copied != expected:
            raise TerminalNotificationBlocked("terminal_notification.response_invalid")
        ack_json = _canonical(copied)
        with _transaction(self.connection):
            self._require_claim(claim, now)
            updated = self.connection.execute("""
                UPDATE cms_source_terminal_notification
                SET status = 'succeeded', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = NULL, ack_sha256 = ?, updated_at = ?
                WHERE event_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                now, _hash(ack_json), now, claim.event_id,
                claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise TerminalNotificationBlocked("terminal_notification.completion_lost")
        return self.status(claim.event_id, now=now)

    def retry(
        self, claim: TerminalNotificationClaim, code: str, *, now: float | int,
    ) -> TerminalNotificationStatus:
        return self._finish(claim, code, retryable=True, now=now)

    def fail(
        self, claim: TerminalNotificationClaim, code: str, *, now: float | int,
    ) -> TerminalNotificationStatus:
        return self._finish(claim, code, retryable=False, now=now)

    def _finish(
        self,
        claim: TerminalNotificationClaim,
        code: str,
        *,
        retryable: bool,
        now: float | int,
    ) -> TerminalNotificationStatus:
        if not _error(code):
            raise TerminalNotificationBlocked("terminal_notification.error_invalid")
        now = _time(now)
        with _transaction(self.connection):
            row = self._require_claim(claim, now)
            terminal = not retryable or int(row["attempts"]) >= int(
                row["max_attempts"]
            )
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** max(0, claim.attempt - 1)),
            )
            updated = self.connection.execute("""
                UPDATE cms_source_terminal_notification SET status = ?, next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, updated_at = ?
                WHERE event_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                "failed" if terminal else "retry_wait",
                now if terminal else now + delay,
                code, now, claim.event_id, claim.lease_owner,
                claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise TerminalNotificationBlocked(
                    "terminal_notification.failure_lost"
                )
        return self.status(claim.event_id, now=now)

    def status(
        self, event_id: str, *, now: float | int,
    ) -> TerminalNotificationStatus:
        self._validate_schema()
        event_id = _token(event_id)
        now = _time(now)
        row = self.connection.execute(
            "SELECT * FROM cms_source_terminal_notification WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise TerminalNotificationBlocked("terminal_notification.missing")
        self._validated_row(row)
        lease = row["lease_expires_at"]
        return TerminalNotificationStatus(
            event_id, row["notification_id"], row["payload_sha256"], row["status"],
            int(row["attempts"]), int(row["max_attempts"]), float(row["next_attempt_at"]),
            None if lease is None else float(lease),
            row["status"] == "leased" and float(lease) <= now,
            row["last_error_code"], row["ack_sha256"],
        )

    def health(self, *, now: float | int) -> TerminalNotificationHealth:
        now = _time(now)
        self._validate_schema()
        quick = self.connection.execute("PRAGMA quick_check").fetchall()
        if len(quick) != 1 or tuple(quick[0]) != ("ok",):
            raise TerminalNotificationBlocked(
                "terminal_notification.database_integrity"
            )
        rows = self.connection.execute(
            "SELECT * FROM cms_source_terminal_notification ORDER BY event_id"
        ).fetchall()
        for row in rows:
            self._validated_row(row)
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] += 1
        due = sum(
            row["status"] in {"pending", "retry_wait"}
            and row["next_attempt_at"] <= now
            for row in rows
        )
        expired = sum(
            row["status"] == "leased" and row["lease_expires_at"] <= now
            for row in rows
        )
        return TerminalNotificationHealth(
            "blocked" if expired or counts["failed"] else "ok", counts, due,
            expired, counts["failed"],
        )

    def _require_claim(
        self, claim: TerminalNotificationClaim, now: float,
    ) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM cms_source_terminal_notification WHERE event_id = ?",
            (claim.event_id,),
        ).fetchone()
        if (
            row is None or row["status"] != "leased"
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
        ):
            raise TerminalNotificationBlocked("terminal_notification.claim_lost")
        self._validated_row(row)
        if float(row["lease_expires_at"]) <= now:
            raise TerminalNotificationBlocked("terminal_notification.lease_expired")
        return row

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            payload = json.loads(row["payload_json"])
            lifecycle = dict(payload)
            lifecycle.pop("notification_id")
            expected_id = "terminal-" + _hash(_canonical(lifecycle))
            lifecycle_sha256 = payload.get("lifecycle_sha256")
            terminal_status = payload.get("terminal_status")
            semantic_valid = (
                _token(payload.get("event_id")) == payload.get("event_id")
                and _token(payload.get("site_id")) == payload.get("site_id")
                and _token(payload.get("plan_id")) == payload.get("plan_id")
                and _token(payload.get("website_version"))
                == payload.get("website_version")
                and isinstance(payload.get("source_sequence"), int)
                and not isinstance(payload.get("source_sequence"), bool)
                and payload["source_sequence"] > 0
                and isinstance(payload.get("job_count"), int)
                and not isinstance(payload.get("job_count"), bool)
                and payload["job_count"] > 0
                and _sha(payload.get("change_sha256"))
                == payload.get("change_sha256")
                and _sha(payload.get("lifecycle_binding_sha256"))
                == payload.get("lifecycle_binding_sha256")
                and terminal_status in TERMINAL_STATUSES
                and (
                    terminal_status in {"cancelled", "superseded"}
                    and lifecycle_sha256 is None
                    or terminal_status not in {"cancelled", "superseded"}
                    and _sha(lifecycle_sha256) == lifecycle_sha256
                )
            )
            valid_payload = (
                payload.get("schema") == NOTIFICATION_SCHEMA
                and set(payload) == {
                    "schema", "notification_id", "event_id", "site_id", "plan_id",
                    "website_version", "source_sequence", "job_count", "change_sha256",
                    "lifecycle_binding_sha256", "terminal_status", "lifecycle_sha256",
                }
                and payload["event_id"] == row["event_id"]
                and payload["notification_id"] == row["notification_id"] == expected_id
                and _hash(row["payload_json"]) == row["payload_sha256"]
                and semantic_valid
            )
        except Exception:
            valid_payload = False
        status = row["status"]
        leased = status == "leased"
        lease_values = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        succeeded = status == "succeeded"
        error = row["last_error_code"]
        expected_ack_sha256 = None
        if valid_payload:
            expected_ack_sha256 = _hash(_canonical({
                "schema": ACK_SCHEMA,
                "notification_id": payload["notification_id"],
                "event_id": payload["event_id"],
                "site_id": payload["site_id"],
                "status": "accepted",
                "notification_sha256": row["payload_sha256"],
            }))
        if (
            not valid_payload or status not in STATUSES
            or isinstance(row["attempts"], bool) or not isinstance(row["attempts"], int)
            or not 0 <= row["attempts"] <= row["max_attempts"] <= 20
            or row["max_attempts"] < 1
            or leased != all(item is not None for item in lease_values)
            or (not leased and any(item is not None for item in lease_values))
            or (status in {"retry_wait", "failed"}) != (error is not None)
            or (error is not None and not _error(error))
            or succeeded != (row["ack_sha256"] is not None)
            or (
                row["ack_sha256"] is not None
                and row["ack_sha256"] != expected_ack_sha256
            )
            or _time(row["next_attempt_at"]) < _time(row["created_at"])
            or _time(row["updated_at"]) < _time(row["created_at"])
        ):
            raise TerminalNotificationBlocked("terminal_notification.state_invalid")
