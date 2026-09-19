#!/usr/bin/env python3
"""Durable, explicitly started recovery for a remote benchmark watcher.

The runner performs contract discovery, failed-generation observation, and
idempotent rearm as three separately leased network steps.  It persists only
content-free hashes and the exact opaque request identity needed to resume an
uncertain rearm after a crash.
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
MAX_ATTEMPTS = 100
MAX_DELAY_SECONDS = 86_400.0
STATES = {"pending", "leased", "retry_wait", "succeeded", "not_required", "failed"}
PHASES = {"openapi", "status", "rearm"}
_META_COLUMNS = ("singleton", "schema_version")
_COLUMNS = (
    "singleton", "client_binding_sha256", "operation_sha256", "request_id",
    "state", "phase", "attempts", "max_attempts", "lease_seconds",
    "base_delay_seconds", "max_delay_seconds", "next_attempt_at",
    "lease_owner", "lease_token", "lease_expires_at", "contract_sha256",
    "openapi_sha256", "failed_generation_attempts", "failed_at",
    "failed_error_code", "request_sha256", "receipt_sha256", "completed_at",
    "last_error_code", "updated_at",
)


def _load_client():
    path = Path(__file__).resolve().with_name(
        "website_localization_benchmark_watcher_control_client.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_benchmark_watcher_recovery_runner_client",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark watcher control client is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLIENT = _load_client()
_CONTROL = _CLIENT._CONTROL


class BenchmarkWatcherRecoveryRunnerBlocked(RuntimeError):
    """Stable, content-free durable recovery failure."""


@dataclass(frozen=True)
class RecoveryRunnerStatus:
    state: str
    phase: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    contract_sha256: str | None
    openapi_sha256: str | None
    generation_observed: bool
    request_sha256: str | None
    receipt_sha256: str | None
    completed_at: float | None
    last_error_code: str | None


@dataclass(frozen=True)
class RecoveryRunnerOutcome:
    attempted: bool
    state: str
    phase: str
    attempt: int
    next_attempt_at: float
    error_code: str | None


@dataclass(frozen=True)
class _Claim:
    phase: str
    attempt: int
    worker_id: str
    lease_token: str
    generation_attempts: int | None
    failed_at: float | None
    failed_error_code: str | None
    request_id: str


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkWatcherRecoveryRunnerBlocked(
            f"benchmark_watcher.recovery_runner.{name}_invalid"
        )
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise BenchmarkWatcherRecoveryRunnerBlocked(
            f"benchmark_watcher.recovery_runner.{name}_invalid"
        )
    return result


def _worker(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 128 or not value.isascii()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise BenchmarkWatcherRecoveryRunnerBlocked(
            "benchmark_watcher.recovery_runner.worker_invalid"
        )
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise BenchmarkWatcherRecoveryRunnerBlocked(
            "benchmark_watcher.recovery_runner.state_invalid"
        ) from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise BenchmarkWatcherRecoveryRunnerBlocked(
            "benchmark_watcher.recovery_runner.transaction_nested"
        )
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableBenchmarkWatcherRecoveryRunner:
    """Resume one explicit remote watcher-recovery operation after crashes."""

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
        if any(
            not callable(getattr(client, name, None))
            for name in ("openapi", "status", "rearm")
        ):
            raise TypeError("client must expose openapi(), status(), and rearm()")
        origin = getattr(client, "origin", None)
        if not isinstance(origin, str) or not origin:
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.client_invalid"
            )
        if (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or not 3 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.max_attempts_invalid"
            )
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.client = client
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
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.delay_invalid"
            )
        self.max_attempts = max_attempts
        self.client_binding_sha256 = _sha256(_canonical({
            "schema": "blun.website-localization-benchmark-watcher-recovery-client-binding.v1",
            "origin": origin,
            "control_contract_sha256": _CONTROL._OPENAPI.document_sha256(
                _CONTROL._openapi_contract()
            ),
            "lease_seconds": self.lease_seconds,
            "base_delay_seconds": self.base_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "max_attempts": self.max_attempts,
        }))
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_watcher_recovery_runner_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO benchmark_watcher_recovery_runner_meta
                VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_watcher_recovery_runner (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    client_binding_sha256 TEXT NOT NULL,
                    operation_sha256 TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'pending', 'leased', 'retry_wait', 'succeeded',
                        'not_required', 'failed'
                    )),
                    phase TEXT NOT NULL CHECK (phase IN ('openapi', 'status', 'rearm')),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 3 AND 100),
                    lease_seconds REAL NOT NULL CHECK (lease_seconds > 0),
                    base_delay_seconds REAL NOT NULL CHECK (base_delay_seconds > 0),
                    max_delay_seconds REAL NOT NULL CHECK (
                        max_delay_seconds >= base_delay_seconds
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    contract_sha256 TEXT,
                    openapi_sha256 TEXT,
                    failed_generation_attempts INTEGER,
                    failed_at REAL,
                    failed_error_code TEXT,
                    request_sha256 TEXT,
                    receipt_sha256 TEXT,
                    completed_at REAL,
                    last_error_code TEXT,
                    updated_at REAL NOT NULL,
                    CHECK (
                        (state = 'leased' AND lease_owner IS NOT NULL
                         AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                        OR
                        (state <> 'leased' AND lease_owner IS NULL
                         AND lease_token IS NULL AND lease_expires_at IS NULL)
                    )
                )
            """)
        self._validate_schema()
        row = self._row(required=False)
        if row is not None:
            self._validated_row(row)

    def _validate_schema(self) -> None:
        meta_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(benchmark_watcher_recovery_runner_meta)"
            ).fetchall()
        )
        columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(benchmark_watcher_recovery_runner)"
            ).fetchall()
        )
        meta = self.connection.execute(
            "SELECT singleton, schema_version "
            "FROM benchmark_watcher_recovery_runner_meta"
        ).fetchall()
        if (
            meta_columns != _META_COLUMNS or columns != _COLUMNS
            or len(meta) != 1 or tuple(meta[0]) != (1, SCHEMA_VERSION)
        ):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.schema_invalid"
            )

    def start(self, operation_id: str, *, now: float | int) -> RecoveryRunnerStatus:
        """Durably record explicit operator intent without network access."""
        self._validate_schema()
        now_value = _number(now, "time", minimum=0, maximum=10**12)
        if (
            not isinstance(operation_id, str)
            or _CONTROL.IDENTIFIER.fullmatch(operation_id) is None
        ):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.operation_invalid"
            )
        operation_sha256 = _sha256(_canonical({
            "schema": "blun.website-localization-benchmark-watcher-recovery-operation.v1",
            "operation_id": operation_id,
        }))
        request_id = "watcher-recovery-" + operation_sha256
        with _transaction(self.connection):
            row = self._row(required=False)
            if row is not None:
                self._validated_row(row)
                if row["operation_sha256"] != operation_sha256:
                    raise BenchmarkWatcherRecoveryRunnerBlocked(
                        "benchmark_watcher.recovery_runner.operation_conflict"
                    )
            else:
                self.connection.execute("""
                    INSERT INTO benchmark_watcher_recovery_runner (
                        singleton, client_binding_sha256, operation_sha256,
                        request_id, state, phase, attempts, max_attempts,
                        lease_seconds, base_delay_seconds, max_delay_seconds,
                        next_attempt_at, lease_owner, lease_token,
                        lease_expires_at, contract_sha256, openapi_sha256,
                        failed_generation_attempts, failed_at,
                        failed_error_code, request_sha256, receipt_sha256,
                        completed_at, last_error_code, updated_at
                    ) VALUES (
                        1, ?, ?, ?, 'pending', 'openapi', 0, ?, ?, ?, ?, ?,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                        NULL, NULL, NULL, NULL, ?
                    )
                """, (
                    self.client_binding_sha256, operation_sha256, request_id,
                    self.max_attempts, self.lease_seconds,
                    self.base_delay_seconds, self.max_delay_seconds,
                    now_value, now_value,
                ))
        return self.status(now=now_value)

    def status(self, *, now: float | int) -> RecoveryRunnerStatus:
        self._validate_schema()
        now_value = _number(now, "time", minimum=0, maximum=10**12)
        row = self._row()
        self._validated_row(row)
        return self._status(row, now_value)

    def run_once(
        self, worker_id: str, *, now: float | int,
    ) -> RecoveryRunnerOutcome:
        """Perform at most one leased discovery, status, or rearm request."""
        self._validate_schema()
        worker = _worker(worker_id)
        now_value = _number(now, "time", minimum=0, maximum=10**12)
        claim = self._claim(worker, now_value)
        if claim is None:
            current = self.status(now=now_value)
            return RecoveryRunnerOutcome(
                False, current.state, current.phase, current.attempts,
                current.next_attempt_at, current.last_error_code,
            )
        try:
            if claim.phase == "openapi":
                snapshot = self.client.openapi()
                return self._complete_openapi(claim, snapshot, now_value)
            if claim.phase == "status":
                snapshot = self.client.status()
                return self._complete_status(claim, snapshot, now_value)
            snapshot = self.client.rearm(
                request_id=claim.request_id,
                expected_attempts=claim.generation_attempts,
                expected_failed_at=claim.failed_at,
                expected_error_code=claim.failed_error_code,
            )
            return self._complete_rearm(claim, snapshot, now_value)
        except BenchmarkWatcherRecoveryRunnerBlocked:
            raise
        except Exception as error:
            is_client_failure = bool(getattr(
                error, "benchmark_watcher_control_client_failure", False,
            ))
            code = getattr(error, "code", None) if is_client_failure else None
            retryable = getattr(error, "retryable", None) if is_client_failure else None
            if (
                not isinstance(code, str)
                or _CONTROL.ERROR_CODE.fullmatch(code) is None
                or type(retryable) is not bool
            ):
                code = "benchmark_watcher.recovery_runner.client_invalid"
                retryable = False
            return self._fail_attempt(
                claim, code=code, retryable=retryable, now=now_value,
            )

    def run_forever(
        self, worker_id: str, *, clock: Callable[[], float], stop_event: Any,
        maximum_wait_seconds: float | int = 30,
    ) -> RecoveryRunnerStatus:
        """Run until recovery is final or the host requests a clean stop."""
        worker = _worker(worker_id)
        if not callable(clock):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.clock_invalid"
            )
        is_set = getattr(stop_event, "is_set", None)
        wait = getattr(stop_event, "wait", None)
        if not callable(is_set) or not callable(wait):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.stop_event_invalid"
            )
        maximum_wait = _number(
            maximum_wait_seconds, "delay", minimum=0.1, maximum=3600,
        )
        while True:
            try:
                now = _number(clock(), "time", minimum=0, maximum=10**12)
                stopped = is_set()
            except BenchmarkWatcherRecoveryRunnerBlocked:
                raise
            except Exception:
                raise BenchmarkWatcherRecoveryRunnerBlocked(
                    "benchmark_watcher.recovery_runner.clock_invalid"
                ) from None
            if type(stopped) is not bool:
                raise BenchmarkWatcherRecoveryRunnerBlocked(
                    "benchmark_watcher.recovery_runner.stop_event_invalid"
                )
            if stopped:
                return self.status(now=now)
            self.run_once(worker, now=now)
            current = self.status(now=now)
            if current.state in {"succeeded", "not_required", "failed"}:
                return current
            action_at = (
                current.lease_expires_at
                if current.state == "leased" else current.next_attempt_at
            )
            if action_at is None:
                raise BenchmarkWatcherRecoveryRunnerBlocked(
                    "benchmark_watcher.recovery_runner.state_invalid"
                )
            try:
                awakened = wait(min(maximum_wait, max(0.1, action_at - now)))
            except Exception:
                raise BenchmarkWatcherRecoveryRunnerBlocked(
                    "benchmark_watcher.recovery_runner.stop_event_invalid"
                ) from None
            if type(awakened) is not bool:
                raise BenchmarkWatcherRecoveryRunnerBlocked(
                    "benchmark_watcher.recovery_runner.stop_event_invalid"
                )

    def _claim(self, worker_id: str, now: float) -> _Claim | None:
        with _transaction(self.connection):
            row = self._row()
            self._validated_row(row)
            if row["state"] in {"succeeded", "not_required", "failed"}:
                return None
            due = (
                row["state"] == "leased" and row["lease_expires_at"] <= now
            ) or (
                row["state"] in {"pending", "retry_wait"}
                and row["next_attempt_at"] <= now
            )
            if not due:
                return None
            attempt = row["attempts"] + 1
            token = secrets.token_hex(16)
            self.connection.execute("""
                UPDATE benchmark_watcher_recovery_runner
                SET state = 'leased', attempts = ?, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?, updated_at = ?
                WHERE singleton = 1
            """, (
                attempt, worker_id, token, now + self.lease_seconds, now,
            ))
            return _Claim(
                row["phase"], attempt, worker_id, token,
                row["failed_generation_attempts"], row["failed_at"],
                row["failed_error_code"], row["request_id"],
            )

    def _complete_openapi(
        self, claim: _Claim, snapshot: Any, now: float,
    ) -> RecoveryRunnerOutcome:
        contract_sha256 = getattr(snapshot, "contract_sha256", None)
        openapi_sha256 = getattr(snapshot, "openapi_sha256", None)
        if (
            not isinstance(contract_sha256, str)
            or _CONTROL.SHA256.fullmatch(contract_sha256) is None
            or not isinstance(openapi_sha256, str)
            or _CONTROL.SHA256.fullmatch(openapi_sha256) is None
        ):
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.client_invalid",
                retryable=False, now=now,
            )
        if claim.attempt + 2 > self.max_attempts:
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.attempts_exhausted",
                retryable=False, now=now,
            )
        with _transaction(self.connection):
            self._leased_row(claim)
            self.connection.execute("""
                UPDATE benchmark_watcher_recovery_runner
                SET state = 'pending', phase = 'status', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, contract_sha256 = ?,
                    openapi_sha256 = ?, last_error_code = NULL, updated_at = ?
                WHERE singleton = 1
            """, (now, contract_sha256, openapi_sha256, now))
        return RecoveryRunnerOutcome(
            True, "pending", "status", claim.attempt, now, None,
        )

    def _complete_status(
        self, claim: _Claim, snapshot: Any, now: float,
    ) -> RecoveryRunnerOutcome:
        state = getattr(snapshot, "state", None)
        rearmable = getattr(snapshot, "rearmable", None)
        generation = getattr(snapshot, "generation", None)
        checked_at_value = getattr(snapshot, "checked_at", None)
        try:
            checked_at = _CONTROL._timestamp(checked_at_value)
        except (TypeError, ValueError):
            checked_at = None
        if (
            state not in {"pending", "leased", "retry_wait", "succeeded", "failed"}
            or type(rearmable) is not bool or checked_at is None
        ):
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.client_invalid",
                retryable=False, now=now,
            )
        if not rearmable:
            if generation is not None or state == "failed":
                return self._fail_attempt(
                    claim,
                    code="benchmark_watcher.recovery_runner.client_invalid",
                    retryable=False, now=now,
                )
            with _transaction(self.connection):
                self._leased_row(claim)
                self.connection.execute("""
                    UPDATE benchmark_watcher_recovery_runner
                    SET state = 'not_required', lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        completed_at = ?, last_error_code = NULL, updated_at = ?
                    WHERE singleton = 1
                """, (now, now))
            return RecoveryRunnerOutcome(
                True, "not_required", "status", claim.attempt, now, None,
            )
        if not isinstance(generation, dict) or set(generation) != {
            "attempts", "failed_at", "error_code",
        }:
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.client_invalid",
                retryable=False, now=now,
            )
        try:
            generation_attempts = generation["attempts"]
            failed_at = _CONTROL._timestamp(generation["failed_at"])
            error_code = generation["error_code"]
        except (KeyError, TypeError, ValueError):
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.client_invalid",
                retryable=False, now=now,
            )
        if (
            state != "failed" or isinstance(generation_attempts, bool)
            or not isinstance(generation_attempts, int)
            or not 1 <= generation_attempts <= _CONTROL._WATCHER.MAX_ATTEMPTS
            or not isinstance(error_code, str)
            or _CONTROL.ERROR_CODE.fullmatch(error_code) is None
            or failed_at > checked_at
        ):
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.client_invalid",
                retryable=False, now=now,
            )
        if claim.attempt + 1 > self.max_attempts:
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.attempts_exhausted",
                retryable=False, now=now,
            )
        request = _CLIENT._request(
            claim.request_id, generation_attempts, failed_at, error_code,
        )
        request_sha256 = _sha256(_CLIENT._canonical(request))
        with _transaction(self.connection):
            self._leased_row(claim)
            self.connection.execute("""
                UPDATE benchmark_watcher_recovery_runner
                SET state = 'pending', phase = 'rearm', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, failed_generation_attempts = ?,
                    failed_at = ?, failed_error_code = ?, request_sha256 = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE singleton = 1
            """, (
                now, generation_attempts, failed_at, error_code,
                request_sha256, now,
            ))
        return RecoveryRunnerOutcome(
            True, "pending", "rearm", claim.attempt, now, None,
        )

    def _complete_rearm(
        self, claim: _Claim, snapshot: Any, now: float,
    ) -> RecoveryRunnerOutcome:
        request_sha256 = getattr(snapshot, "request_sha256", None)
        payload = getattr(snapshot, "as_payload", None)
        if not isinstance(request_sha256, str) or not callable(payload):
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.client_invalid",
                retryable=False, now=now,
            )
        try:
            receipt = payload()
            if not isinstance(receipt, dict):
                raise TypeError
            receipt_sha256 = _sha256(_canonical(receipt))
        except Exception:
            return self._fail_attempt(
                claim,
                code="benchmark_watcher.recovery_runner.client_invalid",
                retryable=False, now=now,
            )
        with _transaction(self.connection):
            row = self._leased_row(claim)
            if request_sha256 != row["request_sha256"]:
                raise BenchmarkWatcherRecoveryRunnerBlocked(
                    "benchmark_watcher.recovery_runner.result_invalid"
                )
            self.connection.execute("""
                UPDATE benchmark_watcher_recovery_runner
                SET state = 'succeeded', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    receipt_sha256 = ?, completed_at = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE singleton = 1
            """, (receipt_sha256, now, now))
        return RecoveryRunnerOutcome(
            True, "succeeded", "rearm", claim.attempt, now, None,
        )

    def _fail_attempt(
        self, claim: _Claim, *, code: str, retryable: bool, now: float,
    ) -> RecoveryRunnerOutcome:
        with _transaction(self.connection):
            self._leased_row(claim)
            remaining_phases = {"openapi": 2, "status": 1, "rearm": 0}[
                claim.phase
            ]
            retry = (
                retryable
                and claim.attempt + remaining_phases < self.max_attempts
            )
            state = "retry_wait" if retry else "failed"
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** max(0, claim.attempt - 1)),
            )
            next_attempt_at = now + delay if retry else now
            self.connection.execute("""
                UPDATE benchmark_watcher_recovery_runner
                SET state = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    completed_at = ?, last_error_code = ?, updated_at = ?
                WHERE singleton = 1
            """, (
                state, next_attempt_at, None if retry else now, code, now,
            ))
        return RecoveryRunnerOutcome(
            True, state, claim.phase, claim.attempt, next_attempt_at, code,
        )

    def _row(self, *, required: bool = True) -> sqlite3.Row | None:
        rows = self.connection.execute(
            "SELECT * FROM benchmark_watcher_recovery_runner"
        ).fetchall()
        if len(rows) > 1 or (required and len(rows) != 1):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.state_invalid"
            )
        return rows[0] if rows else None

    def _leased_row(self, claim: _Claim) -> sqlite3.Row:
        row = self._row()
        self._validated_row(row)
        if (
            row["state"] != "leased" or row["phase"] != claim.phase
            or row["attempts"] != claim.attempt
            or row["lease_owner"] != claim.worker_id
            or row["lease_token"] != claim.lease_token
        ):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.lease_lost"
            )
        return row

    def _validated_row(self, row: sqlite3.Row) -> None:
        values = dict(row)
        hashes = (
            values["client_binding_sha256"], values["operation_sha256"],
        )
        optional_hashes = (
            values["contract_sha256"], values["openapi_sha256"],
            values["request_sha256"], values["receipt_sha256"],
        )
        valid_hashes = all(
            isinstance(value, str) and _CONTROL.SHA256.fullmatch(value) is not None
            for value in hashes
        ) and all(
            value is None or (
                isinstance(value, str)
                and _CONTROL.SHA256.fullmatch(value) is not None
            )
            for value in optional_hashes
        )
        expected_request_id = "watcher-recovery-" + values["operation_sha256"]
        valid_policy = (
            values["client_binding_sha256"] == self.client_binding_sha256
            and values["max_attempts"] == self.max_attempts
            and values["lease_seconds"] == self.lease_seconds
            and values["base_delay_seconds"] == self.base_delay_seconds
            and values["max_delay_seconds"] == self.max_delay_seconds
        )
        valid_generation = (
            values["failed_generation_attempts"] is None
            and values["failed_at"] is None
            and values["failed_error_code"] is None
        ) or (
            isinstance(values["failed_generation_attempts"], int)
            and not isinstance(values["failed_generation_attempts"], bool)
            and 1 <= values["failed_generation_attempts"]
            <= _CONTROL._WATCHER.MAX_ATTEMPTS
            and isinstance(values["failed_at"], (int, float))
            and math.isfinite(values["failed_at"])
            and values["failed_at"] >= 0
            and isinstance(values["failed_error_code"], str)
            and _CONTROL.ERROR_CODE.fullmatch(values["failed_error_code"])
            is not None
        )
        has_discovery = (
            values["contract_sha256"] is not None
            and values["openapi_sha256"] is not None
        )
        valid_discovery_pair = (
            values["contract_sha256"] is None
        ) == (values["openapi_sha256"] is None)
        has_generation = values["failed_generation_attempts"] is not None
        valid_phase = (
            (values["phase"] == "openapi" and not has_discovery
             and not has_generation and values["request_sha256"] is None)
            or (values["phase"] == "status" and has_discovery
                and not has_generation and values["request_sha256"] is None)
            or (values["phase"] == "rearm" and has_discovery and has_generation
                and values["request_sha256"] is not None)
        )
        final = values["state"] in {"succeeded", "not_required", "failed"}
        valid_final = (
            final == (values["completed_at"] is not None)
            and (
                values["completed_at"] is None
                or (
                    isinstance(values["completed_at"], (int, float))
                    and math.isfinite(values["completed_at"])
                    and values["completed_at"] >= 0
                )
            )
            and (values["state"] != "succeeded" or (
                values["phase"] == "rearm"
                and values["receipt_sha256"] is not None
                and values["last_error_code"] is None
            ))
            and (values["state"] != "not_required" or (
                values["phase"] == "status"
                and values["receipt_sha256"] is None
                and values["last_error_code"] is None
            ))
            and (values["state"] != "failed" or (
                values["last_error_code"] is not None
                and values["receipt_sha256"] is None
            ))
        )
        valid_lease = (
            values["state"] == "leased"
            and isinstance(values["lease_owner"], str)
            and isinstance(values["lease_token"], str)
            and isinstance(values["lease_expires_at"], (int, float))
            and math.isfinite(values["lease_expires_at"])
        ) or (
            values["state"] != "leased"
            and values["lease_owner"] is None
            and values["lease_token"] is None
            and values["lease_expires_at"] is None
        )
        valid_error = (
            values["last_error_code"] is None
            or (
                isinstance(values["last_error_code"], str)
                and _CONTROL.ERROR_CODE.fullmatch(values["last_error_code"])
                is not None
            )
        )
        valid_state_error = (
            (values["state"] in {"retry_wait", "failed"})
            == (values["last_error_code"] is not None)
            or values["state"] == "leased"
        )
        if not (
            values["singleton"] == 1 and valid_hashes and valid_policy
            and values["request_id"] == expected_request_id
            and _CONTROL.IDENTIFIER.fullmatch(values["request_id"]) is not None
            and values["state"] in STATES and values["phase"] in PHASES
            and isinstance(values["attempts"], int)
            and 0 <= values["attempts"] <= values["max_attempts"]
            and all(
                isinstance(values[name], (int, float))
                and math.isfinite(values[name]) and values[name] >= 0
                for name in ("next_attempt_at", "updated_at")
            )
            and valid_generation and valid_discovery_pair and valid_phase
            and valid_final and valid_lease and valid_error and valid_state_error
            and (values["receipt_sha256"] is None or values["state"] == "succeeded")
        ):
            raise BenchmarkWatcherRecoveryRunnerBlocked(
                "benchmark_watcher.recovery_runner.state_invalid"
            )

    def _status(self, row: sqlite3.Row, now: float) -> RecoveryRunnerStatus:
        return RecoveryRunnerStatus(
            state=row["state"], phase=row["phase"], attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_attempt_at=float(row["next_attempt_at"]),
            lease_expires_at=(
                None if row["lease_expires_at"] is None
                else float(row["lease_expires_at"])
            ),
            lease_expired=(
                row["state"] == "leased" and row["lease_expires_at"] <= now
            ),
            contract_sha256=row["contract_sha256"],
            openapi_sha256=row["openapi_sha256"],
            generation_observed=row["failed_generation_attempts"] is not None,
            request_sha256=row["request_sha256"],
            receipt_sha256=row["receipt_sha256"],
            completed_at=row["completed_at"],
            last_error_code=row["last_error_code"],
        )
