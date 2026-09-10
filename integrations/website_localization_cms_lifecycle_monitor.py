#!/usr/bin/env python3
"""Durable source-side monitor for accepted CMS localization changes.

The monitor starts from the exact success record produced by the durable
change dispatcher. Each leased poll asks the CMS client for a freshly signed
lifecycle view; request freshness and response validation remain owned by that
client. Only content-free identities, counts, error codes, and hashes are
retained locally.
"""

from __future__ import annotations

import importlib.util
import json
import math
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1
MAX_FAILURES = 20
MAX_LEASE_SECONDS = 86_400.0
MAX_DELAY_SECONDS = 86_400.0
STATES = ("pending", "leased", "watching", "retry_wait", "terminal", "failed")
TERMINAL_SUCCESS = {"cancelled", "deleted", "published", "superseded"}
TERMINAL_FAILURE = {
    "deletion_failed", "localization_failed", "publication_blocked",
    "publication_failed",
}
REMOTE_STATUSES = TERMINAL_SUCCESS | TERMINAL_FAILURE | {
    "awaiting_approval", "deleting", "processing", "publishing",
    "queue_recovery", "ready",
}
_COLUMNS = (
    "event_id", "site_id", "plan_id", "website_version",
    "source_sequence", "job_count", "change_sha256", "binding_sha256", "state",
    "poll_attempts", "consecutive_failures", "max_consecutive_failures",
    "next_poll_at", "lease_owner", "lease_token", "lease_expires_at",
    "last_error_code", "remote_status", "lifecycle_sha256",
    "lifecycle_json",
    "required_locales_json", "approved_locales_json",
    "blocked_locales_json", "queue_counts_json", "delivery_json",
    "tombstone_json", "created_at", "updated_at",
)
_META_COLUMNS = ("singleton", "schema_version")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load lifecycle monitor dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_DISPATCH = _load_module(
    "blun_website_localization_cms_lifecycle_monitor_dispatch",
    _ROOT / "integrations" / "website_localization_cms_dispatch.py",
)
LifecycleMonitorBlocked = _DISPATCH.CMSDispatchBlocked


@dataclass(frozen=True)
class LifecyclePollClaim:
    event_id: str
    site_id: str
    plan_id: str
    website_version: str
    source_sequence: int
    job_count: int
    change_sha256: str
    attempt: int
    max_consecutive_failures: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class LifecycleMonitorStatus:
    event_id: str
    site_id: str
    plan_id: str
    website_version: str
    source_sequence: int
    job_count: int
    change_sha256: str
    binding_sha256: str
    state: str
    poll_attempts: int
    consecutive_failures: int
    max_consecutive_failures: int
    next_poll_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    remote_status: str | None
    lifecycle_sha256: str | None
    required_locales: tuple[str, ...]
    approved_locales: tuple[str, ...]
    blocked_locales: tuple[tuple[str, str], ...]
    queue_counts: dict[str, int]
    delivery: Mapping[str, Any] | None
    tombstone: Mapping[str, Any] | None


@dataclass(frozen=True)
class LifecyclePollOutcome:
    event_id: str
    state: str
    remote_status: str | None
    attempt: int
    consecutive_failures: int
    next_poll_at: float
    error_code: str | None


@dataclass(frozen=True)
class LifecycleMonitorHealth:
    status: str
    counts: dict[str, int]
    due: int
    expired_leases: int
    failed: int
    remote_failures: int


def _value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _canonical(value: Any, code: str) -> str:
    return _DISPATCH._canonical_json(value, code)


def _binding(
    event_id: str,
    site_id: str,
    plan_id: str,
    website_version: str,
    source_sequence: int,
    job_count: int,
    change_sha256: str,
    max_consecutive_failures: int,
) -> str:
    value = {
        "schema": "blun.cms-source-lifecycle-monitor-binding.v1",
        "event_id": event_id,
        "site_id": site_id,
        "plan_id": plan_id,
        "website_version": website_version,
        "source_sequence": source_sequence,
        "job_count": job_count,
        "change_sha256": change_sha256,
        "max_consecutive_failures": max_consecutive_failures,
    }
    return _DISPATCH._hash(
        _canonical(value, "lifecycle_monitor.binding_invalid")
    )


def _json(value: Any, code: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError, RecursionError):
        raise LifecycleMonitorBlocked(code) from None


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise LifecycleMonitorBlocked("lifecycle_monitor.transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableCMSLifecycleMonitor:
    """Poll accepted changes until a verified terminal CMS lifecycle state."""

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
        self.poll_interval_seconds = _DISPATCH._duration(
            poll_interval_seconds, "lifecycle_monitor.delay_invalid",
            maximum=MAX_DELAY_SECONDS,
        )
        self.base_delay_seconds = _DISPATCH._duration(
            base_delay_seconds, "lifecycle_monitor.delay_invalid",
            maximum=MAX_DELAY_SECONDS,
        )
        self.max_delay_seconds = _DISPATCH._duration(
            max_delay_seconds, "lifecycle_monitor.delay_invalid",
            maximum=MAX_DELAY_SECONDS,
        )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise LifecycleMonitorBlocked("lifecycle_monitor.delay_invalid")
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_lifecycle_monitor_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_source_lifecycle_monitor_meta
                VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_lifecycle_monitor (
                    event_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    website_version TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL CHECK (source_sequence > 0),
                    job_count INTEGER NOT NULL CHECK (job_count > 0),
                    change_sha256 TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending', 'leased', 'watching',
                                  'retry_wait', 'terminal', 'failed')
                    ),
                    poll_attempts INTEGER NOT NULL CHECK (poll_attempts >= 0),
                    consecutive_failures INTEGER NOT NULL CHECK (
                        consecutive_failures >= 0
                    ),
                    max_consecutive_failures INTEGER NOT NULL CHECK (
                        max_consecutive_failures >= 1
                        AND max_consecutive_failures <= 20
                    ),
                    next_poll_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    remote_status TEXT,
                    lifecycle_sha256 TEXT,
                    lifecycle_json TEXT,
                    required_locales_json TEXT,
                    approved_locales_json TEXT,
                    blocked_locales_json TEXT,
                    queue_counts_json TEXT,
                    delivery_json TEXT,
                    tombstone_json TEXT,
                    created_at REAL NOT NULL,
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
                CREATE INDEX IF NOT EXISTS cms_source_lifecycle_monitor_due
                ON cms_source_lifecycle_monitor (
                    state, next_poll_at, created_at, event_id
                )
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_lifecycle_monitor_meta)"
            ).fetchall()
        )
        meta = self.connection.execute(
            "SELECT singleton, schema_version "
            "FROM cms_source_lifecycle_monitor_meta"
        ).fetchall()
        columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_lifecycle_monitor)"
            ).fetchall()
        )
        if (
            meta_columns != _META_COLUMNS
            or len(meta) != 1
            or tuple(meta[0]) != (1, SCHEMA_VERSION)
            or columns != _COLUMNS
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.schema_altered")

    def register(
        self,
        change: Mapping[str, Any],
        dispatch_status: Any,
        *,
        max_consecutive_failures: int = 5,
        now: float | int,
    ) -> LifecycleMonitorStatus:
        """Bind monitoring to one validated successful dispatch result."""
        self._validate_schema()
        if (
            isinstance(max_consecutive_failures, bool)
            or not isinstance(max_consecutive_failures, int)
            or not 1 <= max_consecutive_failures <= MAX_FAILURES
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.max_failures_invalid")
        now = _DISPATCH._timestamp(now, "lifecycle_monitor.time_invalid")
        event_id, payload_json, change_sha256 = _DISPATCH._change(change)
        copied = _json(payload_json, "lifecycle_monitor.change_invalid")
        site_id = _DISPATCH._token(
            copied.get("site_id"), "lifecycle_monitor.change_invalid",
        )
        website_version = _DISPATCH._token(
            copied.get("website_version"), "lifecycle_monitor.change_invalid",
        )
        source_sequence = copied.get("source_sequence")
        plan_id = _value(dispatch_status, "remote_plan_id")
        job_count = _value(dispatch_status, "remote_job_count")
        remote_status = _value(dispatch_status, "remote_status")
        dispatch_response_sha256 = _value(dispatch_status, "response_sha256")
        if (
            _value(dispatch_status, "event_id") != event_id
            or _value(dispatch_status, "payload_sha256") != change_sha256
            or _value(dispatch_status, "status") != "succeeded"
            or not isinstance(plan_id, str)
            or _DISPATCH.TOKEN.fullmatch(plan_id) is None
            or isinstance(job_count, bool)
            or not isinstance(job_count, int)
            or job_count <= 0
            or remote_status not in {"enqueued", "superseded", "cancelled"}
            or not isinstance(dispatch_response_sha256, str)
            or _DISPATCH.SHA256.fullmatch(dispatch_response_sha256) is None
            or not isinstance(source_sequence, int)
            or isinstance(source_sequence, bool)
            or source_sequence <= 0
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.dispatch_invalid")
        initial_state = "terminal" if remote_status in {
            "superseded", "cancelled",
        } else "pending"
        initial_remote = remote_status if initial_state == "terminal" else None
        binding_sha256 = _binding(
            event_id, site_id, plan_id, website_version, source_sequence,
            job_count, change_sha256, max_consecutive_failures,
        )
        identity = (
            site_id, plan_id, website_version, source_sequence, job_count,
            change_sha256, binding_sha256, max_consecutive_failures,
        )
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM cms_source_lifecycle_monitor WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None:
                current = (
                    row["site_id"], row["plan_id"], row["website_version"],
                    row["source_sequence"], row["job_count"],
                    row["change_sha256"], row["binding_sha256"],
                    row["max_consecutive_failures"],
                )
                if current != identity:
                    raise LifecycleMonitorBlocked(
                        "lifecycle_monitor.idempotency_collision"
                    )
            else:
                self.connection.execute("""
                    INSERT INTO cms_source_lifecycle_monitor (
                        event_id, site_id, plan_id, website_version,
                        source_sequence, job_count, change_sha256,
                        binding_sha256, state,
                        poll_attempts, consecutive_failures,
                        max_consecutive_failures, next_poll_at,
                        remote_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)
                """, (
                    event_id, site_id, plan_id, website_version,
                    source_sequence, job_count, change_sha256, binding_sha256,
                    initial_state,
                    max_consecutive_failures, now, initial_remote, now, now,
                ))
        return self.status(event_id, now=now)

    def claim(
        self,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> LifecyclePollClaim | None:
        self._validate_schema()
        worker_id = _DISPATCH._token(
            worker_id, "lifecycle_monitor.worker_invalid", limit=128,
        )
        now = _DISPATCH._timestamp(now, "lifecycle_monitor.time_invalid")
        lease_seconds = _DISPATCH._duration(
            lease_seconds, "lifecycle_monitor.lease_invalid",
            maximum=MAX_LEASE_SECONDS,
        )
        integrity_error = None
        claim = None
        with _transaction(self.connection):
            expired = self.connection.execute("""
                SELECT * FROM cms_source_lifecycle_monitor
                WHERE state = 'leased' AND lease_expires_at <= ?
                ORDER BY event_id
            """, (now,)).fetchall()
            for row in expired:
                self._validated_row(row)
                failures = int(row["consecutive_failures"]) + 1
                terminal = failures >= int(row["max_consecutive_failures"])
                delay = self._retry_delay(failures)
                self.connection.execute("""
                    UPDATE cms_source_lifecycle_monitor
                    SET state = ?, consecutive_failures = ?,
                        next_poll_at = ?, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_code = 'lease_expired', updated_at = ?
                    WHERE event_id = ? AND state = 'leased'
                """, (
                    "failed" if terminal else "retry_wait", failures,
                    now if terminal else now + delay, now, row["event_id"],
                ))
            row = self.connection.execute("""
                SELECT * FROM cms_source_lifecycle_monitor
                WHERE state IN ('pending', 'watching', 'retry_wait')
                  AND next_poll_at <= ?
                ORDER BY created_at, event_id LIMIT 1
            """, (now,)).fetchone()
            if row is not None:
                try:
                    self._validated_row(row)
                except LifecycleMonitorBlocked:
                    self.connection.execute("""
                        UPDATE cms_source_lifecycle_monitor
                        SET state = 'failed', last_error_code =
                            'lifecycle_monitor.state_invalid', updated_at = ?
                        WHERE event_id = ?
                    """, (now, row["event_id"]))
                    integrity_error = "lifecycle_monitor.state_invalid"
                else:
                    token = secrets.token_urlsafe(32)
                    expires = now + lease_seconds
                    updated = self.connection.execute("""
                        UPDATE cms_source_lifecycle_monitor
                        SET state = 'leased', poll_attempts = poll_attempts + 1,
                            lease_owner = ?, lease_token = ?,
                            lease_expires_at = ?, last_error_code = NULL,
                            updated_at = ?
                        WHERE event_id = ?
                          AND state IN ('pending', 'watching', 'retry_wait')
                    """, (worker_id, token, expires, now, row["event_id"]))
                    if updated.rowcount != 1:
                        raise LifecycleMonitorBlocked(
                            "lifecycle_monitor.claim_lost"
                        )
                    claim = LifecyclePollClaim(
                        event_id=row["event_id"], site_id=row["site_id"],
                        plan_id=row["plan_id"],
                        website_version=row["website_version"],
                        source_sequence=int(row["source_sequence"]),
                        job_count=int(row["job_count"]),
                        change_sha256=row["change_sha256"],
                        attempt=int(row["poll_attempts"]) + 1,
                        max_consecutive_failures=int(
                            row["max_consecutive_failures"]
                        ),
                        lease_owner=worker_id, lease_token=token,
                        lease_expires_at=expires,
                    )
        if integrity_error is not None:
            raise LifecycleMonitorBlocked(integrity_error)
        return claim

    def run_once(
        self,
        client: Any,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> LifecyclePollOutcome | None:
        read = getattr(client, "lifecycle", None)
        if not callable(read):
            raise LifecycleMonitorBlocked("lifecycle_monitor.client_invalid")
        lease_seconds = _DISPATCH._duration(
            lease_seconds, "lifecycle_monitor.lease_invalid",
            maximum=MAX_LEASE_SECONDS,
        )
        timeout = getattr(client, "timeout", None)
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.client_invalid")
        if timeout is not None and lease_seconds <= float(timeout):
            raise LifecycleMonitorBlocked("lifecycle_monitor.lease_too_short")
        claim = self.claim(worker_id, now=now, lease_seconds=lease_seconds)
        if claim is None:
            return None
        try:
            # No request_id is supplied: the secure client creates and signs a
            # fresh purpose-bound request for every durable polling attempt.
            response = read(claim.event_id, claim.site_id)
            status = self.complete(claim, response, now=now)
        except LifecycleMonitorBlocked as error:
            if error.code != "lifecycle_monitor.response_invalid":
                raise
            status = self.fail(claim, error.code, now=now)
        except Exception as error:
            if getattr(error, "cms_client_failure", False) is True:
                code = getattr(error, "code", None)
                retryable = getattr(error, "retryable", None)
                try:
                    code, _ = _DISPATCH._error(code)
                except LifecycleMonitorBlocked:
                    code, retryable = "lifecycle_monitor.client_failure", False
                status = (
                    self.retry(claim, code, now=now)
                    if retryable is True
                    else self.fail(claim, code, now=now)
                )
            else:
                status = self.fail(
                    claim, "lifecycle_monitor.client_failure", now=now,
                )
        return LifecyclePollOutcome(
            event_id=status.event_id, state=status.state,
            remote_status=status.remote_status, attempt=status.poll_attempts,
            consecutive_failures=status.consecutive_failures,
            next_poll_at=status.next_poll_at,
            error_code=status.last_error_code,
        )

    def complete(
        self,
        claim: LifecyclePollClaim,
        response: Any,
        *,
        now: float | int,
    ) -> LifecycleMonitorStatus:
        self._validate_schema()
        claim = self._claim(claim)
        now = _DISPATCH._timestamp(now, "lifecycle_monitor.time_invalid")
        copied, response_json = self._validated_response(claim, response)
        remote_status = copied["status"]
        terminal = remote_status in TERMINAL_SUCCESS | TERMINAL_FAILURE
        snapshot = (
            _canonical(copied["required_locales"], "lifecycle_monitor.response_invalid"),
            _canonical(copied["approved_locales"], "lifecycle_monitor.response_invalid"),
            _canonical(copied["blocked_locales"], "lifecycle_monitor.response_invalid"),
            _canonical(copied["queue_counts"], "lifecycle_monitor.response_invalid"),
            _canonical(copied["delivery"], "lifecycle_monitor.response_invalid"),
            _canonical(copied["tombstone"], "lifecycle_monitor.response_invalid"),
        )
        with _transaction(self.connection):
            self._require_live_claim(claim, now)
            updated = self.connection.execute("""
                UPDATE cms_source_lifecycle_monitor
                SET state = ?, consecutive_failures = 0,
                    next_poll_at = ?, lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    remote_status = ?, lifecycle_sha256 = ?, lifecycle_json = ?,
                    required_locales_json = ?, approved_locales_json = ?,
                    blocked_locales_json = ?, queue_counts_json = ?,
                    delivery_json = ?, tombstone_json = ?, updated_at = ?
                WHERE event_id = ? AND state = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                "terminal" if terminal else "watching",
                now if terminal else now + self.poll_interval_seconds,
                remote_status, _DISPATCH._hash(response_json), response_json,
                *snapshot, now,
                claim.event_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LifecycleMonitorBlocked(
                    "lifecycle_monitor.completion_lost"
                )
        return self.status(claim.event_id, now=now)

    def retry(
        self, claim: LifecyclePollClaim, error_code: str, *, now: float | int,
    ) -> LifecycleMonitorStatus:
        return self._finish_failure(claim, error_code, retryable=True, now=now)

    def fail(
        self, claim: LifecyclePollClaim, error_code: str, *, now: float | int,
    ) -> LifecycleMonitorStatus:
        return self._finish_failure(claim, error_code, retryable=False, now=now)

    def _finish_failure(
        self,
        claim: LifecyclePollClaim,
        error_code: str,
        *,
        retryable: bool,
        now: float | int,
    ) -> LifecycleMonitorStatus:
        self._validate_schema()
        claim = self._claim(claim)
        error_code, _ = _DISPATCH._error(error_code)
        now = _DISPATCH._timestamp(now, "lifecycle_monitor.time_invalid")
        with _transaction(self.connection):
            row = self._require_live_claim(claim, now)
            failures = int(row["consecutive_failures"]) + 1
            terminal = not retryable or failures >= int(
                row["max_consecutive_failures"]
            )
            updated = self.connection.execute("""
                UPDATE cms_source_lifecycle_monitor
                SET state = ?, consecutive_failures = ?, next_poll_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = ?, updated_at = ?
                WHERE event_id = ? AND state = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                "failed" if terminal else "retry_wait", failures,
                now if terminal else now + self._retry_delay(failures),
                error_code, now, claim.event_id, claim.lease_owner,
                claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LifecycleMonitorBlocked("lifecycle_monitor.failure_lost")
        return self.status(claim.event_id, now=now)

    def status(
        self, event_id: str, *, now: float | int,
    ) -> LifecycleMonitorStatus:
        self._validate_schema()
        event_id = _DISPATCH._token(
            event_id, "lifecycle_monitor.event_invalid",
        )
        now = _DISPATCH._timestamp(now, "lifecycle_monitor.time_invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_source_lifecycle_monitor WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise LifecycleMonitorBlocked("lifecycle_monitor.event_missing")
        self._validated_row(row)
        lease = row["lease_expires_at"]
        return LifecycleMonitorStatus(
            event_id=row["event_id"], site_id=row["site_id"],
            plan_id=row["plan_id"], website_version=row["website_version"],
            source_sequence=int(row["source_sequence"]),
            job_count=int(row["job_count"]),
            change_sha256=row["change_sha256"],
            binding_sha256=row["binding_sha256"], state=row["state"],
            poll_attempts=int(row["poll_attempts"]),
            consecutive_failures=int(row["consecutive_failures"]),
            max_consecutive_failures=int(row["max_consecutive_failures"]),
            next_poll_at=float(row["next_poll_at"]),
            lease_expires_at=None if lease is None else float(lease),
            lease_expired=(
                row["state"] == "leased" and float(lease) <= now
            ),
            last_error_code=row["last_error_code"],
            remote_status=row["remote_status"],
            lifecycle_sha256=row["lifecycle_sha256"],
            required_locales=tuple(self._stored(row, "required_locales_json", [])),
            approved_locales=tuple(self._stored(row, "approved_locales_json", [])),
            blocked_locales=tuple(
                tuple(item) for item in self._stored(
                    row, "blocked_locales_json", [],
                )
            ),
            queue_counts=dict(self._stored(row, "queue_counts_json", {})),
            delivery=self._stored(row, "delivery_json", None),
            tombstone=self._stored(row, "tombstone_json", None),
        )

    def health(self, *, now: float | int) -> LifecycleMonitorHealth:
        now = _DISPATCH._timestamp(now, "lifecycle_monitor.time_invalid")
        self._validate_schema()
        quick = self.connection.execute("PRAGMA quick_check").fetchall()
        if len(quick) != 1 or tuple(quick[0]) != ("ok",):
            raise LifecycleMonitorBlocked("lifecycle_monitor.database_integrity")
        rows = self.connection.execute(
            "SELECT * FROM cms_source_lifecycle_monitor ORDER BY event_id"
        ).fetchall()
        for row in rows:
            self._validated_row(row)
        counts = {state: 0 for state in STATES}
        for row in rows:
            counts[row["state"]] += 1
        due = sum(
            1 for row in rows
            if row["state"] in {"pending", "watching", "retry_wait"}
            and row["next_poll_at"] <= now
        )
        expired = sum(
            1 for row in rows
            if row["state"] == "leased" and row["lease_expires_at"] <= now
        )
        remote_failures = sum(
            1 for row in rows if row["remote_status"] in TERMINAL_FAILURE
        )
        failed = counts["failed"]
        return LifecycleMonitorHealth(
            status="blocked" if expired or failed or remote_failures else "ok",
            counts=counts, due=due, expired_leases=expired, failed=failed,
            remote_failures=remote_failures,
        )

    def _validated_response(
        self, claim: LifecyclePollClaim, response: Any,
    ) -> tuple[dict[str, Any], str]:
        if not isinstance(response, Mapping):
            raise LifecycleMonitorBlocked("lifecycle_monitor.response_invalid")
        response_json = _canonical(
            dict(response), "lifecycle_monitor.response_invalid",
        )
        copied = _json(response_json, "lifecycle_monitor.response_invalid")
        expected = {
            "schema", "request_id", "event_id", "site_id", "plan_id",
            "website_version", "source_sequence", "status",
            "required_locales", "approved_locales", "blocked_locales",
            "queue_counts", "delivery", "tombstone",
        }
        client = _DISPATCH._CLIENT
        if (
            not isinstance(copied, dict)
            or set(copied) != expected
            or copied.get("schema") != client._API.LIFECYCLE_RESPONSE_SCHEMA
            or copied.get("event_id") != claim.event_id
            or copied.get("site_id") != claim.site_id
            or copied.get("plan_id") != claim.plan_id
            or copied.get("website_version") != claim.website_version
            or copied.get("source_sequence") != claim.source_sequence
            or copied.get("status") not in REMOTE_STATUSES - {"superseded"}
            or not client.CMSLocalizationHTTPClient._valid_lifecycle(copied)
            or len(copied.get("required_locales", [])) != claim.job_count
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.response_invalid")
        return copied, response_json

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            event_id = _DISPATCH._token(
                row["event_id"], "lifecycle_monitor.state_invalid",
            )
            site_id = _DISPATCH._token(
                row["site_id"], "lifecycle_monitor.state_invalid",
            )
            plan_id = _DISPATCH._token(
                row["plan_id"], "lifecycle_monitor.state_invalid",
            )
            website_version = _DISPATCH._token(
                row["website_version"], "lifecycle_monitor.state_invalid",
            )
            created = _DISPATCH._timestamp(
                row["created_at"], "lifecycle_monitor.state_invalid",
            )
            updated = _DISPATCH._timestamp(
                row["updated_at"], "lifecycle_monitor.state_invalid",
            )
            next_poll = _DISPATCH._timestamp(
                row["next_poll_at"], "lifecycle_monitor.state_invalid",
            )
        except (TypeError, LifecycleMonitorBlocked):
            raise LifecycleMonitorBlocked("lifecycle_monitor.state_invalid") from None
        state = row["state"]
        attempts = row["poll_attempts"]
        failures = row["consecutive_failures"]
        maximum = row["max_consecutive_failures"]
        source_sequence = row["source_sequence"]
        job_count = row["job_count"]
        error = row["last_error_code"]
        remote = row["remote_status"]
        leases = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        snapshot_columns = (
            "lifecycle_sha256", "lifecycle_json", "required_locales_json",
            "approved_locales_json", "blocked_locales_json",
            "queue_counts_json", "delivery_json", "tombstone_json",
        )
        snapshot = tuple(row[name] for name in snapshot_columns)
        has_snapshot = all(item is not None for item in snapshot)
        immediate_terminal = (
            state == "terminal" and remote in {"cancelled", "superseded"}
            and not any(item is not None for item in snapshot)
        )
        if has_snapshot:
            response = self._stored(row, "lifecycle_json", None)
            required = self._stored(row, "required_locales_json", None)
            approved = self._stored(row, "approved_locales_json", None)
            blocked = self._stored(row, "blocked_locales_json", None)
            counts = self._stored(row, "queue_counts_json", None)
            delivery = self._stored(row, "delivery_json", None)
            tombstone = self._stored(row, "tombstone_json", None)
            expected_response_keys = {
                "schema", "request_id", "event_id", "site_id", "plan_id",
                "website_version", "source_sequence", "status",
                "required_locales", "approved_locales", "blocked_locales",
                "queue_counts", "delivery", "tombstone",
            }
            valid_snapshot = (
                isinstance(response, dict)
                and set(response) == expected_response_keys
                and response.get("schema")
                == _DISPATCH._CLIENT._API.LIFECYCLE_RESPONSE_SCHEMA
                and isinstance(response.get("request_id"), str)
                and _DISPATCH.TOKEN.fullmatch(response["request_id"]) is not None
                and response.get("event_id") == event_id
                and response.get("site_id") == site_id
                and response.get("plan_id") == plan_id
                and response.get("website_version") == website_version
                and response.get("source_sequence") == source_sequence
                and response.get("status") == remote
                and response.get("required_locales") == required
                and response.get("approved_locales") == approved
                and response.get("blocked_locales") == blocked
                and response.get("queue_counts") == counts
                and response.get("delivery") == delivery
                and response.get("tombstone") == tombstone
                and _DISPATCH._CLIENT.CMSLocalizationHTTPClient._valid_lifecycle(
                    response
                )
                and len(required) == job_count
                and row["lifecycle_sha256"]
                == _DISPATCH._hash(row["lifecycle_json"])
            )
        else:
            valid_snapshot = not any(item is not None for item in snapshot)
        if (
            event_id != row["event_id"] or site_id != row["site_id"]
            or plan_id != row["plan_id"]
            or website_version != row["website_version"]
            or state not in STATES
            or isinstance(source_sequence, bool)
            or not isinstance(source_sequence, int) or source_sequence <= 0
            or isinstance(job_count, bool) or not isinstance(job_count, int)
            or job_count <= 0
            or not isinstance(row["change_sha256"], str)
            or _DISPATCH.SHA256.fullmatch(row["change_sha256"]) is None
            or not isinstance(row["binding_sha256"], str)
            or _DISPATCH.SHA256.fullmatch(row["binding_sha256"]) is None
            or row["binding_sha256"] != _binding(
                event_id, site_id, plan_id, website_version,
                source_sequence, job_count, row["change_sha256"], maximum,
            )
            or isinstance(attempts, bool) or not isinstance(attempts, int)
            or attempts < 0
            or isinstance(failures, bool) or not isinstance(failures, int)
            or isinstance(maximum, bool) or not isinstance(maximum, int)
            or not 0 <= failures <= maximum <= MAX_FAILURES or maximum < 1
            or next_poll < created or updated < created
            or (state == "leased") != all(item is not None for item in leases)
            or (state != "leased" and any(item is not None for item in leases))
            or (state == "leased" and (
                _DISPATCH._token(
                    leases[0], "lifecycle_monitor.state_invalid", limit=128,
                ) != leases[0]
                or _DISPATCH._token(
                    leases[1], "lifecycle_monitor.state_invalid",
                ) != leases[1]
                or _DISPATCH._timestamp(
                    leases[2], "lifecycle_monitor.state_invalid",
                ) <= updated
            ))
            or (error is not None and (
                not isinstance(error, str)
                or _DISPATCH.ERROR_CODE.fullmatch(error) is None
            ))
            or (state in {"retry_wait", "failed"}) != (error is not None)
            or (remote is not None and remote not in REMOTE_STATUSES)
            or not valid_snapshot
            or (not has_snapshot and remote is not None and not immediate_terminal)
            or (has_snapshot and (
                not isinstance(row["lifecycle_sha256"], str)
                or _DISPATCH.SHA256.fullmatch(row["lifecycle_sha256"]) is None
            ))
            or (remote is None and has_snapshot)
            or (has_snapshot and remote in TERMINAL_SUCCESS | TERMINAL_FAILURE
                and state != "terminal")
            or (state == "watching" and (
                not has_snapshot or remote in TERMINAL_SUCCESS | TERMINAL_FAILURE
            ))
            or (state == "terminal" and not (
                immediate_terminal
                or (has_snapshot and remote in TERMINAL_SUCCESS | TERMINAL_FAILURE)
            ))
            or (state == "pending" and (remote is not None or attempts != 0))
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.state_invalid")

    def _claim(self, claim: Any) -> LifecyclePollClaim:
        if not isinstance(claim, LifecyclePollClaim):
            raise LifecycleMonitorBlocked("lifecycle_monitor.claim_invalid")
        _DISPATCH._token(claim.event_id, "lifecycle_monitor.claim_invalid")
        _DISPATCH._token(claim.site_id, "lifecycle_monitor.claim_invalid")
        _DISPATCH._token(claim.plan_id, "lifecycle_monitor.claim_invalid")
        _DISPATCH._token(
            claim.website_version, "lifecycle_monitor.claim_invalid",
        )
        _DISPATCH._token(
            claim.lease_owner, "lifecycle_monitor.claim_invalid", limit=128,
        )
        _DISPATCH._token(claim.lease_token, "lifecycle_monitor.claim_invalid")
        if (
            isinstance(claim.source_sequence, bool)
            or not isinstance(claim.source_sequence, int)
            or claim.source_sequence <= 0
            or isinstance(claim.job_count, bool)
            or not isinstance(claim.job_count, int) or claim.job_count <= 0
            or not isinstance(claim.change_sha256, str)
            or _DISPATCH.SHA256.fullmatch(claim.change_sha256) is None
            or isinstance(claim.attempt, bool)
            or not isinstance(claim.attempt, int) or claim.attempt <= 0
            or isinstance(claim.max_consecutive_failures, bool)
            or not isinstance(claim.max_consecutive_failures, int)
            or not 1 <= claim.max_consecutive_failures <= MAX_FAILURES
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.claim_invalid")
        _DISPATCH._timestamp(
            claim.lease_expires_at, "lifecycle_monitor.claim_invalid",
        )
        return claim

    def _require_live_claim(
        self, claim: LifecyclePollClaim, now: float,
    ) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM cms_source_lifecycle_monitor WHERE event_id = ?",
            (claim.event_id,),
        ).fetchone()
        if row is None:
            raise LifecycleMonitorBlocked("lifecycle_monitor.claim_missing")
        self._validated_row(row)
        if (
            row["state"] != "leased"
            or row["site_id"] != claim.site_id
            or row["plan_id"] != claim.plan_id
            or row["website_version"] != claim.website_version
            or row["source_sequence"] != claim.source_sequence
            or row["job_count"] != claim.job_count
            or row["change_sha256"] != claim.change_sha256
            or row["poll_attempts"] != claim.attempt
            or row["max_consecutive_failures"] != claim.max_consecutive_failures
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
            or row["lease_expires_at"] != claim.lease_expires_at
        ):
            raise LifecycleMonitorBlocked("lifecycle_monitor.claim_lost")
        if row["lease_expires_at"] <= now:
            raise LifecycleMonitorBlocked("lifecycle_monitor.lease_expired")
        if now < row["updated_at"]:
            raise LifecycleMonitorBlocked("lifecycle_monitor.clock_regressed")
        return row

    def _stored(self, row: sqlite3.Row, name: str, empty: Any) -> Any:
        raw = row[name]
        if raw is None:
            return empty
        value = _json(raw, "lifecycle_monitor.state_invalid")
        if _canonical(value, "lifecycle_monitor.state_invalid") != raw:
            raise LifecycleMonitorBlocked("lifecycle_monitor.state_invalid")
        return value

    def _retry_delay(self, failures: int) -> float:
        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, failures - 1)),
        )
