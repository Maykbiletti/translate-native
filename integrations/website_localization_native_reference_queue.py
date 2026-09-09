#!/usr/bin/env python3
"""Durable editorial queue for qualified-native benchmark references.

Only identifiers, lease state, stable error codes, and the accepted artifact
digest are stored here.  Source and target prose remain in the separately
bound work order and native-reference artifact.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import re
import secrets
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator


SCHEMA = "blun.website-localization-native-reference-queue.v1"
HEALTH_SCHEMA = "blun.website-localization-native-reference-queue-health.v1"
STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
MAX_STALE_SECONDS = 31_536_000.0
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TABLE_COLUMNS = (
    "work_id", "campaign_id", "target_locale", "suite_case_key", "status",
    "attempts", "max_attempts", "next_attempt_at", "lease_owner",
    "lease_token", "lease_expires_at", "last_error_code",
    "last_error_detail_hash", "artifact_sha256", "created_at", "updated_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native-reference queue dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CAMPAIGN = _load_module(
    "blun_website_localization_native_reference_queue_campaign",
    _ROOT / "integrations" / "website_localization_benchmark_campaign.py",
)


class NativeReferenceQueueBlocked(RuntimeError):
    """Content-free queue failure safe for operational reporting."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("native-reference queue error code is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClaimedNativeReference:
    work_id: str
    campaign_id: str
    target_locale: str
    suite_case_key: str
    job_payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class NativeReferenceQueueOutcome:
    work_id: str
    target_locale: str
    suite_case_key: str
    status: str
    attempt: int
    max_attempts: int
    next_attempt_at: float
    error_code: str | None
    error_detail_hash: str | None
    artifact_sha256: str | None


@dataclass(frozen=True)
class NativeReferenceQueueHealth:
    campaign_id: str
    status: str
    reasons: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]
    work_count: int
    last_progress_at: float | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": HEALTH_SCHEMA,
            "campaign_id": self.campaign_id,
            "status": self.status,
            "reasons": list(self.reasons),
            "counts": dict(self.counts),
            "work_count": self.work_count,
            "last_progress_at": self.last_progress_at,
        }


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeReferenceQueueBlocked("native_reference.queue.time_invalid")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise NativeReferenceQueueBlocked("native_reference.queue.time_invalid")
    return value


def _duration(value: Any, *, allow_zero: bool = False) -> float:
    value = _timestamp(value)
    if (value < 0 if allow_zero else value <= 0) or value > MAX_LEASE_SECONDS:
        raise NativeReferenceQueueBlocked("native_reference.queue.duration_invalid")
    return value


def _identifier(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 256 or "\x00" in value
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise NativeReferenceQueueBlocked("native_reference.queue.identity_invalid")
    return value


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise NativeReferenceQueueBlocked(
            "native_reference.queue.external_transaction",
        )
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class NativeReferenceWorkQueue:
    """Lease exact campaign jobs to independent native editorial workers."""

    def __init__(self, campaign_store: Any):
        connection = getattr(campaign_store, "connection", None)
        if (
            not isinstance(connection, sqlite3.Connection)
            or not callable(getattr(campaign_store, "_verify_binding_locked", None))
        ):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.campaign_store_invalid",
            )
        self.campaign_store = campaign_store
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        with _transaction(self.connection):
            self.connection.execute(f"""
                CREATE TABLE IF NOT EXISTS benchmark_native_reference_queue (
                    work_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    target_locale TEXT NOT NULL,
                    suite_case_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN {STATUSES}),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (
                        max_attempts BETWEEN 1 AND {MAX_ATTEMPTS}
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_error_detail_hash TEXT,
                    artifact_sha256 TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (campaign_id, target_locale, suite_case_key),
                    FOREIGN KEY (work_id) REFERENCES benchmark_campaign_work (work_id),
                    FOREIGN KEY (campaign_id) REFERENCES benchmark_campaigns (campaign_id)
                )
            """)
            self.connection.execute("""
                CREATE INDEX IF NOT EXISTS benchmark_native_reference_queue_ready
                ON benchmark_native_reference_queue
                (campaign_id, status, next_attempt_at, target_locale, suite_case_key)
            """)
        self._verify_schema()

    def _verify_schema(self) -> None:
        columns = tuple(
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(benchmark_native_reference_queue)"
            )
        )
        if columns != TABLE_COLUMNS:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.schema_unsupported",
            )

    def ensure(
        self, policy: Any, campaign_id: str, *, max_attempts: Any, now: Any,
    ) -> None:
        if (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.attempts_invalid",
            )
        now = _timestamp(now)
        try:
            policy = _CAMPAIGN._BENCHMARK._validate_policy(policy)
            _CAMPAIGN._assert_policy_current(policy, now)
            expected = _CAMPAIGN._expected_work(policy)
            with _transaction(self.connection):
                self.campaign_store._verify_binding_locked(policy, campaign_id)
                self.connection.executemany("""
                    INSERT OR IGNORE INTO benchmark_native_reference_queue (
                        work_id, campaign_id, target_locale, suite_case_key,
                        status, attempts, max_attempts, next_attempt_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (
                    (work_id, campaign_id, locale, case_key, max_attempts, now, now, now)
                    for work_id, locale, case_key in expected
                ))
                self._verify_binding_locked(policy, campaign_id)
                configured = self.connection.execute("""
                    SELECT MIN(max_attempts), MAX(max_attempts)
                    FROM benchmark_native_reference_queue WHERE campaign_id = ?
                """, (campaign_id,)).fetchone()
                if tuple(configured) != (max_attempts, max_attempts):
                    raise NativeReferenceQueueBlocked(
                        "native_reference.queue.attempts_mismatch",
                    )
        except NativeReferenceQueueBlocked:
            raise
        except Exception:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.binding_invalid",
            ) from None

    def _verify_binding_locked(self, policy: Any, campaign_id: str) -> None:
        self.campaign_store._verify_binding_locked(policy, campaign_id)
        expected = set(_CAMPAIGN._expected_work(policy))
        rows = self.connection.execute("""
                SELECT *
                FROM benchmark_native_reference_queue WHERE campaign_id = ?
            """, (campaign_id,)).fetchall()
        campaign_attempts = {
            row["work_id"]: row["max_attempts"]
            for row in self.connection.execute("""
                SELECT work_id, max_attempts FROM benchmark_campaign_work
                WHERE campaign_id = ?
            """, (campaign_id,))
        }
        observed = set()
        for row in rows:
            self._validate_row(row)
            if campaign_attempts.get(row["work_id"]) != row["max_attempts"]:
                raise NativeReferenceQueueBlocked(
                    "native_reference.queue.state_invalid",
                )
            observed.add((
                row["work_id"], row["target_locale"], row["suite_case_key"],
            ))
        if observed != expected:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.state_invalid",
            )

    @staticmethod
    def _validate_row(row: sqlite3.Row, *, now: float | None = None) -> None:
        try:
            if tuple(row.keys()) != TABLE_COLUMNS or row["status"] not in STATUSES:
                raise ValueError
            attempts = row["attempts"]
            maximum = row["max_attempts"]
            if (
                isinstance(attempts, bool) or not isinstance(attempts, int)
                or isinstance(maximum, bool) or not isinstance(maximum, int)
                or not 0 <= attempts <= maximum <= MAX_ATTEMPTS
            ):
                raise ValueError
            created = _timestamp(row["created_at"])
            updated = _timestamp(row["updated_at"])
            _timestamp(row["next_attempt_at"])
            if created > updated or now is not None and updated > now:
                raise ValueError
            lease_values = (
                row["lease_owner"], row["lease_token"], row["lease_expires_at"],
            )
            if row["status"] == "leased":
                _identifier(row["lease_owner"])
                _identifier(row["lease_token"])
                _timestamp(row["lease_expires_at"])
                if attempts < 1:
                    raise ValueError
            elif any(value is not None for value in lease_values):
                raise ValueError
            code = row["last_error_code"]
            detail = row["last_error_detail_hash"]
            artifact = row["artifact_sha256"]
            if code is not None and (
                not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None
            ):
                raise ValueError
            if detail is not None and (
                not isinstance(detail, str) or SHA256.fullmatch(detail) is None
            ):
                raise ValueError
            if row["status"] == "pending" and (
                attempts != 0 or code is not None or detail is not None
            ):
                raise ValueError
            if row["status"] in {"leased", "succeeded"} and (
                code is not None or detail is not None
            ):
                raise ValueError
            if row["status"] in {"retry_wait", "failed"} and (
                attempts < 1 or code is None
            ):
                raise ValueError
            if row["status"] == "retry_wait" and attempts >= maximum:
                raise ValueError
            if row["status"] == "succeeded":
                if (
                    attempts < 1 or not isinstance(artifact, str)
                    or SHA256.fullmatch(artifact) is None
                ):
                    raise ValueError
            elif artifact is not None:
                raise ValueError
        except NativeReferenceQueueBlocked:
            raise
        except Exception:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.state_invalid",
            ) from None

    def claim(
        self, policy: Any, campaign_id: str, worker_id: Any, *,
        now: Any, lease_seconds: Any = 3600,
    ) -> ClaimedNativeReference | None:
        worker_id = _identifier(worker_id)
        now = _timestamp(now)
        lease_seconds = _duration(lease_seconds)
        try:
            policy = _CAMPAIGN._BENCHMARK._validate_policy(policy)
            _CAMPAIGN._assert_policy_current(policy, now)
        except Exception:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.policy_invalid",
            ) from None
        with _transaction(self.connection):
            self._verify_binding_locked(policy, campaign_id)
            expired = self.connection.execute("""
                SELECT work_id, attempts, max_attempts
                FROM benchmark_native_reference_queue
                WHERE campaign_id = ? AND status = 'leased'
                  AND lease_expires_at <= ?
            """, (campaign_id, now)).fetchall()
            for row in expired:
                terminal = row["attempts"] >= row["max_attempts"]
                self.connection.execute("""
                    UPDATE benchmark_native_reference_queue
                    SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_code = 'native_reference.queue.lease_expired',
                        updated_at = ? WHERE work_id = ?
                """, (
                    "failed" if terminal else "retry_wait", now, now,
                    row["work_id"],
                ))
            row = self.connection.execute("""
                SELECT * FROM benchmark_native_reference_queue
                WHERE campaign_id = ?
                  AND status IN ('pending', 'retry_wait')
                  AND next_attempt_at <= ? AND attempts < max_attempts
                ORDER BY target_locale, suite_case_key LIMIT 1
            """, (campaign_id, now)).fetchone()
            if row is None:
                return None
            job_payload = _CAMPAIGN._job_payload(
                policy, row["target_locale"], row["suite_case_key"],
            )
            attempt = row["attempts"] + 1
            token = secrets.token_urlsafe(32)
            expires = now + lease_seconds
            changed = self.connection.execute("""
                UPDATE benchmark_native_reference_queue
                SET status = 'leased', attempts = ?, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?,
                    last_error_code = NULL, last_error_detail_hash = NULL,
                    updated_at = ?
                WHERE work_id = ? AND status IN ('pending', 'retry_wait')
            """, (
                attempt, worker_id, token, expires, now, row["work_id"],
            )).rowcount
            if changed != 1:
                raise NativeReferenceQueueBlocked(
                    "native_reference.queue.claim_lost",
                )
        return ClaimedNativeReference(
            work_id=row["work_id"], campaign_id=campaign_id,
            target_locale=row["target_locale"],
            suite_case_key=row["suite_case_key"],
            job_payload=job_payload,
            attempt=attempt, max_attempts=row["max_attempts"],
            lease_owner=worker_id, lease_token=token,
            lease_expires_at=expires,
        )

    def _assert_live_locked(
        self, claim: Any, now: float,
    ) -> sqlite3.Row:
        if not isinstance(claim, ClaimedNativeReference):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.claim_invalid",
            )
        row = self.connection.execute("""
            SELECT * FROM benchmark_native_reference_queue WHERE work_id = ?
        """, (claim.work_id,)).fetchone()
        if row is None or (
            row["status"] != "leased"
            or row["campaign_id"] != claim.campaign_id
            or row["target_locale"] != claim.target_locale
            or row["suite_case_key"] != claim.suite_case_key
            or row["attempts"] != claim.attempt
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
            or row["lease_expires_at"] != claim.lease_expires_at
            or row["lease_expires_at"] <= now
        ):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.lease_lost",
            )
        return row

    def assert_live(self, claim: Any, *, now: Any) -> None:
        now = _timestamp(now)
        with _transaction(self.connection):
            self._assert_live_locked(claim, now)

    def renew(
        self, claim: Any, *, now: Any, lease_seconds: Any,
    ) -> ClaimedNativeReference:
        now = _timestamp(now)
        expires = now + _duration(lease_seconds)
        with _transaction(self.connection):
            self._assert_live_locked(claim, now)
            self.connection.execute("""
                UPDATE benchmark_native_reference_queue
                SET lease_expires_at = ?, updated_at = ? WHERE work_id = ?
            """, (expires, now, claim.work_id))
        return ClaimedNativeReference(**{
            **asdict(claim), "lease_expires_at": expires,
        })

    def complete(
        self, policy: Any, claim: Any, artifact_sha256: Any, *, now: Any,
    ) -> NativeReferenceQueueOutcome:
        if not isinstance(claim, ClaimedNativeReference):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.claim_invalid",
            )
        if not isinstance(artifact_sha256, str) or SHA256.fullmatch(
            artifact_sha256,
        ) is None:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.artifact_invalid",
            )
        now = _timestamp(now)
        try:
            policy = _CAMPAIGN._BENCHMARK._validate_policy(policy)
            _CAMPAIGN._assert_policy_current(policy, now)
        except Exception:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.policy_invalid",
            ) from None
        with _transaction(self.connection):
            self._verify_binding_locked(policy, claim.campaign_id)
            self._assert_live_locked(claim, now)
            self.connection.execute("""
                UPDATE benchmark_native_reference_queue
                SET status = 'succeeded', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL, last_error_detail_hash = NULL,
                    artifact_sha256 = ?, updated_at = ? WHERE work_id = ?
            """, (artifact_sha256, now, claim.work_id))
        return self._outcome(claim.work_id)

    def transition_failure(
        self, policy: Any, claim: Any, code: Any, *, retryable: bool,
        delay_seconds: Any = 0, detail: str | None = None, now: Any,
    ) -> NativeReferenceQueueOutcome:
        if not isinstance(claim, ClaimedNativeReference):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.claim_invalid",
            )
        code = _identifier(code)
        if ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.failure_invalid",
            )
        delay = _duration(delay_seconds, allow_zero=True)
        now = _timestamp(now)
        if detail is not None and (
            not isinstance(detail, str) or "\x00" in detail
            or not unicodedata.is_normalized("NFC", detail)
        ):
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.failure_invalid",
            )
        detail_hash = (
            hashlib.sha256(detail.encode("utf-8")).hexdigest()
            if detail is not None else None
        )
        try:
            policy = _CAMPAIGN._BENCHMARK._validate_policy(policy)
        except Exception:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.policy_invalid",
            ) from None
        with _transaction(self.connection):
            self._verify_binding_locked(policy, claim.campaign_id)
            row = self._assert_live_locked(claim, now)
            terminal = not retryable or row["attempts"] >= row["max_attempts"]
            self.connection.execute("""
                UPDATE benchmark_native_reference_queue
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, last_error_detail_hash = ?,
                    updated_at = ? WHERE work_id = ?
            """, (
                "failed" if terminal else "retry_wait",
                now if terminal else now + delay, code, detail_hash, now,
                claim.work_id,
            ))
        return self._outcome(claim.work_id)

    def _outcome(self, work_id: str) -> NativeReferenceQueueOutcome:
        row = self.connection.execute("""
            SELECT * FROM benchmark_native_reference_queue WHERE work_id = ?
        """, (work_id,)).fetchone()
        if row is None:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.state_invalid",
            )
        return NativeReferenceQueueOutcome(
            work_id=row["work_id"], target_locale=row["target_locale"],
            suite_case_key=row["suite_case_key"], status=row["status"],
            attempt=row["attempts"], max_attempts=row["max_attempts"],
            next_attempt_at=row["next_attempt_at"],
            error_code=row["last_error_code"],
            error_detail_hash=row["last_error_detail_hash"],
            artifact_sha256=row["artifact_sha256"],
        )

    def status(self, policy: Any, campaign_id: str) -> dict[str, Any]:
        try:
            policy = _CAMPAIGN._BENCHMARK._validate_policy(policy)
            with _transaction(self.connection):
                self._verify_binding_locked(policy, campaign_id)
                rows = self.connection.execute("""
                    SELECT status, COUNT(*) AS count
                    FROM benchmark_native_reference_queue
                    WHERE campaign_id = ? GROUP BY status
                """, (campaign_id,)).fetchall()
                errors = self.connection.execute("""
                    SELECT last_error_code, COUNT(*) AS count
                    FROM benchmark_native_reference_queue
                    WHERE campaign_id = ? AND last_error_code IS NOT NULL
                    GROUP BY last_error_code ORDER BY last_error_code
                """, (campaign_id,)).fetchall()
        except NativeReferenceQueueBlocked:
            raise
        except Exception:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.state_invalid",
            ) from None
        counts = {name: 0 for name in STATUSES}
        counts.update({row["status"]: row["count"] for row in rows})
        return {
            "schema": SCHEMA,
            "campaign_id": campaign_id,
            "work_count": len(_CAMPAIGN._expected_work(policy)),
            "counts": counts,
            "error_counts": {
                row["last_error_code"]: row["count"] for row in errors
            },
            "complete": counts["succeeded"] == len(
                _CAMPAIGN._expected_work(policy)
            ),
            "blocked": counts["failed"] > 0,
        }

    def health(
        self, policy: Any, campaign_id: str, *, now: Any,
        stale_after_seconds: Any = 3600,
    ) -> NativeReferenceQueueHealth:
        now = _timestamp(now)
        stale_after_seconds = _timestamp(stale_after_seconds)
        if not 0 < stale_after_seconds <= MAX_STALE_SECONDS:
            raise NativeReferenceQueueBlocked(
                "native_reference.queue.stale_threshold_invalid",
            )
        counts = {name: 0 for name in STATUSES}
        reasons: set[str] = set()
        last_progress: float | None = None
        try:
            policy = _CAMPAIGN._BENCHMARK._validate_policy(policy)
            if now > policy.valid_until:
                reasons.add("native_reference.queue.policy_expired")
            if self.connection.in_transaction:
                raise ValueError
            self.connection.execute("BEGIN")
            try:
                self._verify_binding_locked(policy, campaign_id)
                rows = self.connection.execute("""
                    SELECT * FROM benchmark_native_reference_queue
                    WHERE campaign_id = ? ORDER BY target_locale, suite_case_key
                """, (campaign_id,)).fetchall()
                if len(rows) != len(_CAMPAIGN._expected_work(policy)):
                    raise ValueError
                for row in rows:
                    if tuple(row.keys()) != TABLE_COLUMNS or row["status"] not in counts:
                        raise ValueError
                    counts[row["status"]] += 1
                    attempts = row["attempts"]
                    maximum = row["max_attempts"]
                    created = _timestamp(row["created_at"])
                    updated = _timestamp(row["updated_at"])
                    next_at = _timestamp(row["next_attempt_at"])
                    if (
                        isinstance(attempts, bool) or not isinstance(attempts, int)
                        or isinstance(maximum, bool) or not isinstance(maximum, int)
                        or not 0 <= attempts <= maximum <= MAX_ATTEMPTS
                        or created > updated or updated > now
                    ):
                        raise ValueError
                    last_progress = max(
                        updated,
                        last_progress if last_progress is not None else updated,
                    )
                    leased = row["status"] == "leased"
                    lease_values = (
                        row["lease_owner"], row["lease_token"],
                        row["lease_expires_at"],
                    )
                    if leased:
                        _identifier(row["lease_owner"])
                        _identifier(row["lease_token"])
                        if attempts < 1:
                            raise ValueError
                        if _timestamp(row["lease_expires_at"]) <= now:
                            reasons.add("native_reference.queue.lease_expired")
                    elif any(value is not None for value in lease_values):
                        raise ValueError
                    code = row["last_error_code"]
                    detail = row["last_error_detail_hash"]
                    artifact = row["artifact_sha256"]
                    if code is not None:
                        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                            raise ValueError
                        reasons.add("native_reference.queue.error." + code)
                    if detail is not None and (
                        not isinstance(detail, str) or SHA256.fullmatch(detail) is None
                    ):
                        raise ValueError
                    if row["status"] == "succeeded":
                        if code is not None or detail is not None or attempts < 1:
                            raise ValueError
                        if not isinstance(artifact, str) or SHA256.fullmatch(artifact) is None:
                            raise ValueError
                    elif artifact is not None:
                        raise ValueError
                    if row["status"] == "pending" and (
                        attempts != 0 or code is not None or detail is not None
                    ):
                        raise ValueError
                    if row["status"] in {"retry_wait", "failed"} and (
                        attempts < 1 or code is None
                    ):
                        raise ValueError
                    if row["status"] == "retry_wait" and next_at <= now:
                        reasons.add("native_reference.queue.retry_due")
                if last_progress is None:
                    raise ValueError
                if counts["failed"]:
                    reasons.add("native_reference.queue.failed")
                if (
                    counts["succeeded"] < len(rows)
                    and now - last_progress > stale_after_seconds
                ):
                    reasons.add("native_reference.queue.stalled")
            finally:
                self.connection.rollback()
            status = (
                "blocked" if counts["failed"] or any(
                    reason in {
                        "native_reference.queue.policy_expired",
                        "native_reference.queue.failed",
                    } for reason in reasons
                )
                else "degraded" if reasons else "healthy"
            )
        except Exception:
            reasons = {"native_reference.queue.state_invalid"}
            status = "blocked"
        return NativeReferenceQueueHealth(
            campaign_id=campaign_id, status=status,
            reasons=tuple(sorted(reasons)),
            counts=tuple((name, counts[name]) for name in STATUSES),
            work_count=sum(counts.values()), last_progress_at=last_progress,
        )
