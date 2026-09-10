#!/usr/bin/env python3
"""Durable source-side outbox for CMS localization change events.

The website host owns the SQLite connection and the HTTPS client. One leased
attempt submits one immutable event. A crash after remote acceptance safely
replays the same event identity after lease expiry; the server remains the
idempotency authority.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
MAX_DELAY_SECONDS = 86_400.0
STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_COLUMNS = (
    "event_id", "payload_json", "payload_sha256", "status", "attempts",
    "max_attempts", "next_attempt_at", "lease_owner", "lease_token",
    "lease_expires_at", "last_error_code", "last_error_detail_hash",
    "remote_plan_id", "remote_job_count", "remote_status",
    "response_sha256", "created_at", "updated_at",
)
_META_COLUMNS = ("singleton", "schema_version")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load CMS dispatch dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CLIENT = _load_module(
    "blun_website_localization_cms_dispatch_client",
    _ROOT / "integrations" / "website_localization_cms_client.py",
)


class CMSDispatchBlocked(RuntimeError):
    """Stable, content-free failure of the durable source dispatcher."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("CMS dispatch error code is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ChangeDispatchClaim:
    event_id: str
    payload_sha256: str
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float
    payload_json: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class ChangeDispatchStatus:
    event_id: str
    payload_sha256: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    last_error_detail_hash: str | None
    remote_plan_id: str | None
    remote_job_count: int | None
    remote_status: str | None
    response_sha256: str | None


@dataclass(frozen=True)
class ChangeDispatchOutcome:
    event_id: str
    status: str
    attempt: int
    max_attempts: int
    next_attempt_at: float
    error_code: str | None


@dataclass(frozen=True)
class ChangeDispatchHealth:
    status: str
    counts: dict[str, int]
    due: int
    expired_leases: int
    failed: int


def _token(value: Any, code: str, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or TOKEN.fullmatch(value) is None
    ):
        raise CMSDispatchBlocked(code)
    return value


def _timestamp(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CMSDispatchBlocked(code)
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise CMSDispatchBlocked(code)
    return result


def _duration(value: Any, code: str, *, maximum: float) -> float:
    result = _timestamp(value, code)
    if result <= 0 or result > maximum:
        raise CMSDispatchBlocked(code)
    return result


def _canonical_json(value: Any, code: str) -> str:
    try:
        result = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise CMSDispatchBlocked(code) from None
    if not result or len(result.encode("utf-8")) > _CLIENT._API.MAX_MESSAGE_BYTES:
        raise CMSDispatchBlocked(code)
    return result


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _change(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise CMSDispatchBlocked("dispatch.change_invalid")
    payload_json = _canonical_json(dict(value), "dispatch.change_invalid")
    try:
        copied = json.loads(payload_json)
    except (json.JSONDecodeError, RecursionError):
        raise CMSDispatchBlocked("dispatch.change_invalid") from None
    if (
        not isinstance(copied, dict)
        or copied.get("schema") != _CLIENT._CMS.CHANGE_SCHEMA
    ):
        raise CMSDispatchBlocked("dispatch.change_invalid")
    try:
        _CLIENT._CMS.WebsiteLocalizationCMSBridge._validated_event(None, copied)
    except Exception:
        raise CMSDispatchBlocked("dispatch.change_invalid") from None
    event_id = _token(copied.get("event_id"), "dispatch.change_invalid")
    return event_id, payload_json, _hash(payload_json)


def _error(code: Any, detail_hash: Any = None) -> tuple[str, str | None]:
    if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
        raise CMSDispatchBlocked("dispatch.error_invalid")
    if detail_hash is not None and (
        not isinstance(detail_hash, str) or SHA256.fullmatch(detail_hash) is None
    ):
        raise CMSDispatchBlocked("dispatch.error_invalid")
    return code, detail_hash


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise CMSDispatchBlocked("dispatch.transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableCMSChangeDispatcher:
    """Persist, lease, and submit exact CMS change events at least once."""

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
            base_delay_seconds, "dispatch.delay_invalid",
            maximum=MAX_DELAY_SECONDS,
        )
        self.max_delay_seconds = _duration(
            max_delay_seconds, "dispatch.delay_invalid",
            maximum=MAX_DELAY_SECONDS,
        )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise CMSDispatchBlocked("dispatch.delay_invalid")
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_change_outbox_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL
                        CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_source_change_outbox_meta
                VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_change_outbox (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'leased', 'retry_wait',
                                   'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (
                        max_attempts >= 1 AND max_attempts <= 20
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_error_detail_hash TEXT,
                    remote_plan_id TEXT,
                    remote_job_count INTEGER,
                    remote_status TEXT,
                    response_sha256 TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
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
                CREATE INDEX IF NOT EXISTS cms_source_change_outbox_due
                ON cms_source_change_outbox (
                    status, next_attempt_at, created_at, event_id
                )
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_change_outbox_meta)"
            ).fetchall()
        )
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM cms_source_change_outbox_meta"
        ).fetchall()
        columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_change_outbox)"
            ).fetchall()
        )
        if (
            meta_columns != _META_COLUMNS
            or len(meta) != 1
            or tuple(meta[0]) != (1, SCHEMA_VERSION)
            or columns != _COLUMNS
        ):
            raise CMSDispatchBlocked("dispatch.schema_altered")

    def enqueue(
        self,
        change: Mapping[str, Any],
        *,
        max_attempts: int = 5,
        now: float | int,
    ) -> ChangeDispatchStatus:
        self._validate_schema()
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise CMSDispatchBlocked("dispatch.max_attempts_invalid")
        now = _timestamp(now, "dispatch.time_invalid")
        event_id, payload_json, payload_sha256 = _change(change)
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT payload_sha256, max_attempts FROM cms_source_change_outbox "
                "WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["payload_sha256"] != payload_sha256
                    or row["max_attempts"] != max_attempts
                ):
                    raise CMSDispatchBlocked("dispatch.idempotency_collision")
            else:
                self.connection.execute("""
                    INSERT INTO cms_source_change_outbox (
                        event_id, payload_json, payload_sha256, status,
                        attempts, max_attempts, next_attempt_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (
                    event_id, payload_json, payload_sha256, max_attempts,
                    now, now, now,
                ))
        return self.status(event_id, now=now)

    def claim(
        self,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> ChangeDispatchClaim | None:
        self._validate_schema()
        worker_id = _token(worker_id, "dispatch.worker_invalid", limit=128)
        now = _timestamp(now, "dispatch.time_invalid")
        lease_seconds = _duration(
            lease_seconds, "dispatch.lease_invalid", maximum=MAX_LEASE_SECONDS,
        )
        claim = None
        integrity_error = None
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_source_change_outbox
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'lease_expired',
                    last_error_detail_hash = NULL, updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts >= max_attempts
            """, (now, now))
            self.connection.execute("""
                UPDATE cms_source_change_outbox
                SET status = 'retry_wait', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'lease_expired',
                    last_error_detail_hash = NULL, updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts < max_attempts
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_source_change_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND next_attempt_at <= ? AND attempts < max_attempts
                ORDER BY created_at, event_id LIMIT 1
            """, (now,)).fetchone()
            if row is not None:
                try:
                    self._validated_row(row)
                except CMSDispatchBlocked:
                    self.connection.execute("""
                        UPDATE cms_source_change_outbox
                        SET status = 'failed', last_error_code =
                            'dispatch.payload_integrity', updated_at = ?
                        WHERE event_id = ?
                    """, (now, row["event_id"]))
                    integrity_error = "dispatch.payload_integrity"
                else:
                    lease_token = secrets.token_urlsafe(32)
                    lease_expires_at = now + lease_seconds
                    updated = self.connection.execute("""
                        UPDATE cms_source_change_outbox
                        SET status = 'leased', attempts = attempts + 1,
                            lease_owner = ?, lease_token = ?,
                            lease_expires_at = ?, last_error_code = NULL,
                            last_error_detail_hash = NULL, updated_at = ?
                        WHERE event_id = ?
                          AND status IN ('pending', 'retry_wait')
                    """, (
                        worker_id, lease_token, lease_expires_at, now,
                        row["event_id"],
                    ))
                    if updated.rowcount != 1:
                        raise CMSDispatchBlocked("dispatch.claim_lost")
                    claim = ChangeDispatchClaim(
                        event_id=row["event_id"],
                        payload_sha256=row["payload_sha256"],
                        attempt=int(row["attempts"]) + 1,
                        max_attempts=int(row["max_attempts"]),
                        lease_owner=worker_id,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        payload_json=row["payload_json"],
                    )
        if integrity_error is not None:
            raise CMSDispatchBlocked(integrity_error)
        return claim

    def run_once(
        self,
        client: Any,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> ChangeDispatchOutcome | None:
        submit = getattr(client, "submit_change", None)
        if not callable(submit):
            raise CMSDispatchBlocked("dispatch.client_invalid")
        lease_seconds = _duration(
            lease_seconds, "dispatch.lease_invalid", maximum=MAX_LEASE_SECONDS,
        )
        timeout = getattr(client, "timeout", None)
        if (
            timeout is not None
            and (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or timeout <= 0
            )
        ):
            raise CMSDispatchBlocked("dispatch.client_invalid")
        if timeout is not None and lease_seconds <= float(timeout):
            raise CMSDispatchBlocked("dispatch.lease_too_short")
        claim = self.claim(
            worker_id, now=now, lease_seconds=lease_seconds,
        )
        if claim is None:
            return None
        try:
            response = submit(json.loads(claim.payload_json))
            status = self.complete(claim, response, now=now)
        except CMSDispatchBlocked as error:
            if error.code != "dispatch.response_invalid":
                raise
            status = self.fail(claim, error.code, now=now)
        except Exception as error:
            if getattr(error, "cms_client_failure", False) is True:
                code = getattr(error, "code", None)
                retryable = getattr(error, "retryable", None)
                try:
                    code, _ = _error(code)
                except CMSDispatchBlocked:
                    code, retryable = "dispatch.client_failure", False
                if retryable is True:
                    status = self.retry(claim, code, now=now)
                else:
                    status = self.fail(claim, code, now=now)
            else:
                status = self.fail(
                    claim, "dispatch.client_failure", now=now,
                )
        return ChangeDispatchOutcome(
            event_id=status.event_id,
            status=status.status,
            attempt=status.attempts,
            max_attempts=status.max_attempts,
            next_attempt_at=status.next_attempt_at,
            error_code=status.last_error_code,
        )

    def complete(
        self,
        claim: ChangeDispatchClaim,
        response: Any,
        *,
        now: float | int,
    ) -> ChangeDispatchStatus:
        self._validate_schema()
        claim = self._claim(claim)
        now = _timestamp(now, "dispatch.time_invalid")
        if not isinstance(response, Mapping):
            raise CMSDispatchBlocked("dispatch.response_invalid")
        response_json = _canonical_json(
            dict(response), "dispatch.response_invalid",
        )
        copied = json.loads(response_json)
        if (
            set(copied) != {
                "schema", "event_id", "plan_id", "job_count",
                "inserted_jobs", "status",
            }
            or copied.get("schema") != _CLIENT._API.API_SCHEMA
            or copied.get("event_id") != claim.event_id
            or not isinstance(copied.get("plan_id"), str)
            or TOKEN.fullmatch(copied["plan_id"]) is None
            or isinstance(copied.get("job_count"), bool)
            or not isinstance(copied.get("job_count"), int)
            or copied["job_count"] <= 0
            or isinstance(copied.get("inserted_jobs"), bool)
            or not isinstance(copied.get("inserted_jobs"), int)
            or not 0 <= copied["inserted_jobs"] <= copied["job_count"]
            or copied.get("status") not in {"enqueued", "superseded", "cancelled"}
        ):
            raise CMSDispatchBlocked("dispatch.response_invalid")
        with _transaction(self.connection):
            self._require_live_claim(claim, now)
            updated = self.connection.execute("""
                UPDATE cms_source_change_outbox
                SET status = 'succeeded', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    last_error_detail_hash = NULL, remote_plan_id = ?,
                    remote_job_count = ?, remote_status = ?,
                    response_sha256 = ?, updated_at = ?
                WHERE event_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                now, copied["plan_id"], copied["job_count"], copied["status"],
                _hash(response_json), now, claim.event_id,
                claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise CMSDispatchBlocked("dispatch.completion_lost")
        return self.status(claim.event_id, now=now)

    def retry(
        self,
        claim: ChangeDispatchClaim,
        error_code: str,
        *,
        now: float | int,
    ) -> ChangeDispatchStatus:
        self._validate_schema()
        claim = self._claim(claim)
        error_code, detail_hash = _error(error_code)
        now = _timestamp(now, "dispatch.time_invalid")
        with _transaction(self.connection):
            row = self._require_live_claim(claim, now)
            terminal = int(row["attempts"]) >= int(row["max_attempts"])
            status = "failed" if terminal else "retry_wait"
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** max(0, claim.attempt - 1)),
            )
            next_attempt_at = now if terminal else now + delay
            self._finish_failure(
                claim, status, error_code, detail_hash, next_attempt_at, now,
            )
        return self.status(claim.event_id, now=now)

    def fail(
        self,
        claim: ChangeDispatchClaim,
        error_code: str,
        *,
        now: float | int,
    ) -> ChangeDispatchStatus:
        self._validate_schema()
        claim = self._claim(claim)
        error_code, detail_hash = _error(error_code)
        now = _timestamp(now, "dispatch.time_invalid")
        with _transaction(self.connection):
            self._require_live_claim(claim, now)
            self._finish_failure(
                claim, "failed", error_code, detail_hash, now, now,
            )
        return self.status(claim.event_id, now=now)

    def _finish_failure(
        self,
        claim: ChangeDispatchClaim,
        status: str,
        error_code: str,
        detail_hash: str | None,
        next_attempt_at: float,
        now: float,
    ) -> None:
        updated = self.connection.execute("""
            UPDATE cms_source_change_outbox
            SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL,
                last_error_code = ?, last_error_detail_hash = ?,
                updated_at = ?
            WHERE event_id = ? AND status = 'leased'
              AND lease_owner = ? AND lease_token = ?
        """, (
            status, next_attempt_at, error_code, detail_hash, now,
            claim.event_id, claim.lease_owner, claim.lease_token,
        ))
        if updated.rowcount != 1:
            raise CMSDispatchBlocked("dispatch.failure_lost")

    def status(
        self, event_id: str, *, now: float | int,
    ) -> ChangeDispatchStatus:
        self._validate_schema()
        event_id = _token(event_id, "dispatch.event_invalid")
        now = _timestamp(now, "dispatch.time_invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_source_change_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise CMSDispatchBlocked("dispatch.event_missing")
        self._validated_row(row)
        lease_expires_at = row["lease_expires_at"]
        return ChangeDispatchStatus(
            event_id=row["event_id"],
            payload_sha256=row["payload_sha256"],
            status=row["status"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            lease_expires_at=(
                None if lease_expires_at is None else float(lease_expires_at)
            ),
            lease_expired=(
                row["status"] == "leased"
                and float(lease_expires_at) <= now
            ),
            last_error_code=row["last_error_code"],
            last_error_detail_hash=row["last_error_detail_hash"],
            remote_plan_id=row["remote_plan_id"],
            remote_job_count=row["remote_job_count"],
            remote_status=row["remote_status"],
            response_sha256=row["response_sha256"],
        )

    def health(self, *, now: float | int) -> ChangeDispatchHealth:
        now = _timestamp(now, "dispatch.time_invalid")
        self._validate_schema()
        quick = self.connection.execute("PRAGMA quick_check").fetchall()
        if len(quick) != 1 or tuple(quick[0]) != ("ok",):
            raise CMSDispatchBlocked("dispatch.database_integrity")
        rows = self.connection.execute(
            "SELECT * FROM cms_source_change_outbox ORDER BY event_id"
        ).fetchall()
        for row in rows:
            self._validated_row(row)
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] += 1
        due = sum(
            1 for row in rows
            if row["status"] in {"pending", "retry_wait"}
            and row["next_attempt_at"] <= now
        )
        expired = sum(
            1 for row in rows
            if row["status"] == "leased" and row["lease_expires_at"] <= now
        )
        failed = counts["failed"]
        return ChangeDispatchHealth(
            status="blocked" if expired or failed else "ok",
            counts=counts,
            due=due,
            expired_leases=expired,
            failed=failed,
        )

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            event_id, payload_json, payload_sha256 = _change(
                json.loads(row["payload_json"])
            )
            status = row["status"]
            attempts = row["attempts"]
            maximum = row["max_attempts"]
            next_attempt_at = _timestamp(
                row["next_attempt_at"], "dispatch.state_invalid",
            )
            created_at = _timestamp(
                row["created_at"], "dispatch.state_invalid",
            )
            updated_at = _timestamp(
                row["updated_at"], "dispatch.state_invalid",
            )
        except (json.JSONDecodeError, TypeError, CMSDispatchBlocked):
            raise CMSDispatchBlocked("dispatch.state_invalid") from None
        leases = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        leased = status == "leased"
        remote = (
            row["remote_plan_id"], row["remote_job_count"],
            row["remote_status"], row["response_sha256"],
        )
        error = row["last_error_code"]
        if (
            event_id != row["event_id"]
            or payload_json != row["payload_json"]
            or payload_sha256 != row["payload_sha256"]
            or status not in STATUSES
            or isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 0 <= attempts <= maximum <= MAX_ATTEMPTS
            or maximum < 1
            or next_attempt_at < created_at
            or updated_at < created_at
            or leased != all(item is not None for item in leases)
            or (not leased and any(item is not None for item in leases))
            or (leased and (
                _token(leases[0], "dispatch.state_invalid", limit=128) != leases[0]
                or _token(leases[1], "dispatch.state_invalid") != leases[1]
                or _timestamp(leases[2], "dispatch.state_invalid") <= updated_at
            ))
            or (error is not None and (
                not isinstance(error, str) or ERROR_CODE.fullmatch(error) is None
            ))
            or (status in {"retry_wait", "failed"}) != (error is not None)
            or (
                row["last_error_detail_hash"] is not None
                and (
                    not isinstance(row["last_error_detail_hash"], str)
                    or SHA256.fullmatch(row["last_error_detail_hash"]) is None
                )
            )
            or (status == "succeeded") != all(item is not None for item in remote)
            or (status != "succeeded" and any(item is not None for item in remote))
            or (status == "succeeded" and (
                _token(remote[0], "dispatch.state_invalid") != remote[0]
                or isinstance(remote[1], bool)
                or not isinstance(remote[1], int)
                or remote[1] <= 0
                or remote[2] not in {"enqueued", "superseded", "cancelled"}
                or not isinstance(remote[3], str)
                or SHA256.fullmatch(remote[3]) is None
            ))
        ):
            raise CMSDispatchBlocked("dispatch.state_invalid")

    def _claim(self, claim: Any) -> ChangeDispatchClaim:
        if not isinstance(claim, ChangeDispatchClaim):
            raise CMSDispatchBlocked("dispatch.claim_invalid")
        _token(claim.event_id, "dispatch.claim_invalid")
        _token(claim.lease_owner, "dispatch.claim_invalid", limit=128)
        _token(claim.lease_token, "dispatch.claim_invalid")
        if (
            not isinstance(claim.payload_sha256, str)
            or SHA256.fullmatch(claim.payload_sha256) is None
            or _hash(claim.payload_json) != claim.payload_sha256
            or isinstance(claim.attempt, bool)
            or not isinstance(claim.attempt, int)
            or isinstance(claim.max_attempts, bool)
            or not isinstance(claim.max_attempts, int)
            or not 1 <= claim.attempt <= claim.max_attempts <= MAX_ATTEMPTS
        ):
            raise CMSDispatchBlocked("dispatch.claim_invalid")
        _timestamp(claim.lease_expires_at, "dispatch.claim_invalid")
        return claim

    def _require_live_claim(
        self, claim: ChangeDispatchClaim, now: float,
    ) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM cms_source_change_outbox WHERE event_id = ?",
            (claim.event_id,),
        ).fetchone()
        if row is None:
            raise CMSDispatchBlocked("dispatch.claim_missing")
        self._validated_row(row)
        if (
            row["status"] != "leased"
            or row["payload_sha256"] != claim.payload_sha256
            or row["attempts"] != claim.attempt
            or row["max_attempts"] != claim.max_attempts
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
            or row["lease_expires_at"] != claim.lease_expires_at
        ):
            raise CMSDispatchBlocked("dispatch.claim_lost")
        if row["lease_expires_at"] <= now:
            raise CMSDispatchBlocked("dispatch.lease_expired")
        if now < row["updated_at"]:
            raise CMSDispatchBlocked("dispatch.clock_regressed")
        return row
