#!/usr/bin/env python3
"""Transactional queue state for provider-neutral website localization jobs.

The trusted host owns and opens the SQLite connection. This module deliberately
does not choose a filesystem path: secure, platform-specific database placement
belongs to the host. Queue completion records worker output, but never means
that the output is reviewed, signed, or ready for publication.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
MAX_PAYLOAD_BYTES = 2_000_000
MAX_RESULT_BYTES = 2_000_000
QUALITY_PASSES = ["target_native", "source_fidelity"]
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
_COLUMNS = (
    "job_id",
    "target_locale",
    "payload_json",
    "payload_sha256",
    "status",
    "attempts",
    "max_attempts",
    "next_attempt_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "last_error_code",
    "last_error_detail_hash",
    "result_json",
    "result_sha256",
    "created_at",
    "updated_at",
)
_PLAN_COLUMNS = ("plan_id", "job_id", "target_locale", "created_at")


class LocalizationQueueBlocked(RuntimeError):
    """Raised when queue state or an attempted transition is unsafe."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    plan_ids: tuple[str, ...]
    target_locale: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    plan_ids: tuple[str, ...]
    target_locale: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    last_error_code: str | None
    last_error_detail_hash: str | None
    result_sha256: str | None
    created_at: float
    updated_at: float


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise LocalizationQueueBlocked("payload must be finite JSON") from error


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_unicode(value: Any, *, field: str) -> None:
    if isinstance(value, str):
        if "\x00" in value or not unicodedata.is_normalized("NFC", value):
            raise LocalizationQueueBlocked(f"{field} contains unsafe Unicode text")
    elif isinstance(value, list):
        for item in value:
            _validate_unicode(item, field=field)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LocalizationQueueBlocked(f"{field} object keys must be strings")
            _validate_unicode(key, field=field)
            _validate_unicode(item, field=field)


def _field(name: str, value: Any, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LocalizationQueueBlocked(f"{name} must be a non-empty string without outer whitespace")
    if len(value) > limit or "\x00" in value or not unicodedata.is_normalized("NFC", value):
        raise LocalizationQueueBlocked(f"{name} is invalid or too long")
    return value


def _timestamp(name: str, value: float | int | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalizationQueueBlocked(f"{name} must be a finite timestamp")
    value = float(value)
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise LocalizationQueueBlocked(f"{name} must be a finite timestamp")
    return value


def _duration(name: str, value: float | int, *, allow_zero: bool = False) -> float:
    value = _timestamp(name, value)
    if (value < 0 if allow_zero else value <= 0) or value > MAX_LEASE_SECONDS:
        raise LocalizationQueueBlocked(f"{name} is outside the supported range")
    return value


def _error(code: Any, detail: Any = None) -> tuple[str, str | None]:
    code = _field("error_code", code, limit=128)
    if not ERROR_CODE.fullmatch(code):
        raise LocalizationQueueBlocked("error_code has an invalid format")
    if detail is None:
        return code, None
    if not isinstance(detail, str):
        raise LocalizationQueueBlocked("error_detail must be a string")
    if "\x00" in detail or not unicodedata.is_normalized("NFC", detail):
        raise LocalizationQueueBlocked("error_detail contains unsafe Unicode text")
    return code, _hash_text(detail)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise LocalizationQueueBlocked("queue operation cannot join an external transaction")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class LocalizationQueue:
    """Atomic at-least-once queue with expiring, attempt-bound worker leases."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise LocalizationQueueBlocked("connection must be sqlite3.Connection")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise LocalizationQueueBlocked("unsupported localization queue schema")
        if version == 0:
            self._create_schema()
        self._verify_schema()

    def _create_schema(self) -> None:
        with _transaction(self.connection):
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if version == SCHEMA_VERSION:
                return
            if version != 0:
                raise LocalizationQueueBlocked("unsupported localization queue schema")
            self.connection.execute(f"""
                CREATE TABLE localization_jobs (
                    job_id TEXT PRIMARY KEY,
                    target_locale TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN {_STATUSES}),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND {MAX_ATTEMPTS}),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_error_detail_hash TEXT,
                    result_json TEXT,
                    result_sha256 TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.connection.execute("""
                CREATE TABLE localization_plan_jobs (
                    plan_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    target_locale TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (plan_id, job_id),
                    UNIQUE (plan_id, target_locale),
                    FOREIGN KEY (job_id) REFERENCES localization_jobs (job_id)
                )
            """)
            self.connection.execute("""
                CREATE INDEX localization_jobs_ready
                ON localization_jobs (status, next_attempt_at, created_at, job_id)
            """)
            self.connection.execute("""
                CREATE INDEX localization_plan_jobs_job
                ON localization_plan_jobs (job_id, plan_id)
            """)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _verify_schema(self) -> None:
        rows = self.connection.execute("PRAGMA table_info(localization_jobs)").fetchall()
        columns = tuple(row["name"] for row in rows)
        if columns != _COLUMNS:
            raise LocalizationQueueBlocked("localization queue schema is incomplete or altered")
        plan_rows = self.connection.execute("PRAGMA table_info(localization_plan_jobs)").fetchall()
        plan_columns = tuple(row["name"] for row in plan_rows)
        if plan_columns != _PLAN_COLUMNS:
            raise LocalizationQueueBlocked("localization plan mapping schema is incomplete or altered")

    def enqueue_plan(
        self,
        plan: Any,
        *,
        max_attempts: int = 3,
        now: float | int | None = None,
    ) -> int:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise LocalizationQueueBlocked("max_attempts must be an integer")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise LocalizationQueueBlocked("max_attempts is outside the supported range")
        now = _timestamp("now", now)
        plan_id = _field("plan_id", getattr(plan, "plan_id", None))
        jobs = getattr(plan, "jobs", None)
        if not isinstance(jobs, tuple) or not jobs:
            raise LocalizationQueueBlocked("plan must contain an immutable non-empty job tuple")

        prepared: list[tuple[str, str, str, str]] = []
        targets: set[str] = set()
        for job in jobs:
            job_id = _field("job_id", getattr(job, "job_id", None))
            payload_method = getattr(job, "as_payload", None)
            if not callable(payload_method):
                raise LocalizationQueueBlocked("every plan job must expose as_payload")
            payload = payload_method()
            if not isinstance(payload, dict):
                raise LocalizationQueueBlocked("job payload must be a JSON object")
            _validate_unicode(payload, field="job payload")
            if payload.get("job_id") != job_id or payload.get("idempotency_key") != job_id:
                raise LocalizationQueueBlocked("job identity and idempotency key must match")
            target = payload.get("target")
            target_locale = target.get("locale") if isinstance(target, dict) else None
            target_locale = _field("target_locale", target_locale)
            if target_locale in targets:
                raise LocalizationQueueBlocked("plan contains duplicate target locales")
            targets.add(target_locale)
            if payload.get("quality_passes") != QUALITY_PASSES:
                raise LocalizationQueueBlocked("job must require the two ordered quality passes")
            if payload.get("release_required") is not True:
                raise LocalizationQueueBlocked("job must require a signed release")
            payload_json = _canonical_json(payload)
            if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise LocalizationQueueBlocked("job payload exceeds the byte limit")
            prepared.append((job_id, target_locale, payload_json, _hash_text(payload_json)))

        inserted = 0
        with _transaction(self.connection):
            for job_id, target_locale, payload_json, payload_hash in prepared:
                existing = self.connection.execute(
                    "SELECT payload_sha256 FROM localization_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != payload_hash:
                        raise LocalizationQueueBlocked("idempotency collision with different job content")
                else:
                    self.connection.execute("""
                        INSERT INTO localization_jobs (
                            job_id, target_locale, payload_json, payload_sha256,
                            status, attempts, max_attempts, next_attempt_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                    """, (
                        job_id,
                        target_locale,
                        payload_json,
                        payload_hash,
                        max_attempts,
                        now,
                        now,
                        now,
                    ))
                    inserted += 1
                mapped = self.connection.execute("""
                    SELECT job_id FROM localization_plan_jobs
                    WHERE plan_id = ? AND target_locale = ?
                """, (plan_id, target_locale)).fetchone()
                if mapped is not None and mapped["job_id"] != job_id:
                    raise LocalizationQueueBlocked("plan target locale maps to different job content")
                self.connection.execute("""
                    INSERT OR IGNORE INTO localization_plan_jobs (
                        plan_id, job_id, target_locale, created_at
                    ) VALUES (?, ?, ?, ?)
                """, (plan_id, job_id, target_locale, now))
        return inserted

    def claim(
        self,
        worker_id: Any,
        *,
        now: float | int | None = None,
        lease_seconds: float | int = 300,
    ) -> ClaimedJob | None:
        worker_id = _field("worker_id", worker_id, limit=128)
        now = _timestamp("now", now)
        lease_seconds = _duration("lease_seconds", lease_seconds)
        blocked_error: str | None = None
        claimed: ClaimedJob | None = None
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE localization_jobs
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'lease_expired',
                    updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts >= max_attempts
            """, (now, now))
            self.connection.execute("""
                UPDATE localization_jobs
                SET status = 'retry_wait', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts < max_attempts
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM localization_jobs
                WHERE status IN ('pending', 'retry_wait')
                  AND next_attempt_at <= ? AND attempts < max_attempts
                ORDER BY created_at, target_locale, job_id
                LIMIT 1
            """, (now,)).fetchone()
            if row is not None:
                payload_hash = _hash_text(row["payload_json"])
                if payload_hash != row["payload_sha256"]:
                    self.connection.execute("""
                        UPDATE localization_jobs
                        SET status = 'failed', last_error_code = 'payload_integrity',
                            updated_at = ?
                        WHERE job_id = ?
                    """, (now, row["job_id"]))
                    blocked_error = "queued payload failed its integrity check"
                else:
                    try:
                        payload = json.loads(row["payload_json"])
                    except json.JSONDecodeError:
                        self.connection.execute("""
                            UPDATE localization_jobs
                            SET status = 'failed', last_error_code = 'payload_integrity',
                                updated_at = ?
                            WHERE job_id = ?
                        """, (now, row["job_id"]))
                        blocked_error = "queued payload is no longer valid JSON"
                    else:
                        lease_token = secrets.token_urlsafe(32)
                        lease_expires_at = now + lease_seconds
                        updated = self.connection.execute("""
                            UPDATE localization_jobs
                            SET status = 'leased', attempts = attempts + 1,
                                lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                                last_error_code = NULL, last_error_detail_hash = NULL,
                                updated_at = ?
                            WHERE job_id = ? AND status IN ('pending', 'retry_wait')
                        """, (
                            worker_id,
                            lease_token,
                            lease_expires_at,
                            now,
                            row["job_id"],
                        ))
                        if updated.rowcount != 1:
                            raise LocalizationQueueBlocked("claim lost its transactional job identity")
                        claimed = ClaimedJob(
                            job_id=row["job_id"],
                            plan_ids=self._plan_ids(row["job_id"]),
                            target_locale=row["target_locale"],
                            payload=payload,
                            attempt=int(row["attempts"]) + 1,
                            max_attempts=int(row["max_attempts"]),
                            lease_owner=worker_id,
                            lease_token=lease_token,
                            lease_expires_at=lease_expires_at,
                        )
        if blocked_error:
            raise LocalizationQueueBlocked(blocked_error)
        return claimed

    def renew(
        self,
        claim: ClaimedJob,
        *,
        now: float | int | None = None,
        lease_seconds: float | int = 300,
    ) -> ClaimedJob:
        claim = self._claim_identity(claim)
        now = _timestamp("now", now)
        lease_seconds = _duration("lease_seconds", lease_seconds)
        new_expiry = now + lease_seconds
        with _transaction(self.connection):
            self._require_live_lease(claim, now)
            updated = self.connection.execute("""
                UPDATE localization_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                new_expiry,
                now,
                claim.job_id,
                claim.lease_owner,
                claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LocalizationQueueBlocked("lease renewal lost its job identity")
        return ClaimedJob(
            **{**claim.__dict__, "lease_expires_at": new_expiry},
        )

    def complete(
        self,
        claim: ClaimedJob,
        result: Any,
        *,
        now: float | int | None = None,
    ) -> JobStatus:
        claim = self._claim_identity(claim)
        now = _timestamp("now", now)
        if not isinstance(result, dict):
            raise LocalizationQueueBlocked("worker result must be a JSON object")
        _validate_unicode(result, field="worker result")
        result_json = _canonical_json(result)
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise LocalizationQueueBlocked("worker result exceeds the byte limit")
        result_hash = _hash_text(result_json)
        with _transaction(self.connection):
            self._require_live_lease(claim, now)
            updated = self.connection.execute("""
                UPDATE localization_jobs
                SET status = 'succeeded', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, result_json = ?, result_sha256 = ?,
                    last_error_code = NULL, last_error_detail_hash = NULL,
                    updated_at = ?
                WHERE job_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                result_json,
                result_hash,
                now,
                claim.job_id,
                claim.lease_owner,
                claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LocalizationQueueBlocked("completion lost its job identity")
        return self.status(claim.job_id)

    def retry(
        self,
        claim: ClaimedJob,
        error_code: Any,
        *,
        error_detail: Any = None,
        delay_seconds: float | int = 0,
        now: float | int | None = None,
    ) -> JobStatus:
        claim = self._claim_identity(claim)
        error_code, detail_hash = _error(error_code, error_detail)
        now = _timestamp("now", now)
        delay_seconds = _duration("delay_seconds", delay_seconds, allow_zero=True)
        with _transaction(self.connection):
            row = self._require_live_lease(claim, now)
            terminal = int(row["attempts"]) >= int(row["max_attempts"])
            status = "failed" if terminal else "retry_wait"
            next_attempt_at = now if terminal else now + delay_seconds
            updated = self.connection.execute("""
                UPDATE localization_jobs
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, last_error_detail_hash = ?, updated_at = ?
                WHERE job_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                status,
                next_attempt_at,
                error_code,
                detail_hash,
                now,
                claim.job_id,
                claim.lease_owner,
                claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LocalizationQueueBlocked("retry lost its job identity")
        return self.status(claim.job_id)

    def fail(
        self,
        claim: ClaimedJob,
        error_code: Any,
        *,
        error_detail: Any = None,
        now: float | int | None = None,
    ) -> JobStatus:
        claim = self._claim_identity(claim)
        error_code, detail_hash = _error(error_code, error_detail)
        now = _timestamp("now", now)
        with _transaction(self.connection):
            self._require_live_lease(claim, now)
            updated = self.connection.execute("""
                UPDATE localization_jobs
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = ?,
                    last_error_detail_hash = ?, updated_at = ?
                WHERE job_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                error_code,
                detail_hash,
                now,
                claim.job_id,
                claim.lease_owner,
                claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LocalizationQueueBlocked("failure transition lost its job identity")
        return self.status(claim.job_id)

    def status(self, job_id: Any) -> JobStatus:
        job_id = _field("job_id", job_id)
        row = self.connection.execute("""
            SELECT job_id, target_locale, status, attempts, max_attempts,
                   next_attempt_at, lease_expires_at, last_error_code,
                   last_error_detail_hash, result_sha256, created_at, updated_at
            FROM localization_jobs WHERE job_id = ?
        """, (job_id,)).fetchone()
        if row is None:
            raise LocalizationQueueBlocked("unknown localization job")
        return JobStatus(plan_ids=self._plan_ids(job_id), **dict(row))

    def plan_counts(self, plan_id: Any) -> dict[str, int]:
        plan_id = _field("plan_id", plan_id)
        counts = {status: 0 for status in _STATUSES}
        rows = self.connection.execute("""
            SELECT jobs.status, COUNT(*) AS count
            FROM localization_plan_jobs AS plans
            JOIN localization_jobs AS jobs ON jobs.job_id = plans.job_id
            WHERE plans.plan_id = ? GROUP BY jobs.status
        """, (plan_id,)).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["count"])
        return counts

    def _plan_ids(self, job_id: str) -> tuple[str, ...]:
        rows = self.connection.execute("""
            SELECT plan_id FROM localization_plan_jobs
            WHERE job_id = ? ORDER BY plan_id
        """, (job_id,)).fetchall()
        plan_ids = tuple(row["plan_id"] for row in rows)
        if not plan_ids:
            raise LocalizationQueueBlocked("localization job has no owning plan")
        return plan_ids

    def _claim_identity(self, claim: Any) -> ClaimedJob:
        if not isinstance(claim, ClaimedJob):
            raise LocalizationQueueBlocked("transition requires the exact claimed job")
        _field("job_id", claim.job_id)
        _field("lease_owner", claim.lease_owner, limit=128)
        _field("lease_token", claim.lease_token, limit=128)
        return claim

    def _require_live_lease(self, claim: ClaimedJob, now: float) -> sqlite3.Row:
        row = self.connection.execute("""
            SELECT status, attempts, max_attempts, lease_owner, lease_token,
                   lease_expires_at
            FROM localization_jobs WHERE job_id = ?
        """, (claim.job_id,)).fetchone()
        if row is None or row["status"] != "leased":
            raise LocalizationQueueBlocked("job no longer has an active lease")
        if row["lease_owner"] != claim.lease_owner or row["lease_token"] != claim.lease_token:
            raise LocalizationQueueBlocked("claim does not own the active lease")
        if row["lease_expires_at"] <= now:
            raise LocalizationQueueBlocked("claim lease has expired")
        return row
