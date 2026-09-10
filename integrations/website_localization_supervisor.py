#!/usr/bin/env python3
"""Durable single-instance supervisor for the localization service tick.

The host injects the already configured service tick, clock, and sleeper. The
supervisor persists only operational metadata and never stores customer text,
provider responses, signatures, credentials, or exception messages.
"""

from __future__ import annotations

import math
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping


SCHEMA = "blun.website-localization-supervisor.v1"
TICK_SCHEMA = "blun.website-localization-service-tick.v1"
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
PHASES = {
    "tombstone", "delivery", "ingress", "release", "translation", "benchmark",
    "idle", "supervisor",
}
STATUSES = {
    "idle", "succeeded", "retry_wait", "failed", "blocked", "approved",
    "delivery_ready", "delivered", "enqueued", "superseded", "cancelled",
}
BLOCKED_STATUSES = {"retry_wait", "failed", "blocked"}
_COLUMNS = (
    "singleton", "schema", "revision", "lease_owner", "lease_token",
    "lease_expires_at", "next_tick_at", "consecutive_blocked",
    "last_started_at", "last_finished_at", "last_phase", "last_status",
    "last_error_code", "updated_at",
)


class LocalizationSupervisorBlocked(RuntimeError):
    """Stable supervisor failure that contains no stored customer value."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SupervisorPolicy:
    lease_seconds: float = 300.0
    active_delay_seconds: float = 0.05
    idle_delay_seconds: float = 1.0
    blocked_base_seconds: float = 5.0
    blocked_max_seconds: float = 300.0
    stop_poll_seconds: float = 1.0

    def validated(self) -> "SupervisorPolicy":
        values = asdict(self)
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LocalizationSupervisorBlocked("supervisor.policy.invalid")
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise LocalizationSupervisorBlocked("supervisor.policy.invalid")
            values[name] = number
        if values["blocked_base_seconds"] > values["blocked_max_seconds"]:
            raise LocalizationSupervisorBlocked("supervisor.policy.invalid")
        return SupervisorPolicy(**values)


@dataclass(frozen=True)
class SupervisorStatus:
    schema: str
    status: str
    revision: int
    lease_active: bool
    lease_expires_at: float | None
    next_tick_at: float
    consecutive_blocked: int
    last_started_at: float | None
    last_finished_at: float | None
    last_phase: str | None
    last_status: str | None
    last_error_code: str | None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SupervisorRunOutcome:
    schema: str
    status: str
    next_tick_at: float
    tick: Mapping[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "next_tick_at": self.next_tick_at,
            "tick": dict(self.tick) if self.tick is not None else None,
        }


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalizationSupervisorBlocked("supervisor.clock.invalid")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise LocalizationSupervisorBlocked("supervisor.clock.invalid")
    return value


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise LocalizationSupervisorBlocked(code)
    return value


@contextmanager
def _transaction(connection: sqlite3.Connection):
    if connection.in_transaction:
        raise LocalizationSupervisorBlocked("supervisor.state.transaction_active")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _tick_payload(value: Any) -> dict[str, Any]:
    method = getattr(value, "as_payload", None)
    if callable(method):
        value = method()
    if not isinstance(value, Mapping):
        raise LocalizationSupervisorBlocked("supervisor.tick.result_invalid")
    payload = dict(value)
    expected = {
        "schema", "phase", "status", "event_id", "plan_id", "job_id",
        "target_locale", "delivery_id", "attempt", "error_code",
    }
    if set(payload) != expected or payload.get("schema") != TICK_SCHEMA:
        raise LocalizationSupervisorBlocked("supervisor.tick.result_invalid")
    phase = _identifier(payload.get("phase"), "supervisor.tick.result_invalid")
    status = _identifier(payload.get("status"), "supervisor.tick.result_invalid")
    if phase not in PHASES or status not in STATUSES:
        raise LocalizationSupervisorBlocked("supervisor.tick.result_invalid")
    for name in ("event_id", "plan_id", "job_id", "target_locale", "delivery_id"):
        value = payload.get(name)
        if value is not None:
            _identifier(value, "supervisor.tick.result_invalid")
    attempt = payload.get("attempt")
    if attempt is not None and (
        isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1
    ):
        raise LocalizationSupervisorBlocked("supervisor.tick.result_invalid")
    error_code = payload.get("error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or ERROR_CODE.fullmatch(error_code) is None
    ):
        raise LocalizationSupervisorBlocked("supervisor.tick.result_invalid")
    if (status in BLOCKED_STATUSES) != (error_code is not None):
        raise LocalizationSupervisorBlocked("supervisor.tick.result_invalid")
    del phase
    return payload


class LocalizationServiceSupervisor:
    """Lease and schedule a host-configured service tick durably."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        tick: Callable[[], Any],
        *,
        worker_id: str,
        policy: SupervisorPolicy = SupervisorPolicy(),
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise LocalizationSupervisorBlocked("supervisor.state.connection_invalid")
        if not callable(tick) or not callable(clock):
            raise LocalizationSupervisorBlocked("supervisor.dependency.invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.tick = tick
        self.worker_id = _identifier(worker_id, "supervisor.worker_id.invalid")
        self.policy = policy.validated() if isinstance(policy, SupervisorPolicy) else None
        if self.policy is None:
            raise LocalizationSupervisorBlocked("supervisor.policy.invalid")
        self.clock = clock
        self.token_factory = token_factory or (lambda: secrets.token_hex(32))
        self._active_token: str | None = None
        self._create_schema()
        self._verify_schema()

    def _create_schema(self) -> None:
        with _transaction(self.connection):
            self.connection.execute(f"""
                CREATE TABLE IF NOT EXISTS localization_service_supervisor (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema TEXT NOT NULL CHECK (schema = '{SCHEMA}'),
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    next_tick_at REAL NOT NULL,
                    consecutive_blocked INTEGER NOT NULL CHECK (consecutive_blocked >= 0),
                    last_started_at REAL,
                    last_finished_at REAL,
                    last_phase TEXT,
                    last_status TEXT,
                    last_error_code TEXT,
                    updated_at REAL NOT NULL,
                    CHECK (
                        (lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
                        OR
                        (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                    )
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO localization_service_supervisor (
                    singleton, schema, revision, next_tick_at,
                    consecutive_blocked, updated_at
                ) VALUES (1, ?, 0, 0, 0, 0)
            """, (SCHEMA,))

    def _verify_schema(self) -> None:
        columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(localization_service_supervisor)"
            )
        )
        if columns != _COLUMNS:
            raise LocalizationSupervisorBlocked("supervisor.state.schema_altered")
        rows = self.connection.execute(
            "SELECT * FROM localization_service_supervisor"
        ).fetchall()
        if len(rows) != 1:
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        self._status_from_row(rows[0], 0)

    @staticmethod
    def _status_from_row(row: sqlite3.Row, now: float) -> SupervisorStatus:
        if row["singleton"] != 1 or row["schema"] != SCHEMA:
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        revision = row["revision"]
        blocked = row["consecutive_blocked"]
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (revision, blocked)
        ):
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        next_tick_at = _timestamp(row["next_tick_at"])
        updated_at = _timestamp(row["updated_at"])
        del updated_at
        lease = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        if lease == (None, None, None):
            lease_active = False
            lease_expires_at = None
            status = "waiting" if next_tick_at > now else "ready"
        else:
            _identifier(lease[0], "supervisor.state.invalid")
            _identifier(lease[1], "supervisor.state.invalid")
            lease_expires_at = _timestamp(lease[2])
            lease_active = lease_expires_at > now
            status = "leased" if lease_active else "recoverable"
        times = []
        for name in ("last_started_at", "last_finished_at"):
            value = row[name]
            times.append(None if value is None else _timestamp(value))
        if times[1] is not None and times[0] is None:
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        if times[0] is not None and times[1] is not None and times[1] < times[0]:
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        last_phase, last_status, last_error = (
            row["last_phase"], row["last_status"], row["last_error_code"]
        )
        if (last_phase is None) != (last_status is None):
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        if last_phase is not None:
            _identifier(last_phase, "supervisor.state.invalid")
            _identifier(last_status, "supervisor.state.invalid")
            if last_phase not in PHASES or last_status not in STATUSES:
                raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        if last_error is not None and (
            not isinstance(last_error, str) or ERROR_CODE.fullmatch(last_error) is None
        ):
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        if last_status is None:
            if last_error is not None or blocked != 0:
                raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        elif (last_status in BLOCKED_STATUSES) != (last_error is not None):
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        if (blocked > 0) != (last_status in BLOCKED_STATUSES):
            raise LocalizationSupervisorBlocked("supervisor.state.invalid")
        return SupervisorStatus(
            SCHEMA, status, revision, lease_active, lease_expires_at,
            next_tick_at, blocked, times[0], times[1], last_phase,
            last_status, last_error,
        )

    def status(self, *, now: float | int | None = None) -> SupervisorStatus:
        now = _timestamp(self.clock() if now is None else now)
        try:
            self._verify_schema()
            row = self.connection.execute(
                "SELECT * FROM localization_service_supervisor WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise LocalizationSupervisorBlocked("supervisor.state.invalid")
            return self._status_from_row(row, now)
        except LocalizationSupervisorBlocked:
            raise
        except Exception:
            raise LocalizationSupervisorBlocked("supervisor.state.unavailable") from None

    def _claim(self, now: float) -> tuple[str | None, SupervisorRunOutcome | None]:
        token = _identifier(
            self.token_factory(), "supervisor.lease_token.invalid",
        )
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM localization_service_supervisor WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise LocalizationSupervisorBlocked("supervisor.state.invalid")
            status = self._status_from_row(row, now)
            if status.lease_active:
                return None, SupervisorRunOutcome(
                    SCHEMA, "leased", status.lease_expires_at or now,
                )
            if status.status == "waiting":
                return None, SupervisorRunOutcome(
                    SCHEMA, "waiting", status.next_tick_at,
                )
            expires = now + self.policy.lease_seconds
            updated = self.connection.execute("""
                UPDATE localization_service_supervisor
                SET revision = revision + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    last_started_at = ?, last_finished_at = NULL, updated_at = ?
                WHERE singleton = 1 AND revision = ?
            """, (
                self.worker_id, token, expires, now, now, status.revision,
            ))
            if updated.rowcount != 1:
                raise LocalizationSupervisorBlocked("supervisor.state.conflict")
        return token, None

    def _finish(self, token: str, tick: dict[str, Any], now: float) -> float:
        blocked = tick["status"] in BLOCKED_STATUSES
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM localization_service_supervisor WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise LocalizationSupervisorBlocked("supervisor.state.invalid")
            status = self._status_from_row(row, now)
            if (
                row["lease_owner"] != self.worker_id
                or row["lease_token"] != token
                or row["lease_expires_at"] is None
                or _timestamp(row["lease_expires_at"]) <= now
            ):
                raise LocalizationSupervisorBlocked("supervisor.lease_lost")
            failures = status.consecutive_blocked + 1 if blocked else 0
            if blocked:
                delay = min(
                    self.policy.blocked_max_seconds,
                    self.policy.blocked_base_seconds * (2 ** min(failures - 1, 30)),
                )
            elif tick["phase"] == "idle" and tick["status"] == "idle":
                delay = self.policy.idle_delay_seconds
            else:
                delay = self.policy.active_delay_seconds
            next_tick_at = now + delay
            updated = self.connection.execute("""
                UPDATE localization_service_supervisor
                SET revision = revision + 1,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    next_tick_at = ?, consecutive_blocked = ?,
                    last_finished_at = ?, last_phase = ?, last_status = ?,
                    last_error_code = ?, updated_at = ?
                WHERE singleton = 1 AND lease_owner = ? AND lease_token = ?
            """, (
                next_tick_at, failures, now, tick["phase"], tick["status"],
                tick["error_code"], now, self.worker_id, token,
            ))
            if updated.rowcount != 1:
                raise LocalizationSupervisorBlocked("supervisor.lease_lost")
        return next_tick_at

    def renew_active_lease(self, minimum_child_seconds: float | int) -> float:
        """Renew the current outer lease immediately before child work."""
        if (
            isinstance(minimum_child_seconds, bool)
            or not isinstance(minimum_child_seconds, (int, float))
        ):
            raise LocalizationSupervisorBlocked("supervisor.lease_guard.invalid")
        minimum_child_seconds = float(minimum_child_seconds)
        if (
            not math.isfinite(minimum_child_seconds)
            or minimum_child_seconds <= 0
            or minimum_child_seconds >= self.policy.lease_seconds
        ):
            raise LocalizationSupervisorBlocked("supervisor.lease_guard.invalid")
        token = self._active_token
        if token is None:
            raise LocalizationSupervisorBlocked("supervisor.lease_guard.inactive")
        now = _timestamp(self.clock())
        expires = now + self.policy.lease_seconds
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM localization_service_supervisor WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise LocalizationSupervisorBlocked("supervisor.state.invalid")
            status = self._status_from_row(row, now)
            if (
                not status.lease_active
                or row["lease_owner"] != self.worker_id
                or row["lease_token"] != token
            ):
                raise LocalizationSupervisorBlocked("supervisor.lease_lost")
            updated = self.connection.execute("""
                UPDATE localization_service_supervisor
                SET revision = revision + 1, lease_expires_at = ?, updated_at = ?
                WHERE singleton = 1 AND lease_owner = ? AND lease_token = ?
                  AND lease_expires_at > ? AND revision = ?
            """, (
                expires, now, self.worker_id, token, now, status.revision,
            ))
            if updated.rowcount != 1:
                raise LocalizationSupervisorBlocked("supervisor.lease_lost")
        return expires

    def run_once(self, *, now: float | int | None = None) -> SupervisorRunOutcome:
        started = _timestamp(self.clock() if now is None else now)
        try:
            token, skipped = self._claim(started)
        except LocalizationSupervisorBlocked:
            raise
        except Exception:
            raise LocalizationSupervisorBlocked("supervisor.state.unavailable") from None
        if skipped is not None:
            return skipped
        assert token is not None
        try:
            self._active_token = token
            try:
                try:
                    tick = _tick_payload(self.tick())
                except Exception:
                    tick = {
                        "schema": TICK_SCHEMA,
                        "phase": "supervisor",
                        "status": "blocked",
                        "event_id": None,
                        "plan_id": None,
                        "job_id": None,
                        "target_locale": None,
                        "delivery_id": None,
                        "attempt": None,
                        "error_code": "supervisor.tick.unhandled",
                    }
            finally:
                self._active_token = None
            finished = _timestamp(self.clock())
            if finished < started:
                raise LocalizationSupervisorBlocked("supervisor.clock.invalid")
            next_tick_at = self._finish(token, tick, finished)
            return SupervisorRunOutcome(SCHEMA, "ran", next_tick_at, tick)
        except LocalizationSupervisorBlocked:
            raise
        except Exception:
            raise LocalizationSupervisorBlocked("supervisor.state.unavailable") from None

    def run_forever(
        self,
        *,
        stop_requested: Callable[[], bool],
        sleeper: Callable[[float], Any] = time.sleep,
    ) -> SupervisorStatus:
        if not callable(stop_requested) or not callable(sleeper):
            raise LocalizationSupervisorBlocked("supervisor.dependency.invalid")
        while True:
            try:
                should_stop = stop_requested()
            except Exception:
                raise LocalizationSupervisorBlocked("supervisor.stop.invalid") from None
            if should_stop is not False and should_stop is not True:
                raise LocalizationSupervisorBlocked("supervisor.stop.invalid")
            if should_stop:
                return self.status()
            outcome = self.run_once()
            now = _timestamp(self.clock())
            delay = min(
                self.policy.stop_poll_seconds,
                max(0.0, outcome.next_tick_at - now),
            )
            if delay > 0:
                sleeper(delay)
