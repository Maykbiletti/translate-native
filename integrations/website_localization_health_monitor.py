#!/usr/bin/env python3
"""Crash-resumable, content-free polling for remote localization health.

The poller gives an operator process durable scheduling around the strict
one-request health client. It never stores the remote report: only its
canonical digest, aggregate status, stable reason codes, and cardinalities.
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
from typing import Any, Iterator


SCHEMA_VERSION = 1
MAX_FAILURES = 20
MAX_DELAY_SECONDS = 86_400.0
_META_COLUMNS = ("singleton", "schema_version")
_COLUMNS = (
    "singleton", "state", "poll_attempts", "consecutive_failures",
    "max_consecutive_failures", "next_poll_at", "lease_owner",
    "lease_token", "lease_expires_at", "last_error_code",
    "last_report_status", "last_report_checked_at", "last_report_sha256",
    "last_reason_codes_json", "last_component_count",
    "last_provider_count", "last_website_version_count", "updated_at",
)


def _load_client():
    path = Path(__file__).resolve().with_name(
        "website_localization_health_client.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_health_monitor_client", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("health client is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLIENT = _load_client()


class HealthMonitorBlocked(RuntimeError):
    """Stable fail-closed durable monitor error."""


@dataclass(frozen=True)
class HealthMonitorStatus:
    state: str
    poll_attempts: int
    consecutive_failures: int
    max_consecutive_failures: int
    next_poll_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    last_report_status: str | None
    last_report_checked_at: float | None
    last_report_sha256: str | None
    last_reason_codes: tuple[str, ...]
    last_component_count: int | None
    last_provider_count: int | None
    last_website_version_count: int | None


@dataclass(frozen=True)
class HealthPollOutcome:
    attempted: bool
    state: str
    attempt: int
    consecutive_failures: int
    next_poll_at: float
    report_status: str | None
    error_code: str | None


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HealthMonitorBlocked(f"health_monitor.{name}_invalid")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise HealthMonitorBlocked(f"health_monitor.{name}_invalid")
    return result


def _worker(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 128 or not value.isascii()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise HealthMonitorBlocked("health_monitor.worker_invalid")
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise HealthMonitorBlocked("health_monitor.report_invalid") from None


def _reason_codes(report: dict[str, Any]) -> tuple[str, ...]:
    reasons: set[str] = set()
    for component in report["components"]:
        reasons.update(component["reasons"])
    for provider in report["providers"]:
        if provider["reason"] is not None:
            reasons.add(provider["reason"])
    for website in report["website_versions"]:
        reasons.update(reason for _locale, reason in website["blocked_locales"])
    if any(
        not isinstance(reason, str)
        or _CLIENT._HTTP.ERROR_CODE.fullmatch(reason) is None
        for reason in reasons
    ):
        raise HealthMonitorBlocked("health_monitor.report_invalid")
    return tuple(sorted(reasons))


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise HealthMonitorBlocked("health_monitor.transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableWebsiteLocalizationHealthMonitor:
    """Schedule exactly one remote health read per durable polling attempt."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        client: Any,
        *,
        poll_interval_seconds: float | int = 30,
        lease_seconds: float | int = 30,
        base_delay_seconds: float | int = 5,
        max_delay_seconds: float | int = 300,
        max_consecutive_failures: int = 5,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if not callable(getattr(client, "read", None)):
            raise TypeError("client must expose read()")
        if (
            isinstance(max_consecutive_failures, bool)
            or not isinstance(max_consecutive_failures, int)
            or not 1 <= max_consecutive_failures <= MAX_FAILURES
        ):
            raise HealthMonitorBlocked("health_monitor.max_failures_invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.client = client
        self.poll_interval_seconds = _number(
            poll_interval_seconds, "delay", minimum=0.1,
            maximum=MAX_DELAY_SECONDS,
        )
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
            raise HealthMonitorBlocked("health_monitor.delay_invalid")
        self.max_consecutive_failures = max_consecutive_failures
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS localization_health_monitor_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO localization_health_monitor_meta
                VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS localization_health_monitor (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    state TEXT NOT NULL CHECK (
                        state IN ('scheduled', 'leased', 'retry_wait', 'failed')
                    ),
                    poll_attempts INTEGER NOT NULL CHECK (poll_attempts >= 0),
                    consecutive_failures INTEGER NOT NULL CHECK (
                        consecutive_failures >= 0
                    ),
                    max_consecutive_failures INTEGER NOT NULL CHECK (
                        max_consecutive_failures BETWEEN 1 AND 20
                    ),
                    next_poll_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_report_status TEXT CHECK (
                        last_report_status IS NULL OR
                        last_report_status IN ('healthy', 'degraded', 'blocked')
                    ),
                    last_report_checked_at REAL,
                    last_report_sha256 TEXT,
                    last_reason_codes_json TEXT NOT NULL,
                    last_component_count INTEGER,
                    last_provider_count INTEGER,
                    last_website_version_count INTEGER,
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
                INSERT OR IGNORE INTO localization_health_monitor (
                    singleton, state, poll_attempts, consecutive_failures,
                    max_consecutive_failures, next_poll_at,
                    last_reason_codes_json, updated_at
                ) VALUES (1, 'scheduled', 0, 0, ?, 0, '[]', 0)
            """, (self.max_consecutive_failures,))
        self._validate_schema()
        row = self._row()
        if row["max_consecutive_failures"] != self.max_consecutive_failures:
            raise HealthMonitorBlocked("health_monitor.configuration_changed")
        self._validated_row(row)

    def _validate_schema(self) -> None:
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(localization_health_monitor_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(localization_health_monitor)"
        ).fetchall())
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM localization_health_monitor_meta"
        ).fetchall()
        if (
            meta_columns != _META_COLUMNS or columns != _COLUMNS
            or len(meta) != 1 or tuple(meta[0]) != (1, SCHEMA_VERSION)
        ):
            raise HealthMonitorBlocked("health_monitor.schema_altered")

    def _row(self) -> sqlite3.Row:
        rows = self.connection.execute(
            "SELECT * FROM localization_health_monitor"
        ).fetchall()
        if len(rows) != 1:
            raise HealthMonitorBlocked("health_monitor.state_invalid")
        return rows[0]

    def run_once(self, worker_id: str, *, now: float | int) -> HealthPollOutcome:
        self._validate_schema()
        worker_id = _worker(worker_id)
        now = _number(now, "time", minimum=0, maximum=10**12)
        token = secrets.token_hex(32)
        with _transaction(self.connection):
            row = self._row()
            self._validated_row(row)
            expired = row["state"] == "leased" and row["lease_expires_at"] <= now
            due = row["state"] in {"scheduled", "retry_wait"} and row["next_poll_at"] <= now
            if not (expired or due):
                status = self._status(row, now)
                return HealthPollOutcome(
                    False, status.state, status.poll_attempts,
                    status.consecutive_failures, status.next_poll_at,
                    status.last_report_status, status.last_error_code,
                )
            self.connection.execute("""
                UPDATE localization_health_monitor
                SET state = 'leased', poll_attempts = poll_attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    updated_at = ? WHERE singleton = 1
            """, (worker_id, token, now + self.lease_seconds, now))
            attempt = int(row["poll_attempts"]) + 1
        try:
            snapshot = self.client.read()
            report = snapshot.as_payload()
            reasons = _reason_codes(report)
            report_sha256 = hashlib.sha256(_canonical(report)).hexdigest()
            report_status = report["status"]
            checked_at = float(report["checked_at"])
            component_count = len(report["components"])
            provider_count = len(report["providers"])
            website_count = len(report["website_versions"])
        except _CLIENT.HealthClientFailed as error:
            return self._finish_failure(
                worker_id, token, attempt, error.code, error.retryable, now,
            )
        except HealthMonitorBlocked:
            return self._finish_failure(
                worker_id, token, attempt, "health_monitor.report_invalid", False, now,
            )
        except Exception:
            return self._finish_failure(
                worker_id, token, attempt, "health_monitor.client_invalid", False, now,
            )
        with _transaction(self.connection):
            row = self._leased_row(worker_id, token, attempt)
            self.connection.execute("""
                UPDATE localization_health_monitor
                SET state = 'scheduled', consecutive_failures = 0,
                    next_poll_at = ?, lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    last_report_status = ?, last_report_checked_at = ?,
                    last_report_sha256 = ?, last_reason_codes_json = ?,
                    last_component_count = ?, last_provider_count = ?,
                    last_website_version_count = ?, updated_at = ?
                WHERE singleton = 1
            """, (
                now + self.poll_interval_seconds, report_status, checked_at,
                report_sha256, json.dumps(list(reasons), separators=(",", ":")),
                component_count, provider_count, website_count, now,
            ))
        return HealthPollOutcome(
            True, "scheduled", attempt, 0, now + self.poll_interval_seconds,
            report_status, None,
        )

    def _finish_failure(
        self, worker_id: str, token: str, attempt: int, code: str,
        retryable: bool, now: float,
    ) -> HealthPollOutcome:
        with _transaction(self.connection):
            row = self._leased_row(worker_id, token, attempt)
            failures = int(row["consecutive_failures"]) + 1
            retry = retryable and failures < self.max_consecutive_failures
            state = "retry_wait" if retry else "failed"
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** max(0, failures - 1)),
            )
            next_poll_at = now + delay if retry else now
            self.connection.execute("""
                UPDATE localization_health_monitor
                SET state = ?, consecutive_failures = ?, next_poll_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = ?, updated_at = ?
                WHERE singleton = 1
            """, (state, failures, next_poll_at, code, now))
        return HealthPollOutcome(
            True, state, attempt, failures, next_poll_at, None, code,
        )

    def rearm(self, *, now: float | int) -> HealthMonitorStatus:
        """Explicitly resume a terminal monitor after operator remediation."""
        self._validate_schema()
        now = _number(now, "time", minimum=0, maximum=10**12)
        with _transaction(self.connection):
            row = self._row()
            self._validated_row(row)
            if row["state"] == "leased":
                raise HealthMonitorBlocked("health_monitor.lease_active")
            self.connection.execute("""
                UPDATE localization_health_monitor
                SET state = 'scheduled', consecutive_failures = 0,
                    next_poll_at = ?, last_error_code = NULL, updated_at = ?
                WHERE singleton = 1
            """, (now, now))
        return self.status(now=now)

    def status(self, *, now: float | int) -> HealthMonitorStatus:
        self._validate_schema()
        now = _number(now, "time", minimum=0, maximum=10**12)
        row = self._row()
        self._validated_row(row)
        return self._status(row, now)

    def _status(self, row: sqlite3.Row, now: float) -> HealthMonitorStatus:
        return HealthMonitorStatus(
            state=row["state"], poll_attempts=row["poll_attempts"],
            consecutive_failures=row["consecutive_failures"],
            max_consecutive_failures=row["max_consecutive_failures"],
            next_poll_at=float(row["next_poll_at"]),
            lease_expires_at=(
                None if row["lease_expires_at"] is None
                else float(row["lease_expires_at"])
            ),
            lease_expired=(
                row["state"] == "leased" and row["lease_expires_at"] <= now
            ),
            last_error_code=row["last_error_code"],
            last_report_status=row["last_report_status"],
            last_report_checked_at=row["last_report_checked_at"],
            last_report_sha256=row["last_report_sha256"],
            last_reason_codes=tuple(json.loads(row["last_reason_codes_json"])),
            last_component_count=row["last_component_count"],
            last_provider_count=row["last_provider_count"],
            last_website_version_count=row["last_website_version_count"],
        )

    def _leased_row(self, worker_id: str, token: str, attempt: int) -> sqlite3.Row:
        row = self._row()
        self._validated_row(row)
        if (
            row["state"] != "leased" or row["lease_owner"] != worker_id
            or row["lease_token"] != token or row["poll_attempts"] != attempt
        ):
            raise HealthMonitorBlocked("health_monitor.lease_lost")
        return row

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            reasons = json.loads(row["last_reason_codes_json"])
        except (TypeError, json.JSONDecodeError, RecursionError):
            raise HealthMonitorBlocked("health_monitor.state_invalid") from None
        report_fields = (
            row["last_report_status"], row["last_report_checked_at"],
            row["last_report_sha256"], row["last_component_count"],
            row["last_provider_count"], row["last_website_version_count"],
        )
        no_report = all(value is None for value in report_fields)
        valid_report = (
            row["last_report_status"] in {"healthy", "degraded", "blocked"}
            and isinstance(row["last_report_checked_at"], (int, float))
            and math.isfinite(row["last_report_checked_at"])
            and row["last_report_checked_at"] >= 0
            and isinstance(row["last_report_sha256"], str)
            and len(row["last_report_sha256"]) == 64
            and all(character in "0123456789abcdef" for character in row["last_report_sha256"])
            and all(
                isinstance(value, int) and value >= 0
                for value in report_fields[3:]
            )
        )
        valid_reasons = (
            isinstance(reasons, list) and reasons == sorted(set(reasons))
            and all(
                isinstance(reason, str)
                and _CLIENT._HTTP.ERROR_CODE.fullmatch(reason) is not None
                for reason in reasons
            )
        )
        valid_lease = (
            (row["state"] == "leased" and row["lease_owner"] is not None
             and row["lease_token"] is not None and row["lease_expires_at"] is not None
             and isinstance(row["lease_owner"], str)
             and isinstance(row["lease_token"], str)
             and 0 < len(row["lease_owner"]) <= 128
             and len(row["lease_token"]) > 0
             and isinstance(row["lease_expires_at"], (int, float))
             and math.isfinite(row["lease_expires_at"])
             and row["lease_expires_at"] >= 0)
            or
            (row["state"] != "leased" and row["lease_owner"] is None
             and row["lease_token"] is None and row["lease_expires_at"] is None)
        )
        if not (
            row["singleton"] == 1
            and row["state"] in {"scheduled", "leased", "retry_wait", "failed"}
            and isinstance(row["poll_attempts"], int) and row["poll_attempts"] >= 0
            and isinstance(row["consecutive_failures"], int)
            and 0 <= row["consecutive_failures"] <= row["poll_attempts"]
            and row["max_consecutive_failures"] == self.max_consecutive_failures
            and isinstance(row["next_poll_at"], (int, float))
            and math.isfinite(row["next_poll_at"]) and row["next_poll_at"] >= 0
            and isinstance(row["updated_at"], (int, float))
            and math.isfinite(row["updated_at"]) and row["updated_at"] >= 0
            and valid_lease and valid_reasons and (no_report or valid_report)
            and (not no_report or reasons == [])
            and (
                row["last_error_code"] is None
                or (
                    isinstance(row["last_error_code"], str)
                    and _CLIENT._HTTP.ERROR_CODE.fullmatch(row["last_error_code"])
                )
            )
        ):
            raise HealthMonitorBlocked("health_monitor.state_invalid")
