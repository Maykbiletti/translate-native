#!/usr/bin/env python3
"""Crash-resumable, content-free retrieval of one benchmark report.

The watcher wraps the strict two-request benchmark client with a durable lease,
bounded retries, explicit operator recovery, and a read-only health contract.
It never persists report content or treats a successful fetch as proof that the
candidate may claim superiority.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 1000
MAX_DELAY_SECONDS = 86_400.0
_META_COLUMNS = ("singleton", "schema_version")
_COLUMNS = (
    "singleton", "campaign_id", "policy_sha256", "suite_sha256", "state",
    "attempts", "max_attempts", "lease_seconds", "base_delay_seconds",
    "max_delay_seconds", "next_attempt_at", "lease_owner", "lease_token",
    "lease_expires_at", "last_error_code", "report_sha256", "report_status",
    "superiority_claim_allowed", "block_reasons_json", "locale_count",
    "completed_at", "updated_at",
)


def _load_client():
    path = Path(__file__).resolve().with_name(
        "website_localization_benchmark_client.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_benchmark_watcher_client", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark client is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLIENT = _load_client()


class BenchmarkWatcherBlocked(RuntimeError):
    """Stable, content-free watcher failure."""


@dataclass(frozen=True)
class BenchmarkWatchStatus:
    state: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    report_sha256: str | None
    report_status: str | None
    superiority_claim_allowed: bool | None
    block_reasons: tuple[str, ...]
    locale_count: int | None
    completed_at: float | None


@dataclass(frozen=True)
class BenchmarkWatchOutcome:
    attempted: bool
    state: str
    attempt: int
    next_attempt_at: float
    report_status: str | None
    superiority_claim_allowed: bool | None
    error_code: str | None


@dataclass(frozen=True)
class BenchmarkWatcherHealth:
    checked_at: float
    status: str
    state: str
    ready: bool
    due: bool
    active_lease: bool
    lease_expired: bool
    attempts: int
    max_attempts: int
    next_action_at: float
    last_error_code: str | None
    report_sha256: str | None
    report_status: str | None
    superiority_claim_allowed: bool | None
    block_reasons: tuple[str, ...]
    locale_count: int | None
    completed_at: float | None
    watcher_reasons: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": "blun.website-localization-benchmark-watcher.v1",
            "checked_at": self.checked_at,
            "status": self.status,
            "state": self.state,
            "ready": self.ready,
            "due": self.due,
            "active_lease": self.active_lease,
            "lease_expired": self.lease_expired,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_action_at": self.next_action_at,
            "last_error_code": self.last_error_code,
            "report": (
                None if self.report_sha256 is None else {
                    "sha256": self.report_sha256,
                    "status": self.report_status,
                    "superiority_claim_allowed": (
                        self.superiority_claim_allowed
                    ),
                    "block_reasons": list(self.block_reasons),
                    "locale_count": self.locale_count,
                    "completed_at": self.completed_at,
                }
            ),
            "watcher_reasons": list(self.watcher_reasons),
        }


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkWatcherBlocked(f"benchmark_watcher.{name}_invalid")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise BenchmarkWatcherBlocked(f"benchmark_watcher.{name}_invalid")
    return result


def _worker(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 128 or not value.isascii()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise BenchmarkWatcherBlocked("benchmark_watcher.worker_invalid")
    return value


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise BenchmarkWatcherBlocked("benchmark_watcher.transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableBenchmarkReportWatcher:
    """Retrieve one exact campaign report with durable bounded recovery."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        client: Any,
        *,
        lease_seconds: float | int = 30,
        base_delay_seconds: float | int = 5,
        max_delay_seconds: float | int = 300,
        max_attempts: int = 20,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if not callable(getattr(client, "report", None)):
            raise TypeError("client must expose report()")
        if (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise BenchmarkWatcherBlocked("benchmark_watcher.max_attempts_invalid")
        campaign_id = getattr(client, "expected_campaign_id", None)
        policy_sha256 = getattr(client, "expected_policy_sha256", None)
        suite_sha256 = getattr(client, "expected_suite_sha256", None)
        if (
            not isinstance(campaign_id, str)
            or _CLIENT._HTTP.CAMPAIGN_ID.fullmatch(campaign_id) is None
            or not isinstance(policy_sha256, str)
            or _CLIENT._HTTP.SHA256.fullmatch(policy_sha256) is None
            or not isinstance(suite_sha256, str)
            or _CLIENT._HTTP.SHA256.fullmatch(suite_sha256) is None
        ):
            raise BenchmarkWatcherBlocked("benchmark_watcher.binding_invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.client = client
        self.campaign_id = campaign_id
        self.policy_sha256 = policy_sha256
        self.suite_sha256 = suite_sha256
        self.lease_seconds = _number(
            lease_seconds, "lease", minimum=0.1, maximum=3600,
        )
        self.base_delay_seconds = _number(
            base_delay_seconds, "delay", minimum=0.1,
            maximum=MAX_DELAY_SECONDS,
        )
        self.max_delay_seconds = _number(
            max_delay_seconds, "delay", minimum=0.1,
            maximum=MAX_DELAY_SECONDS,
        )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise BenchmarkWatcherBlocked("benchmark_watcher.delay_invalid")
        self.max_attempts = max_attempts
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_report_watcher_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO benchmark_report_watcher_meta VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_report_watcher (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    campaign_id TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    suite_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'pending', 'leased', 'retry_wait',
                            'succeeded', 'failed'
                        )
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (
                        max_attempts BETWEEN 1 AND 1000
                    ),
                    lease_seconds REAL NOT NULL CHECK (lease_seconds > 0),
                    base_delay_seconds REAL NOT NULL CHECK (
                        base_delay_seconds > 0
                    ),
                    max_delay_seconds REAL NOT NULL CHECK (
                        max_delay_seconds >= base_delay_seconds
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    report_sha256 TEXT,
                    report_status TEXT CHECK (
                        report_status IS NULL OR report_status IN ('PASS', 'BLOCK')
                    ),
                    superiority_claim_allowed INTEGER CHECK (
                        superiority_claim_allowed IS NULL OR
                        superiority_claim_allowed IN (0, 1)
                    ),
                    block_reasons_json TEXT NOT NULL,
                    locale_count INTEGER,
                    completed_at REAL,
                    updated_at REAL NOT NULL,
                    CHECK (
                        (state = 'leased' AND lease_owner IS NOT NULL
                         AND lease_token IS NOT NULL
                         AND lease_expires_at IS NOT NULL)
                        OR
                        (state <> 'leased' AND lease_owner IS NULL
                         AND lease_token IS NULL
                         AND lease_expires_at IS NULL)
                    )
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO benchmark_report_watcher (
                    singleton, campaign_id, policy_sha256, suite_sha256,
                    state, attempts, max_attempts, lease_seconds,
                    base_delay_seconds, max_delay_seconds, next_attempt_at,
                    block_reasons_json, updated_at
                ) VALUES (1, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, 0, '[]', 0)
            """, (
                self.campaign_id, self.policy_sha256, self.suite_sha256,
                self.max_attempts, self.lease_seconds,
                self.base_delay_seconds, self.max_delay_seconds,
            ))
        self._validate_schema()
        self._validated_row(self._row())

    def _validate_schema(self) -> None:
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(benchmark_report_watcher_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(benchmark_report_watcher)"
        ).fetchall())
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM benchmark_report_watcher_meta"
        ).fetchall()
        if (
            meta_columns != _META_COLUMNS or columns != _COLUMNS
            or len(meta) != 1 or tuple(meta[0]) != (1, SCHEMA_VERSION)
        ):
            raise BenchmarkWatcherBlocked("benchmark_watcher.schema_altered")

    def _row(self) -> sqlite3.Row:
        rows = self.connection.execute(
            "SELECT * FROM benchmark_report_watcher"
        ).fetchall()
        if len(rows) != 1:
            raise BenchmarkWatcherBlocked("benchmark_watcher.state_invalid")
        return rows[0]

    def run_once(
        self, worker_id: str, *, now: float | int,
    ) -> BenchmarkWatchOutcome:
        self._validate_schema()
        worker_id = _worker(worker_id)
        now = _number(now, "time", minimum=0, maximum=10**12)
        token = secrets.token_hex(32)
        with _transaction(self.connection):
            row = self._row()
            self._validated_row(row)
            expired = row["state"] == "leased" and row["lease_expires_at"] <= now
            due = (
                row["state"] in {"pending", "retry_wait"}
                and row["next_attempt_at"] <= now
            )
            if expired and row["attempts"] >= self.max_attempts:
                code = "benchmark_watcher.attempts_exhausted"
                self.connection.execute("""
                    UPDATE benchmark_report_watcher
                    SET state = 'failed', next_attempt_at = ?,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error_code = ?,
                        updated_at = ? WHERE singleton = 1
                """, (now, code, now))
                return BenchmarkWatchOutcome(
                    False, "failed", int(row["attempts"]), now,
                    None, None, code,
                )
            if not (expired or due):
                current = self._status(row, now)
                return BenchmarkWatchOutcome(
                    False, current.state, current.attempts,
                    current.next_attempt_at, current.report_status,
                    current.superiority_claim_allowed,
                    current.last_error_code,
                )
            attempt = int(row["attempts"]) + 1
            self.connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'leased', attempts = ?, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE singleton = 1
            """, (attempt, worker_id, token, now + self.lease_seconds, now))
        try:
            snapshot = self.client.report()
            summary = self._report_summary(snapshot, now)
        except BenchmarkWatcherBlocked:
            return self._finish_failure(
                worker_id, token, attempt,
                "benchmark_watcher.report_invalid", False, now,
            )
        except Exception as error:
            if getattr(error, "benchmark_client_failure", None) is True:
                code = getattr(error, "code", None)
                retryable = getattr(error, "retryable", None)
                if (
                    isinstance(code, str)
                    and _CLIENT._HTTP.ERROR_CODE.fullmatch(code) is not None
                    and type(retryable) is bool
                ):
                    return self._finish_failure(
                        worker_id, token, attempt, code, retryable, now,
                    )
            return self._finish_failure(
                worker_id, token, attempt,
                "benchmark_watcher.client_invalid", False, now,
            )
        with _transaction(self.connection):
            self._leased_row(worker_id, token, attempt)
            self.connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'succeeded', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    report_sha256 = ?, report_status = ?,
                    superiority_claim_allowed = ?, block_reasons_json = ?,
                    locale_count = ?, completed_at = ?, updated_at = ?
                WHERE singleton = 1
            """, (
                now, summary["sha256"], summary["status"],
                int(summary["superiority_claim_allowed"]),
                json.dumps(summary["block_reasons"], separators=(",", ":")),
                summary["locale_count"], now, now,
            ))
        return BenchmarkWatchOutcome(
            True, "succeeded", attempt, now, summary["status"],
            summary["superiority_claim_allowed"], None,
        )

    def _report_summary(self, snapshot: Any, now: float) -> dict[str, Any]:
        payload = getattr(snapshot, "as_payload", None)
        campaign = getattr(snapshot, "campaign", None)
        digest = getattr(snapshot, "report_sha256", None)
        if not callable(payload) or not isinstance(campaign, dict):
            raise BenchmarkWatcherBlocked("benchmark_watcher.report_invalid")
        try:
            report = payload()
            campaign = _CLIENT._HTTP._status_payload(campaign)
        except Exception:
            raise BenchmarkWatcherBlocked(
                "benchmark_watcher.report_invalid"
            ) from None
        if (
            not isinstance(report, dict)
            or not isinstance(digest, str)
            or _CLIENT._HTTP.SHA256.fullmatch(digest) is None
            or campaign["campaign_id"] != self.campaign_id
            or campaign["policy_sha256"] != self.policy_sha256
            or campaign["suite_sha256"] != self.suite_sha256
            or campaign["valid_until"] <= now
            or report.get("valid_until") != campaign["valid_until"]
            or report.get("status") not in {"PASS", "BLOCK"}
            or type(report.get("superiority_claim_allowed")) is not bool
            or report["superiority_claim_allowed"] != (
                report["status"] == "PASS"
            )
            or not isinstance(report.get("claim_block_reasons"), list)
            or len(report["claim_block_reasons"]) > 64
            or not isinstance(report.get("locales"), list)
        ):
            raise BenchmarkWatcherBlocked("benchmark_watcher.report_invalid")
        reasons = report["claim_block_reasons"]
        if (
            len(reasons) != len(set(reasons))
            or any(
                not isinstance(reason, str)
                or _CLIENT._HTTP.ERROR_CODE.fullmatch(reason) is None
                for reason in reasons
            )
            or (report["status"] == "PASS" and reasons)
            or (report["status"] == "BLOCK" and not reasons)
        ):
            raise BenchmarkWatcherBlocked("benchmark_watcher.report_invalid")
        try:
            calculated = hashlib.sha256(
                _CLIENT._HTTP._canonical_json(report)
            ).hexdigest()
        except Exception:
            raise BenchmarkWatcherBlocked(
                "benchmark_watcher.report_invalid"
            ) from None
        if calculated != digest:
            raise BenchmarkWatcherBlocked("benchmark_watcher.report_invalid")
        return {
            "sha256": digest,
            "status": report["status"],
            "superiority_claim_allowed": report["superiority_claim_allowed"],
            "block_reasons": sorted(reasons),
            "locale_count": len(report["locales"]),
        }

    def _finish_failure(
        self, worker_id: str, token: str, attempt: int, code: str,
        retryable: bool, now: float,
    ) -> BenchmarkWatchOutcome:
        with _transaction(self.connection):
            self._leased_row(worker_id, token, attempt)
            retry = retryable and attempt < self.max_attempts
            state = "retry_wait" if retry else "failed"
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** max(0, attempt - 1)),
            )
            next_attempt_at = now + delay if retry else now
            self.connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, updated_at = ?
                WHERE singleton = 1
            """, (state, next_attempt_at, code, now))
        return BenchmarkWatchOutcome(
            True, state, attempt, next_attempt_at, None, None, code,
        )

    def rearm(self, *, now: float | int) -> BenchmarkWatchStatus:
        """Reset a failed watcher after explicit operator remediation."""
        self._validate_schema()
        now = _number(now, "time", minimum=0, maximum=10**12)
        with _transaction(self.connection):
            row = self._row()
            self._validated_row(row)
            if row["state"] == "leased":
                raise BenchmarkWatcherBlocked("benchmark_watcher.lease_active")
            if row["state"] == "succeeded":
                raise BenchmarkWatcherBlocked("benchmark_watcher.result_final")
            if row["state"] != "failed":
                raise BenchmarkWatcherBlocked("benchmark_watcher.rearm_not_required")
            self.connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'pending', attempts = 0, next_attempt_at = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE singleton = 1
            """, (now, now))
        return self.status(now=now)

    def status(self, *, now: float | int) -> BenchmarkWatchStatus:
        self._validate_schema()
        now = _number(now, "time", minimum=0, maximum=10**12)
        row = self._row()
        self._validated_row(row)
        return self._status(row, now)

    def health(self, *, now: float | int) -> BenchmarkWatcherHealth:
        """Return a read-only, content-free view of retrieval state."""
        now = _number(now, "time", minimum=0, maximum=10**12)
        current = self.status(now=now)
        active_lease = current.state == "leased" and not current.lease_expired
        due = current.lease_expired or (
            current.state in {"pending", "retry_wait"}
            and current.next_attempt_at <= now
        )
        reasons: set[str] = set()
        if current.state == "pending":
            reasons.add("benchmark_watcher.pending")
        elif current.state == "retry_wait":
            reasons.add("benchmark_watcher.retry_wait")
        elif current.state == "failed":
            reasons.add("benchmark_watcher.failed")
        if current.lease_expired:
            reasons.add("benchmark_watcher.lease_expired")
        if current.report_status == "BLOCK":
            reasons.add("benchmark_watcher.report_blocked")
        if current.state == "failed" or current.report_status == "BLOCK":
            health_status = "blocked"
        elif current.state == "succeeded":
            health_status = "healthy"
        else:
            health_status = "degraded"
        next_action_at = (
            current.lease_expires_at
            if current.state == "leased" else current.next_attempt_at
        )
        if next_action_at is None:
            raise BenchmarkWatcherBlocked("benchmark_watcher.state_invalid")
        return BenchmarkWatcherHealth(
            checked_at=now,
            status=health_status,
            state=current.state,
            ready=current.state == "succeeded",
            due=due,
            active_lease=active_lease,
            lease_expired=current.lease_expired,
            attempts=current.attempts,
            max_attempts=current.max_attempts,
            next_action_at=next_action_at,
            last_error_code=current.last_error_code,
            report_sha256=current.report_sha256,
            report_status=current.report_status,
            superiority_claim_allowed=current.superiority_claim_allowed,
            block_reasons=current.block_reasons,
            locale_count=current.locale_count,
            completed_at=current.completed_at,
            watcher_reasons=tuple(sorted(reasons)),
        )

    def run_forever(
        self,
        worker_id: str,
        *,
        clock: Callable[[], float],
        stop_event: Any,
        maximum_wait_seconds: float | int = 30,
    ) -> BenchmarkWatchStatus:
        """Run until the report is final, the watcher fails, or the host stops."""
        worker_id = _worker(worker_id)
        if not callable(clock):
            raise BenchmarkWatcherBlocked("benchmark_watcher.clock_invalid")
        is_set = getattr(stop_event, "is_set", None)
        wait = getattr(stop_event, "wait", None)
        if not callable(is_set) or not callable(wait):
            raise BenchmarkWatcherBlocked("benchmark_watcher.stop_event_invalid")
        maximum_wait = _number(
            maximum_wait_seconds, "delay", minimum=0.1, maximum=3600,
        )
        while True:
            try:
                stopped = is_set()
            except Exception:
                raise BenchmarkWatcherBlocked(
                    "benchmark_watcher.stop_event_invalid"
                ) from None
            if type(stopped) is not bool:
                raise BenchmarkWatcherBlocked(
                    "benchmark_watcher.stop_event_invalid"
                )
            try:
                now = _number(clock(), "time", minimum=0, maximum=10**12)
            except BenchmarkWatcherBlocked:
                raise
            except Exception:
                raise BenchmarkWatcherBlocked(
                    "benchmark_watcher.clock_invalid"
                ) from None
            if stopped:
                return self.status(now=now)
            self.run_once(worker_id, now=now)
            current = self.status(now=now)
            if current.state in {"succeeded", "failed"}:
                return current
            action_at = (
                current.lease_expires_at
                if current.state == "leased" else current.next_attempt_at
            )
            if action_at is None:
                raise BenchmarkWatcherBlocked("benchmark_watcher.state_invalid")
            delay = min(maximum_wait, max(0.1, action_at - now))
            try:
                awakened = wait(delay)
            except Exception:
                raise BenchmarkWatcherBlocked(
                    "benchmark_watcher.stop_event_invalid"
                ) from None
            if type(awakened) is not bool:
                raise BenchmarkWatcherBlocked(
                    "benchmark_watcher.stop_event_invalid"
                )
            if awakened:
                try:
                    stopped_now = is_set()
                except Exception:
                    raise BenchmarkWatcherBlocked(
                        "benchmark_watcher.stop_event_invalid"
                    ) from None
                if type(stopped_now) is not bool or not stopped_now:
                    raise BenchmarkWatcherBlocked(
                        "benchmark_watcher.stop_event_invalid"
                    )

    def _status(self, row: sqlite3.Row, now: float) -> BenchmarkWatchStatus:
        return BenchmarkWatchStatus(
            state=row["state"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_attempt_at=float(row["next_attempt_at"]),
            lease_expires_at=(
                None if row["lease_expires_at"] is None
                else float(row["lease_expires_at"])
            ),
            lease_expired=(
                row["state"] == "leased" and row["lease_expires_at"] <= now
            ),
            last_error_code=row["last_error_code"],
            report_sha256=row["report_sha256"],
            report_status=row["report_status"],
            superiority_claim_allowed=(
                None if row["superiority_claim_allowed"] is None
                else bool(row["superiority_claim_allowed"])
            ),
            block_reasons=tuple(json.loads(row["block_reasons_json"])),
            locale_count=row["locale_count"],
            completed_at=row["completed_at"],
        )

    def _leased_row(
        self, worker_id: str, token: str, attempt: int,
    ) -> sqlite3.Row:
        row = self._row()
        self._validated_row(row)
        if (
            row["state"] != "leased" or row["lease_owner"] != worker_id
            or row["lease_token"] != token or row["attempts"] != attempt
        ):
            raise BenchmarkWatcherBlocked("benchmark_watcher.lease_lost")
        return row

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            reasons = json.loads(row["block_reasons_json"])
        except (TypeError, json.JSONDecodeError, RecursionError):
            raise BenchmarkWatcherBlocked(
                "benchmark_watcher.state_invalid"
            ) from None
        report_fields = (
            row["report_sha256"], row["report_status"],
            row["superiority_claim_allowed"], row["locale_count"],
            row["completed_at"],
        )
        no_report = all(value is None for value in report_fields)
        valid_report = (
            isinstance(row["report_sha256"], str)
            and _CLIENT._HTTP.SHA256.fullmatch(row["report_sha256"]) is not None
            and row["report_status"] in {"PASS", "BLOCK"}
            and row["superiority_claim_allowed"] in {0, 1}
            and bool(row["superiority_claim_allowed"])
            == (row["report_status"] == "PASS")
            and isinstance(row["locale_count"], int)
            and row["locale_count"] >= 0
            and isinstance(row["completed_at"], (int, float))
            and math.isfinite(row["completed_at"])
            and row["completed_at"] >= 0
        )
        valid_reasons = (
            isinstance(reasons, list)
            and reasons == sorted(set(reasons))
            and len(reasons) <= 64
            and all(
                isinstance(reason, str)
                and _CLIENT._HTTP.ERROR_CODE.fullmatch(reason) is not None
                for reason in reasons
            )
            and (
                no_report
                or (row["report_status"] == "PASS" and not reasons)
                or (row["report_status"] == "BLOCK" and bool(reasons))
            )
        )
        valid_lease = (
            (
                row["state"] == "leased"
                and isinstance(row["lease_owner"], str)
                and 0 < len(row["lease_owner"]) <= 128
                and isinstance(row["lease_token"], str)
                and bool(row["lease_token"])
                and isinstance(row["lease_expires_at"], (int, float))
                and math.isfinite(row["lease_expires_at"])
                and row["lease_expires_at"] >= 0
            )
            or (
                row["state"] != "leased" and row["lease_owner"] is None
                and row["lease_token"] is None
                and row["lease_expires_at"] is None
            )
        )
        valid_error = (
            row["last_error_code"] is None
            or (
                isinstance(row["last_error_code"], str)
                and _CLIENT._HTTP.ERROR_CODE.fullmatch(row["last_error_code"])
                is not None
            )
        )
        if not (
            row["singleton"] == 1
            and row["campaign_id"] == self.campaign_id
            and row["policy_sha256"] == self.policy_sha256
            and row["suite_sha256"] == self.suite_sha256
            and row["state"] in {
                "pending", "leased", "retry_wait", "succeeded", "failed",
            }
            and isinstance(row["attempts"], int)
            and 0 <= row["attempts"] <= self.max_attempts
            and row["max_attempts"] == self.max_attempts
            and row["lease_seconds"] == self.lease_seconds
            and row["base_delay_seconds"] == self.base_delay_seconds
            and row["max_delay_seconds"] == self.max_delay_seconds
            and isinstance(row["next_attempt_at"], (int, float))
            and math.isfinite(row["next_attempt_at"])
            and row["next_attempt_at"] >= 0
            and isinstance(row["updated_at"], (int, float))
            and math.isfinite(row["updated_at"])
            and row["updated_at"] >= 0
            and valid_lease and valid_error and valid_reasons
            and (no_report or valid_report)
            and (row["state"] == "succeeded") == valid_report
            and (row["state"] != "succeeded") == no_report
            and (row["state"] not in {"retry_wait", "failed"}
                 or row["last_error_code"] is not None)
            and (row["state"] in {"retry_wait", "failed"}
                 or row["last_error_code"] is None)
            and (row["state"] != "retry_wait"
                 or row["attempts"] < self.max_attempts)
        ):
            raise BenchmarkWatcherBlocked("benchmark_watcher.state_invalid")
