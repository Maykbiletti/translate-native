#!/usr/bin/env python3
"""Durable website-side delivery outbox for the source-CMS HTTPS client.

The website host persists one immutable change, cancellation, or tombstone
before network access. One token-bound lease performs one client call. A crash
after remote acceptance safely replays the same identity after lease expiry;
the source runtime remains the idempotency authority.
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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
MAX_DELAY_SECONDS = 86_400.0
STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
ACTIVE_STATUSES = {"pending", "leased", "retry_wait"}
OPERATIONS = ("change", "cancellation", "tombstone")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_META_COLUMNS = ("singleton", "schema_version")
_COLUMNS = (
    "operation", "request_id", "event_id", "site_id", "payload_json",
    "payload_sha256", "capabilities_sha256", "status", "attempts",
    "delivery_max_attempts", "source_max_attempts", "next_attempt_at",
    "lease_owner", "lease_token", "lease_expires_at", "last_error_code",
    "response_sha256", "created_at", "updated_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source delivery dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CLIENT = _load_module(
    "blun_website_localization_cms_source_delivery_client",
    _ROOT / "integrations" / "website_localization_cms_source_client.py",
)


class CMSSourceDeliveryBlocked(RuntimeError):
    """Stable, content-free failure of the durable delivery outbox."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("source delivery failure is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceDeliveryClaim:
    operation: str
    request_id: str
    event_id: str
    site_id: str
    payload_sha256: str
    capabilities_sha256: str
    attempt: int
    delivery_max_attempts: int
    source_max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float
    payload_json: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class SourceDeliveryStatus:
    operation: str
    request_id: str
    event_id: str
    site_id: str
    payload_sha256: str
    capabilities_sha256: str
    status: str
    attempts: int
    delivery_max_attempts: int
    source_max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    response_sha256: str | None


@dataclass(frozen=True)
class SourceDeliveryOutcome:
    operation: str
    request_id: str
    event_id: str
    status: str
    attempt: int
    delivery_max_attempts: int
    next_attempt_at: float
    error_code: str | None


@dataclass(frozen=True)
class SourceDeliveryHealth:
    schema: str
    status: str
    counts: dict[str, int]
    operations: dict[str, int]
    due: int
    expired_leases: int
    failed: int
    contract_mismatches: int
    error_code: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "counts": dict(self.counts),
            "operations": dict(self.operations),
            "due": self.due,
            "expired_leases": self.expired_leases,
            "failed": self.failed,
            "contract_mismatches": self.contract_mismatches,
            "error_code": self.error_code,
        }


def _token(value: Any, code: str, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or TOKEN.fullmatch(value) is None
    ):
        raise CMSSourceDeliveryBlocked(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CMSSourceDeliveryBlocked(code)
    return value


def _timestamp(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CMSSourceDeliveryBlocked(code)
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise CMSSourceDeliveryBlocked(code)
    return result


def _duration(value: Any, code: str, *, maximum: float) -> float:
    result = _timestamp(value, code)
    if result <= 0 or result > maximum:
        raise CMSSourceDeliveryBlocked(code)
    return result


def _attempts(value: Any, code: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ATTEMPTS
    ):
        raise CMSSourceDeliveryBlocked(code)
    return value


def _canonical(value: Any, code: str) -> str:
    try:
        result = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise CMSSourceDeliveryBlocked(code) from None
    if not result or len(result.encode("utf-8")) > _CLIENT._HTTP.MAX_BODY_BYTES:
        raise CMSSourceDeliveryBlocked(code)
    return result


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload(value: Any) -> tuple[str, str, str, str, str, str]:
    if not isinstance(value, Mapping):
        raise CMSSourceDeliveryBlocked("source_delivery.request_invalid")
    try:
        schema = value.get("schema")
        if schema == _CLIENT._CMS.CHANGE_SCHEMA:
            copied, raw = _CLIENT._copy_payload(value, "change")
            operation = "change"
            request_id = copied["event_id"]
        else:
            copied, raw = _CLIENT._copy_payload(value, "removal")
            if copied["schema"] == _CLIENT._CMS.CANCELLATION_SCHEMA:
                operation, identity = "cancellation", "cancellation_id"
            else:
                operation, identity = "tombstone", "tombstone_id"
            request_id = copied[identity]
        event_id = copied["event_id"]
        site_id = copied["site_id"]
    except Exception:
        raise CMSSourceDeliveryBlocked(
            "source_delivery.request_invalid",
        ) from None
    payload_json = raw.decode("utf-8")
    return (
        operation, request_id, event_id, site_id, payload_json,
        hashlib.sha256(raw).hexdigest(),
    )


def _error_code(error: Exception) -> tuple[str, bool]:
    code = getattr(error, "code", None)
    retryable = getattr(error, "retryable", None)
    declared = getattr(error, "cms_source_client_failure", False) is True
    if (
        not declared
        or not isinstance(code, str)
        or ERROR_CODE.fullmatch(code) is None
        or not isinstance(retryable, bool)
    ):
        return "source_delivery.client_failure", False
    return code, retryable


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise CMSSourceDeliveryBlocked("source_delivery.transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableCMSSourceDeliveryOutbox:
    """Persist and deliver source events at least once through one pinned client."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        client: Any,
        *,
        clock: Callable[[], float | int] = time.time,
        base_delay_seconds: float | int = 5,
        max_delay_seconds: float | int = 300,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if not callable(clock):
            raise TypeError("clock must be callable")
        digest = getattr(client, "expected_capabilities_sha256", None)
        if (
            not callable(getattr(client, "submit_change", None))
            or not callable(getattr(client, "submit_removal", None))
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
        ):
            raise TypeError("client must provide the pinned source operations")
        timeout = getattr(client, "timeout", None)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 300
        ):
            raise TypeError("client timeout is invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.client = client
        self.capabilities_sha256 = digest
        self.timeout = float(timeout)
        self.clock = clock
        self.base_delay_seconds = _duration(
            base_delay_seconds, "source_delivery.delay_invalid",
            maximum=MAX_DELAY_SECONDS,
        )
        self.max_delay_seconds = _duration(
            max_delay_seconds, "source_delivery.delay_invalid",
            maximum=MAX_DELAY_SECONDS,
        )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise CMSSourceDeliveryBlocked("source_delivery.delay_invalid")
        self._initialize()

    def _now(self, value: float | int | None = None) -> float:
        try:
            supplied = self.clock() if value is None else value
        except Exception:
            raise CMSSourceDeliveryBlocked(
                "source_delivery.time_invalid",
            ) from None
        return _timestamp(supplied, "source_delivery.time_invalid")

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_delivery_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_source_delivery_meta VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_delivery_outbox (
                    operation TEXT NOT NULL CHECK (
                        operation IN ('change', 'cancellation', 'tombstone')
                    ),
                    request_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    capabilities_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending', 'leased', 'retry_wait',
                            'succeeded', 'failed'
                        )
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    delivery_max_attempts INTEGER NOT NULL CHECK (
                        delivery_max_attempts BETWEEN 1 AND 20
                    ),
                    source_max_attempts INTEGER NOT NULL CHECK (
                        source_max_attempts BETWEEN 1 AND 20
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    response_sha256 TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (operation, request_id),
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
                CREATE INDEX IF NOT EXISTS cms_source_delivery_due
                ON cms_source_delivery_outbox (
                    status, next_attempt_at, created_at, operation, request_id
                )
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_delivery_meta)"
            ).fetchall()
        )
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM cms_source_delivery_meta"
        ).fetchall()
        columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_delivery_outbox)"
            ).fetchall()
        )
        if (
            meta_columns != _META_COLUMNS
            or len(meta) != 1
            or tuple(meta[0]) != (1, SCHEMA_VERSION)
            or columns != _COLUMNS
        ):
            raise CMSSourceDeliveryBlocked("source_delivery.schema_altered")

    def enqueue_change(
        self,
        change: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
        now: float | int | None = None,
    ) -> SourceDeliveryStatus:
        return self._enqueue(
            change,
            source_max_attempts=source_max_attempts,
            delivery_max_attempts=delivery_max_attempts,
            now=now,
            expected_operation="change",
        )

    def enqueue_removal(
        self,
        removal: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
        now: float | int | None = None,
    ) -> SourceDeliveryStatus:
        return self._enqueue(
            removal,
            source_max_attempts=source_max_attempts,
            delivery_max_attempts=delivery_max_attempts,
            now=now,
            expected_operation=None,
        )

    def _enqueue(
        self,
        value: Mapping[str, Any],
        *,
        source_max_attempts: int,
        delivery_max_attempts: int,
        now: float | int | None,
        expected_operation: str | None,
    ) -> SourceDeliveryStatus:
        self._validate_schema()
        source_max_attempts = _attempts(
            source_max_attempts, "source_delivery.source_attempts_invalid",
        )
        delivery_max_attempts = _attempts(
            delivery_max_attempts, "source_delivery.attempts_invalid",
        )
        current = self._now(now)
        payload = _payload(value)
        operation, request_id, event_id, site_id, payload_json, payload_hash = payload
        if expected_operation is not None and operation != expected_operation:
            raise CMSSourceDeliveryBlocked("source_delivery.request_invalid")
        with _transaction(self.connection):
            row = self.connection.execute("""
                SELECT payload_sha256, capabilities_sha256,
                       delivery_max_attempts, source_max_attempts
                FROM cms_source_delivery_outbox
                WHERE operation = ? AND request_id = ?
            """, (operation, request_id)).fetchone()
            binding = (
                payload_hash,
                self.capabilities_sha256,
                delivery_max_attempts,
                source_max_attempts,
            )
            if row is not None and tuple(row) != binding:
                raise CMSSourceDeliveryBlocked(
                    "source_delivery.idempotency_collision",
                )
            if row is None:
                self.connection.execute("""
                    INSERT INTO cms_source_delivery_outbox (
                        operation, request_id, event_id, site_id, payload_json,
                        payload_sha256, capabilities_sha256, status, attempts,
                        delivery_max_attempts, source_max_attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
                """, (
                    operation, request_id, event_id, site_id, payload_json,
                    payload_hash, self.capabilities_sha256,
                    delivery_max_attempts, source_max_attempts,
                    current, current, current,
                ))
        return self.status(operation, request_id, now=current)

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            operation = row["operation"]
            request_id = _token(
                row["request_id"], "source_delivery.integrity",
            )
            event_id = _token(row["event_id"], "source_delivery.integrity")
            site_id = _token(row["site_id"], "source_delivery.integrity")
            payload_hash = _sha256(
                row["payload_sha256"], "source_delivery.integrity",
            )
            _sha256(row["capabilities_sha256"], "source_delivery.integrity")
            status = row["status"]
            attempts = row["attempts"]
            delivery_max = _attempts(
                row["delivery_max_attempts"], "source_delivery.integrity",
            )
            _attempts(row["source_max_attempts"], "source_delivery.integrity")
            next_attempt = _timestamp(
                row["next_attempt_at"], "source_delivery.integrity",
            )
            created = _timestamp(row["created_at"], "source_delivery.integrity")
            updated = _timestamp(row["updated_at"], "source_delivery.integrity")
            parsed = json.loads(row["payload_json"])
            normalized = _payload(parsed)
        except CMSSourceDeliveryBlocked:
            raise
        except Exception:
            raise CMSSourceDeliveryBlocked(
                "source_delivery.integrity",
            ) from None
        if (
            operation not in OPERATIONS
            or status not in STATUSES
            or isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 0 <= attempts <= delivery_max
            or not created <= updated
            or next_attempt < created
            or normalized != (
                operation, request_id, event_id, site_id,
                row["payload_json"], payload_hash,
            )
        ):
            raise CMSSourceDeliveryBlocked("source_delivery.integrity")
        leased = status == "leased"
        lease_values = (
            row["lease_owner"], row["lease_token"], row["lease_expires_at"],
        )
        if leased:
            if (
                not all(value is not None for value in lease_values)
                or not _token(
                    row["lease_owner"], "source_delivery.integrity", limit=128,
                )
                or not _token(
                    row["lease_token"], "source_delivery.integrity", limit=256,
                )
                or _timestamp(
                    row["lease_expires_at"], "source_delivery.integrity",
                ) <= updated
            ):
                raise CMSSourceDeliveryBlocked("source_delivery.integrity")
        elif any(value is not None for value in lease_values):
            raise CMSSourceDeliveryBlocked("source_delivery.integrity")
        error = row["last_error_code"]
        response_hash = row["response_sha256"]
        if error is not None and (
            not isinstance(error, str) or ERROR_CODE.fullmatch(error) is None
        ):
            raise CMSSourceDeliveryBlocked("source_delivery.integrity")
        if response_hash is not None:
            _sha256(response_hash, "source_delivery.integrity")
        if (
            (status in {"retry_wait", "failed"}) != (error is not None)
            or (status == "succeeded") != (response_hash is not None)
        ):
            raise CMSSourceDeliveryBlocked("source_delivery.integrity")

    def _validate_rows(self) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM cms_source_delivery_outbox"
        ).fetchall()
        for row in rows:
            self._validated_row(row)
        return rows

    def status(
        self,
        operation: str,
        request_id: str,
        *,
        now: float | int | None = None,
    ) -> SourceDeliveryStatus:
        self._validate_schema()
        if operation not in OPERATIONS:
            raise CMSSourceDeliveryBlocked("source_delivery.status_invalid")
        request_id = _token(request_id, "source_delivery.status_invalid")
        current = self._now(now)
        row = self.connection.execute("""
            SELECT * FROM cms_source_delivery_outbox
            WHERE operation = ? AND request_id = ?
        """, (operation, request_id)).fetchone()
        if row is None:
            raise CMSSourceDeliveryBlocked("source_delivery.status_not_found")
        self._validated_row(row)
        return SourceDeliveryStatus(
            operation=row["operation"],
            request_id=row["request_id"],
            event_id=row["event_id"],
            site_id=row["site_id"],
            payload_sha256=row["payload_sha256"],
            capabilities_sha256=row["capabilities_sha256"],
            status=row["status"],
            attempts=int(row["attempts"]),
            delivery_max_attempts=int(row["delivery_max_attempts"]),
            source_max_attempts=int(row["source_max_attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            lease_expires_at=(
                None
                if row["lease_expires_at"] is None
                else float(row["lease_expires_at"])
            ),
            lease_expired=(
                row["status"] == "leased"
                and float(row["lease_expires_at"]) <= current
            ),
            last_error_code=row["last_error_code"],
            response_sha256=row["response_sha256"],
        )

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: float | int = 600,
        now: float | int | None = None,
    ) -> SourceDeliveryClaim | None:
        self._validate_schema()
        worker_id = _token(
            worker_id, "source_delivery.worker_invalid", limit=128,
        )
        lease_seconds = _duration(
            lease_seconds, "source_delivery.lease_invalid",
            maximum=MAX_LEASE_SECONDS,
        )
        if lease_seconds <= self.timeout:
            raise CMSSourceDeliveryBlocked("source_delivery.lease_too_short")
        current = self._now(now)
        claim = None
        with _transaction(self.connection):
            self._validate_rows()
            self.connection.execute("""
                UPDATE cms_source_delivery_outbox
                SET status = CASE
                        WHEN attempts >= delivery_max_attempts
                        THEN 'failed' ELSE 'retry_wait' END,
                    next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = 'source_delivery.lease_expired',
                    updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
            """, (current, current, current))
            mismatch = self.connection.execute("""
                SELECT 1 FROM cms_source_delivery_outbox
                WHERE status IN ('pending', 'leased', 'retry_wait')
                  AND capabilities_sha256 <> ? LIMIT 1
            """, (self.capabilities_sha256,)).fetchone()
            if mismatch is not None:
                raise CMSSourceDeliveryBlocked(
                    "source_delivery.contract_changed",
                )
            row = self.connection.execute("""
                SELECT * FROM cms_source_delivery_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND next_attempt_at <= ?
                  AND attempts < delivery_max_attempts
                ORDER BY CASE operation WHEN 'change' THEN 1 ELSE 0 END,
                         created_at, operation, request_id
                LIMIT 1
            """, (current,)).fetchone()
            if row is not None:
                self._validated_row(row)
                lease_token = secrets.token_urlsafe(32)
                lease_expires_at = current + lease_seconds
                updated = self.connection.execute("""
                    UPDATE cms_source_delivery_outbox
                    SET status = 'leased', attempts = attempts + 1,
                        lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                        last_error_code = NULL, updated_at = ?
                    WHERE operation = ? AND request_id = ?
                      AND status IN ('pending', 'retry_wait')
                """, (
                    worker_id, lease_token, lease_expires_at, current,
                    row["operation"], row["request_id"],
                ))
                if updated.rowcount != 1:
                    raise CMSSourceDeliveryBlocked(
                        "source_delivery.claim_lost",
                    )
                claim = SourceDeliveryClaim(
                    operation=row["operation"],
                    request_id=row["request_id"],
                    event_id=row["event_id"],
                    site_id=row["site_id"],
                    payload_sha256=row["payload_sha256"],
                    capabilities_sha256=row["capabilities_sha256"],
                    attempt=int(row["attempts"]) + 1,
                    delivery_max_attempts=int(
                        row["delivery_max_attempts"]
                    ),
                    source_max_attempts=int(row["source_max_attempts"]),
                    lease_owner=worker_id,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    payload_json=row["payload_json"],
                )
        return claim

    @staticmethod
    def _claim(value: Any) -> SourceDeliveryClaim:
        if not isinstance(value, SourceDeliveryClaim):
            raise CMSSourceDeliveryBlocked("source_delivery.claim_invalid")
        for name in (
            "request_id", "event_id", "site_id", "lease_owner", "lease_token",
        ):
            _token(getattr(value, name), "source_delivery.claim_invalid")
        if (
            value.operation not in OPERATIONS
            or SHA256.fullmatch(value.payload_sha256) is None
            or SHA256.fullmatch(value.capabilities_sha256) is None
            or not 1 <= value.attempt <= value.delivery_max_attempts <= 20
            or not 1 <= value.source_max_attempts <= 20
        ):
            raise CMSSourceDeliveryBlocked("source_delivery.claim_invalid")
        _timestamp(
            value.lease_expires_at, "source_delivery.claim_invalid",
        )
        return value

    def _current_row(
        self, claim: SourceDeliveryClaim, current: float,
    ) -> sqlite3.Row:
        row = self.connection.execute("""
            SELECT * FROM cms_source_delivery_outbox
            WHERE operation = ? AND request_id = ?
        """, (claim.operation, claim.request_id)).fetchone()
        if row is None:
            raise CMSSourceDeliveryBlocked("source_delivery.claim_lost")
        self._validated_row(row)
        if (
            row["status"] != "leased"
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
            or float(row["lease_expires_at"]) != claim.lease_expires_at
            or float(row["lease_expires_at"]) <= current
            or int(row["attempts"]) != claim.attempt
            or row["payload_sha256"] != claim.payload_sha256
            or row["capabilities_sha256"] != claim.capabilities_sha256
        ):
            raise CMSSourceDeliveryBlocked("source_delivery.claim_lost")
        return row

    def _response(self, claim: SourceDeliveryClaim, value: Any) -> str:
        if not isinstance(value, Mapping):
            raise CMSSourceDeliveryBlocked("source_delivery.response_invalid")
        response_json = _canonical(
            dict(value), "source_delivery.response_invalid",
        )
        try:
            response = json.loads(response_json)
        except (json.JSONDecodeError, RecursionError):
            raise CMSSourceDeliveryBlocked(
                "source_delivery.response_invalid",
            ) from None
        expected_schema = (
            _CLIENT._HTTP.CHANGE_RESPONSE_SCHEMA
            if claim.operation == "change"
            else _CLIENT._HTTP.REMOVAL_RESPONSE_SCHEMA
        )
        if (
            set(response) != {
                "schema", "operation", "request_id", "event_id",
                "payload_sha256", "status", "attempts", "max_attempts",
                "capabilities_sha256",
            }
            or response.get("schema") != expected_schema
            or response.get("operation") != claim.operation
            or response.get("request_id") != claim.request_id
            or response.get("event_id") != claim.event_id
            or response.get("payload_sha256") != claim.payload_sha256
            or response.get("status") not in _CLIENT._HTTP.STATUSES
            or response.get("capabilities_sha256")
            != claim.capabilities_sha256
            or isinstance(response.get("attempts"), bool)
            or not isinstance(response.get("attempts"), int)
            or not 0 <= response["attempts"] <= claim.source_max_attempts
            or response.get("max_attempts") != claim.source_max_attempts
        ):
            raise CMSSourceDeliveryBlocked("source_delivery.response_invalid")
        return response_json

    def complete(
        self,
        claim: SourceDeliveryClaim,
        response: Mapping[str, Any],
        *,
        now: float | int | None = None,
    ) -> SourceDeliveryStatus:
        self._validate_schema()
        claim = self._claim(claim)
        current = self._now(now)
        response_hash = _hash(self._response(claim, response))
        with _transaction(self.connection):
            self._current_row(claim, current)
            updated = self.connection.execute("""
                UPDATE cms_source_delivery_outbox
                SET status = 'succeeded', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL, response_sha256 = ?, updated_at = ?
                WHERE operation = ? AND request_id = ?
                  AND status = 'leased' AND lease_token = ?
            """, (
                response_hash, current, claim.operation, claim.request_id,
                claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise CMSSourceDeliveryBlocked("source_delivery.claim_lost")
        return self.status(claim.operation, claim.request_id, now=current)

    def _record_failure(
        self,
        claim: SourceDeliveryClaim,
        code: str,
        *,
        retryable: bool,
        now: float | int | None = None,
    ) -> SourceDeliveryStatus:
        self._validate_schema()
        claim = self._claim(claim)
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            code = "source_delivery.client_failure"
            retryable = False
        current = self._now(now)
        with _transaction(self.connection):
            self._current_row(claim, current)
            retry = retryable and claim.attempt < claim.delivery_max_attempts
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** (claim.attempt - 1)),
            )
            status = "retry_wait" if retry else "failed"
            next_attempt = current + delay if retry else current
            updated = self.connection.execute("""
                UPDATE cms_source_delivery_outbox
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, response_sha256 = NULL, updated_at = ?
                WHERE operation = ? AND request_id = ?
                  AND status = 'leased' AND lease_token = ?
            """, (
                status, next_attempt, code, current, claim.operation,
                claim.request_id, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise CMSSourceDeliveryBlocked("source_delivery.claim_lost")
        return self.status(claim.operation, claim.request_id, now=current)

    def run_once(
        self,
        worker_id: str,
        *,
        lease_seconds: float | int = 600,
        now: float | int | None = None,
    ) -> SourceDeliveryOutcome | None:
        claim = self.claim(
            worker_id, lease_seconds=lease_seconds, now=now,
        )
        if claim is None:
            return None
        try:
            payload = json.loads(claim.payload_json)
            if claim.operation == "change":
                response = self.client.submit_change(
                    payload, max_attempts=claim.source_max_attempts,
                )
            else:
                response = self.client.submit_removal(
                    payload, max_attempts=claim.source_max_attempts,
                )
            status = self.complete(claim, response)
        except CMSSourceDeliveryBlocked as error:
            if error.code != "source_delivery.response_invalid":
                raise
            status = self._record_failure(
                claim, error.code, retryable=False,
            )
        except Exception as error:
            code, retryable = _error_code(error)
            status = self._record_failure(
                claim, code, retryable=retryable,
            )
        return SourceDeliveryOutcome(
            operation=status.operation,
            request_id=status.request_id,
            event_id=status.event_id,
            status=status.status,
            attempt=status.attempts,
            delivery_max_attempts=status.delivery_max_attempts,
            next_attempt_at=status.next_attempt_at,
            error_code=status.last_error_code,
        )

    def health(
        self, *, now: float | int | None = None,
    ) -> SourceDeliveryHealth:
        self._validate_schema()
        current = self._now(now)
        try:
            rows = self._validate_rows()
        except CMSSourceDeliveryBlocked:
            return SourceDeliveryHealth(
                schema="blun.cms-source-delivery-health.v1",
                status="blocked",
                counts={}, operations={}, due=0, expired_leases=0,
                failed=0, contract_mismatches=0,
                error_code="source_delivery.integrity",
            )
        counts = {name: 0 for name in STATUSES}
        operations = {name: 0 for name in OPERATIONS}
        due = expired = failed = mismatches = 0
        for row in rows:
            counts[row["status"]] += 1
            operations[row["operation"]] += 1
            if (
                row["status"] in {"pending", "retry_wait"}
                and float(row["next_attempt_at"]) <= current
            ):
                due += 1
            if (
                row["status"] == "leased"
                and float(row["lease_expires_at"]) <= current
            ):
                expired += 1
            if row["status"] == "failed":
                failed += 1
            if (
                row["status"] in ACTIVE_STATUSES
                and row["capabilities_sha256"] != self.capabilities_sha256
            ):
                mismatches += 1
        blocked = failed > 0 or expired > 0 or mismatches > 0
        if failed:
            error = "source_delivery.failed"
        elif expired:
            error = "source_delivery.lease_expired"
        elif mismatches:
            error = "source_delivery.contract_changed"
        else:
            error = None
        return SourceDeliveryHealth(
            schema="blun.cms-source-delivery-health.v1",
            status="blocked" if blocked else "ok",
            counts=counts,
            operations=operations,
            due=due,
            expired_leases=expired,
            failed=failed,
            contract_mismatches=mismatches,
            error_code=error,
        )
