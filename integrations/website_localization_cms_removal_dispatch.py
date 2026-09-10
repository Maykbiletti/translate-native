#!/usr/bin/env python3
"""Durable source-side dispatch for CMS cancellation and tombstone requests.

One shared outbox covers both removal phases without confusing their identities:
cancel an unpublished localization, or request deletion of an acknowledged
publication. Each leased attempt sends one immutable request through the secure
CMS client and relies on the server's exact idempotency for crash recovery.
"""

from __future__ import annotations

import importlib.util
import json
import math
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1
OPERATIONS = ("cancellation", "tombstone")
STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
_COLUMNS = (
    "operation", "request_id", "event_id", "payload_json",
    "payload_sha256", "status", "attempts", "max_attempts",
    "next_attempt_at", "lease_owner", "lease_token", "lease_expires_at",
    "last_error_code", "remote_delivery_id", "remote_status",
    "remote_new", "response_sha256", "created_at", "updated_at",
)
_META_COLUMNS = ("singleton", "schema_version")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load CMS removal dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_DISPATCH = _load_module(
    "blun_website_localization_cms_removal_dispatch_core",
    _ROOT / "integrations" / "website_localization_cms_dispatch.py",
)
CMSRemovalBlocked = _DISPATCH.CMSDispatchBlocked


@dataclass(frozen=True)
class RemovalDispatchClaim:
    operation: str
    request_id: str
    event_id: str
    payload_sha256: str
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float
    payload_json: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class RemovalDispatchStatus:
    operation: str
    request_id: str
    event_id: str
    payload_sha256: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    remote_delivery_id: str | None
    remote_status: str | None
    remote_new: bool | None
    response_sha256: str | None


@dataclass(frozen=True)
class RemovalDispatchOutcome:
    operation: str
    request_id: str
    event_id: str
    status: str
    attempt: int
    max_attempts: int
    next_attempt_at: float
    error_code: str | None


@dataclass(frozen=True)
class RemovalDispatchHealth:
    status: str
    counts: dict[str, int]
    operations: dict[str, int]
    due: int
    expired_leases: int
    failed: int


def _removal(value: Any) -> tuple[str, str, str, str, str]:
    if not isinstance(value, Mapping):
        raise CMSRemovalBlocked("removal.request_invalid")
    payload_json = _DISPATCH._canonical_json(
        dict(value), "removal.request_invalid",
    )
    try:
        copied = json.loads(payload_json)
    except (json.JSONDecodeError, RecursionError):
        raise CMSRemovalBlocked("removal.request_invalid") from None
    schema = copied.get("schema") if isinstance(copied, dict) else None
    cms = _DISPATCH._CLIENT._CMS
    if schema == cms.CANCELLATION_SCHEMA:
        operation, identity = "cancellation", "cancellation_id"
        validator = cms.WebsiteLocalizationCMSBridge._validated_cancellation
    elif schema == cms.TOMBSTONE_SCHEMA:
        operation, identity = "tombstone", "tombstone_id"
        validator = cms.WebsiteLocalizationCMSBridge._validated_tombstone
    else:
        raise CMSRemovalBlocked("removal.request_invalid")
    try:
        validator(None, copied)
    except Exception:
        raise CMSRemovalBlocked("removal.request_invalid") from None
    request_id = _DISPATCH._token(
        copied.get(identity), "removal.request_invalid",
    )
    event_id = _DISPATCH._token(
        copied.get("event_id"), "removal.request_invalid",
    )
    return (
        operation, request_id, event_id, payload_json,
        _DISPATCH._hash(payload_json),
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise CMSRemovalBlocked("removal.transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class DurableCMSRemovalDispatcher:
    """Persist and deliver exact cancellation and tombstone requests."""

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
        self.base_delay_seconds = _DISPATCH._duration(
            base_delay_seconds, "removal.delay_invalid",
            maximum=_DISPATCH.MAX_DELAY_SECONDS,
        )
        self.max_delay_seconds = _DISPATCH._duration(
            max_delay_seconds, "removal.delay_invalid",
            maximum=_DISPATCH.MAX_DELAY_SECONDS,
        )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise CMSRemovalBlocked("removal.delay_invalid")
        self._initialize()

    def _initialize(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_removal_outbox_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_source_removal_outbox_meta
                VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_source_removal_outbox (
                    operation TEXT NOT NULL CHECK (
                        operation IN ('cancellation', 'tombstone')
                    ),
                    request_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
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
                    remote_delivery_id TEXT,
                    remote_status TEXT,
                    remote_new INTEGER,
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
                CREATE INDEX IF NOT EXISTS cms_source_removal_outbox_due
                ON cms_source_removal_outbox (
                    status, next_attempt_at, created_at, operation, request_id
                )
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_removal_outbox_meta)"
            ).fetchall()
        )
        meta = self.connection.execute(
            "SELECT singleton, schema_version "
            "FROM cms_source_removal_outbox_meta"
        ).fetchall()
        columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_removal_outbox)"
            ).fetchall()
        )
        if (
            meta_columns != _META_COLUMNS
            or len(meta) != 1
            or tuple(meta[0]) != (1, SCHEMA_VERSION)
            or columns != _COLUMNS
        ):
            raise CMSRemovalBlocked("removal.schema_altered")

    def enqueue(
        self,
        request: Mapping[str, Any],
        *,
        max_attempts: int = 5,
        now: float | int,
    ) -> RemovalDispatchStatus:
        self._validate_schema()
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= _DISPATCH.MAX_ATTEMPTS
        ):
            raise CMSRemovalBlocked("removal.max_attempts_invalid")
        now = _DISPATCH._timestamp(now, "removal.time_invalid")
        operation, request_id, event_id, payload_json, payload_sha256 = (
            _removal(request)
        )
        with _transaction(self.connection):
            row = self.connection.execute("""
                SELECT event_id, payload_sha256, max_attempts
                FROM cms_source_removal_outbox
                WHERE operation = ? AND request_id = ?
            """, (operation, request_id)).fetchone()
            if row is not None:
                if (
                    row["event_id"] != event_id
                    or row["payload_sha256"] != payload_sha256
                    or row["max_attempts"] != max_attempts
                ):
                    raise CMSRemovalBlocked("removal.idempotency_collision")
            else:
                self.connection.execute("""
                    INSERT INTO cms_source_removal_outbox (
                        operation, request_id, event_id, payload_json,
                        payload_sha256, status, attempts, max_attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (
                    operation, request_id, event_id, payload_json,
                    payload_sha256, max_attempts, now, now, now,
                ))
        return self.status(operation, request_id, now=now)

    def claim(
        self,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> RemovalDispatchClaim | None:
        self._validate_schema()
        worker_id = _DISPATCH._token(
            worker_id, "removal.worker_invalid", limit=128,
        )
        now = _DISPATCH._timestamp(now, "removal.time_invalid")
        lease_seconds = _DISPATCH._duration(
            lease_seconds, "removal.lease_invalid",
            maximum=_DISPATCH.MAX_LEASE_SECONDS,
        )
        claim = None
        integrity_error = None
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_source_removal_outbox
                SET status = 'failed', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts >= max_attempts
            """, (now, now))
            self.connection.execute("""
                UPDATE cms_source_removal_outbox
                SET status = 'retry_wait', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts < max_attempts
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_source_removal_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND next_attempt_at <= ? AND attempts < max_attempts
                ORDER BY created_at, operation, request_id LIMIT 1
            """, (now,)).fetchone()
            if row is not None:
                try:
                    self._validated_row(row)
                except CMSRemovalBlocked:
                    self.connection.execute("""
                        UPDATE cms_source_removal_outbox
                        SET status = 'failed',
                            last_error_code = 'removal.payload_integrity',
                            updated_at = ?
                        WHERE operation = ? AND request_id = ?
                    """, (now, row["operation"], row["request_id"]))
                    integrity_error = "removal.payload_integrity"
                else:
                    lease_token = secrets.token_urlsafe(32)
                    lease_expires_at = now + lease_seconds
                    updated = self.connection.execute("""
                        UPDATE cms_source_removal_outbox
                        SET status = 'leased', attempts = attempts + 1,
                            lease_owner = ?, lease_token = ?,
                            lease_expires_at = ?, last_error_code = NULL,
                            updated_at = ?
                        WHERE operation = ? AND request_id = ?
                          AND status IN ('pending', 'retry_wait')
                    """, (
                        worker_id, lease_token, lease_expires_at, now,
                        row["operation"], row["request_id"],
                    ))
                    if updated.rowcount != 1:
                        raise CMSRemovalBlocked("removal.claim_lost")
                    claim = RemovalDispatchClaim(
                        operation=row["operation"],
                        request_id=row["request_id"],
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
            raise CMSRemovalBlocked(integrity_error)
        return claim

    def run_once(
        self,
        client: Any,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> RemovalDispatchOutcome | None:
        methods = {
            "cancellation": getattr(client, "cancel", None),
            "tombstone": getattr(client, "request_tombstone", None),
        }
        if not all(callable(method) for method in methods.values()):
            raise CMSRemovalBlocked("removal.client_invalid")
        lease_seconds = _DISPATCH._duration(
            lease_seconds, "removal.lease_invalid",
            maximum=_DISPATCH.MAX_LEASE_SECONDS,
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
            raise CMSRemovalBlocked("removal.client_invalid")
        if timeout is not None and lease_seconds <= float(timeout):
            raise CMSRemovalBlocked("removal.lease_too_short")
        claim = self.claim(worker_id, now=now, lease_seconds=lease_seconds)
        if claim is None:
            return None
        try:
            response = methods[claim.operation](json.loads(claim.payload_json))
            status = self.complete(claim, response, now=now)
        except CMSRemovalBlocked as error:
            if error.code != "removal.response_invalid":
                raise
            status = self.fail(claim, error.code, now=now)
        except Exception as error:
            if getattr(error, "cms_client_failure", False) is True:
                code = getattr(error, "code", None)
                retryable = getattr(error, "retryable", None)
                try:
                    code, _ = _DISPATCH._error(code)
                except CMSRemovalBlocked:
                    code, retryable = "removal.client_failure", False
                status = (
                    self.retry(claim, code, now=now)
                    if retryable is True
                    else self.fail(claim, code, now=now)
                )
            else:
                status = self.fail(
                    claim, "removal.client_failure", now=now,
                )
        return RemovalDispatchOutcome(
            operation=status.operation,
            request_id=status.request_id,
            event_id=status.event_id,
            status=status.status,
            attempt=status.attempts,
            max_attempts=status.max_attempts,
            next_attempt_at=status.next_attempt_at,
            error_code=status.last_error_code,
        )

    def complete(
        self,
        claim: RemovalDispatchClaim,
        response: Any,
        *,
        now: float | int,
    ) -> RemovalDispatchStatus:
        self._validate_schema()
        claim = self._claim(claim)
        now = _DISPATCH._timestamp(now, "removal.time_invalid")
        if not isinstance(response, Mapping):
            raise CMSRemovalBlocked("removal.response_invalid")
        response_json = _DISPATCH._canonical_json(
            dict(response), "removal.response_invalid",
        )
        copied = json.loads(response_json)
        delivery_id = None
        if claim.operation == "cancellation":
            valid = (
                set(copied) == {
                    "schema", "cancellation_id", "event_id", "status",
                    "newly_cancelled",
                }
                and copied.get("cancellation_id") == claim.request_id
                and copied.get("event_id") == claim.event_id
                and copied.get("status") == "cancelled"
                and isinstance(copied.get("newly_cancelled"), bool)
            )
            remote_new = copied.get("newly_cancelled")
        else:
            valid = (
                set(copied) == {
                    "schema", "tombstone_id", "event_id", "delivery_id",
                    "status", "newly_requested",
                }
                and copied.get("tombstone_id") == claim.request_id
                and copied.get("event_id") == claim.event_id
                and isinstance(copied.get("delivery_id"), str)
                and _DISPATCH.TOKEN.fullmatch(copied["delivery_id"]) is not None
                and copied.get("status") in {
                    "pending", "leased", "retry_wait", "succeeded", "failed",
                }
                and isinstance(copied.get("newly_requested"), bool)
            )
            delivery_id = copied.get("delivery_id")
            remote_new = copied.get("newly_requested")
        if (
            copied.get("schema") != _DISPATCH._CLIENT._API.API_SCHEMA
            or not valid
        ):
            raise CMSRemovalBlocked("removal.response_invalid")
        with _transaction(self.connection):
            self._require_live_claim(claim, now)
            updated = self.connection.execute("""
                UPDATE cms_source_removal_outbox
                SET status = 'succeeded', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    remote_delivery_id = ?, remote_status = ?,
                    remote_new = ?, response_sha256 = ?, updated_at = ?
                WHERE operation = ? AND request_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                now, delivery_id, copied["status"], int(remote_new),
                _DISPATCH._hash(response_json), now, claim.operation,
                claim.request_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise CMSRemovalBlocked("removal.completion_lost")
        return self.status(claim.operation, claim.request_id, now=now)

    def retry(
        self,
        claim: RemovalDispatchClaim,
        error_code: str,
        *,
        now: float | int,
    ) -> RemovalDispatchStatus:
        self._validate_schema()
        claim = self._claim(claim)
        error_code, _ = _DISPATCH._error(error_code)
        now = _DISPATCH._timestamp(now, "removal.time_invalid")
        with _transaction(self.connection):
            row = self._require_live_claim(claim, now)
            terminal = int(row["attempts"]) >= int(row["max_attempts"])
            status = "failed" if terminal else "retry_wait"
            delay = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** max(0, claim.attempt - 1)),
            )
            self._finish_failure(
                claim, status, error_code,
                now if terminal else now + delay, now,
            )
        return self.status(claim.operation, claim.request_id, now=now)

    def fail(
        self,
        claim: RemovalDispatchClaim,
        error_code: str,
        *,
        now: float | int,
    ) -> RemovalDispatchStatus:
        self._validate_schema()
        claim = self._claim(claim)
        error_code, _ = _DISPATCH._error(error_code)
        now = _DISPATCH._timestamp(now, "removal.time_invalid")
        with _transaction(self.connection):
            self._require_live_claim(claim, now)
            self._finish_failure(claim, "failed", error_code, now, now)
        return self.status(claim.operation, claim.request_id, now=now)

    def _finish_failure(
        self,
        claim: RemovalDispatchClaim,
        status: str,
        error_code: str,
        next_attempt_at: float,
        now: float,
    ) -> None:
        updated = self.connection.execute("""
            UPDATE cms_source_removal_outbox
            SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL,
                last_error_code = ?, updated_at = ?
            WHERE operation = ? AND request_id = ? AND status = 'leased'
              AND lease_owner = ? AND lease_token = ?
        """, (
            status, next_attempt_at, error_code, now, claim.operation,
            claim.request_id, claim.lease_owner, claim.lease_token,
        ))
        if updated.rowcount != 1:
            raise CMSRemovalBlocked("removal.failure_lost")

    def status(
        self,
        operation: str,
        request_id: str,
        *,
        now: float | int,
    ) -> RemovalDispatchStatus:
        self._validate_schema()
        if operation not in OPERATIONS:
            raise CMSRemovalBlocked("removal.operation_invalid")
        request_id = _DISPATCH._token(
            request_id, "removal.request_id_invalid",
        )
        now = _DISPATCH._timestamp(now, "removal.time_invalid")
        row = self.connection.execute("""
            SELECT * FROM cms_source_removal_outbox
            WHERE operation = ? AND request_id = ?
        """, (operation, request_id)).fetchone()
        if row is None:
            raise CMSRemovalBlocked("removal.request_missing")
        self._validated_row(row)
        lease_expires_at = row["lease_expires_at"]
        remote_new = row["remote_new"]
        return RemovalDispatchStatus(
            operation=row["operation"],
            request_id=row["request_id"],
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
            remote_delivery_id=row["remote_delivery_id"],
            remote_status=row["remote_status"],
            remote_new=(None if remote_new is None else bool(remote_new)),
            response_sha256=row["response_sha256"],
        )

    def health(self, *, now: float | int) -> RemovalDispatchHealth:
        now = _DISPATCH._timestamp(now, "removal.time_invalid")
        self._validate_schema()
        quick = self.connection.execute("PRAGMA quick_check").fetchall()
        if len(quick) != 1 or tuple(quick[0]) != ("ok",):
            raise CMSRemovalBlocked("removal.database_integrity")
        rows = self.connection.execute(
            "SELECT * FROM cms_source_removal_outbox "
            "ORDER BY operation, request_id"
        ).fetchall()
        for row in rows:
            self._validated_row(row)
        counts = {status: 0 for status in STATUSES}
        operations = {operation: 0 for operation in OPERATIONS}
        for row in rows:
            counts[row["status"]] += 1
            operations[row["operation"]] += 1
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
        return RemovalDispatchHealth(
            status="blocked" if expired or failed else "ok",
            counts=counts,
            operations=operations,
            due=due,
            expired_leases=expired,
            failed=failed,
        )

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            operation, request_id, event_id, payload_json, payload_sha256 = (
                _removal(json.loads(row["payload_json"]))
            )
            attempts = row["attempts"]
            maximum = row["max_attempts"]
            next_attempt_at = _DISPATCH._timestamp(
                row["next_attempt_at"], "removal.state_invalid",
            )
            created_at = _DISPATCH._timestamp(
                row["created_at"], "removal.state_invalid",
            )
            updated_at = _DISPATCH._timestamp(
                row["updated_at"], "removal.state_invalid",
            )
        except (json.JSONDecodeError, TypeError, CMSRemovalBlocked):
            raise CMSRemovalBlocked("removal.state_invalid") from None
        status = row["status"]
        leases = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        leased = status == "leased"
        succeeded = status == "succeeded"
        response_sha256 = row["response_sha256"]
        remote_status = row["remote_status"]
        remote_new = row["remote_new"]
        delivery_id = row["remote_delivery_id"]
        error = row["last_error_code"]
        response_binding = None
        if operation == "cancellation" and type(remote_new) is int:
            response_binding = {
                "schema": _DISPATCH._CLIENT._API.API_SCHEMA,
                "cancellation_id": request_id,
                "event_id": event_id,
                "status": remote_status,
                "newly_cancelled": bool(remote_new),
            }
        elif operation == "tombstone" and type(remote_new) is int:
            response_binding = {
                "schema": _DISPATCH._CLIENT._API.API_SCHEMA,
                "tombstone_id": request_id,
                "event_id": event_id,
                "delivery_id": delivery_id,
                "status": remote_status,
                "newly_requested": bool(remote_new),
            }
        remote_valid = (
            isinstance(response_sha256, str)
            and _DISPATCH.SHA256.fullmatch(response_sha256) is not None
            and type(remote_new) is int
            and remote_new in {0, 1}
            and (
                (operation == "cancellation"
                 and delivery_id is None
                 and remote_status == "cancelled")
                or
                (operation == "tombstone"
                 and isinstance(delivery_id, str)
                 and _DISPATCH.TOKEN.fullmatch(delivery_id) is not None
                 and remote_status in {
                     "pending", "leased", "retry_wait", "succeeded", "failed",
                 })
            )
            and response_binding is not None
            and response_sha256 == _DISPATCH._hash(
                _DISPATCH._canonical_json(
                    response_binding, "removal.state_invalid",
                )
            )
        )
        if (
            operation != row["operation"]
            or request_id != row["request_id"]
            or event_id != row["event_id"]
            or payload_json != row["payload_json"]
            or payload_sha256 != row["payload_sha256"]
            or status not in STATUSES
            or isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 0 <= attempts <= maximum <= _DISPATCH.MAX_ATTEMPTS
            or maximum < 1
            or next_attempt_at < created_at
            or updated_at < created_at
            or leased != all(item is not None for item in leases)
            or (not leased and any(item is not None for item in leases))
            or (leased and (
                _DISPATCH._token(
                    leases[0], "removal.state_invalid", limit=128,
                ) != leases[0]
                or _DISPATCH._token(
                    leases[1], "removal.state_invalid",
                ) != leases[1]
                or _DISPATCH._timestamp(
                    leases[2], "removal.state_invalid",
                ) <= updated_at
            ))
            or (error is not None and (
                not isinstance(error, str)
                or _DISPATCH.ERROR_CODE.fullmatch(error) is None
            ))
            or (status in {"retry_wait", "failed"}) != (error is not None)
            or succeeded != remote_valid
            or (not succeeded and any(
                item is not None for item in (
                    delivery_id, remote_status, remote_new, response_sha256,
                )
            ))
        ):
            raise CMSRemovalBlocked("removal.state_invalid")

    def _claim(self, claim: Any) -> RemovalDispatchClaim:
        if not isinstance(claim, RemovalDispatchClaim):
            raise CMSRemovalBlocked("removal.claim_invalid")
        if claim.operation not in OPERATIONS:
            raise CMSRemovalBlocked("removal.claim_invalid")
        _DISPATCH._token(claim.request_id, "removal.claim_invalid")
        _DISPATCH._token(claim.event_id, "removal.claim_invalid")
        _DISPATCH._token(
            claim.lease_owner, "removal.claim_invalid", limit=128,
        )
        _DISPATCH._token(claim.lease_token, "removal.claim_invalid")
        if (
            not isinstance(claim.payload_json, str)
            or not isinstance(claim.payload_sha256, str)
            or _DISPATCH.SHA256.fullmatch(claim.payload_sha256) is None
            or _DISPATCH._hash(claim.payload_json) != claim.payload_sha256
            or isinstance(claim.attempt, bool)
            or not isinstance(claim.attempt, int)
            or isinstance(claim.max_attempts, bool)
            or not isinstance(claim.max_attempts, int)
            or not 1 <= claim.attempt <= claim.max_attempts <= _DISPATCH.MAX_ATTEMPTS
        ):
            raise CMSRemovalBlocked("removal.claim_invalid")
        try:
            operation, request_id, event_id, payload_json, payload_sha256 = (
                _removal(json.loads(claim.payload_json))
            )
        except (json.JSONDecodeError, TypeError, CMSRemovalBlocked):
            raise CMSRemovalBlocked("removal.claim_invalid") from None
        if (
            operation != claim.operation
            or request_id != claim.request_id
            or event_id != claim.event_id
            or payload_json != claim.payload_json
            or payload_sha256 != claim.payload_sha256
        ):
            raise CMSRemovalBlocked("removal.claim_invalid")
        _DISPATCH._timestamp(
            claim.lease_expires_at, "removal.claim_invalid",
        )
        return claim

    def _require_live_claim(
        self,
        claim: RemovalDispatchClaim,
        now: float,
    ) -> sqlite3.Row:
        row = self.connection.execute("""
            SELECT * FROM cms_source_removal_outbox
            WHERE operation = ? AND request_id = ?
        """, (claim.operation, claim.request_id)).fetchone()
        if row is None:
            raise CMSRemovalBlocked("removal.claim_missing")
        self._validated_row(row)
        if (
            row["status"] != "leased"
            or row["event_id"] != claim.event_id
            or row["payload_sha256"] != claim.payload_sha256
            or row["attempts"] != claim.attempt
            or row["max_attempts"] != claim.max_attempts
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
            or row["lease_expires_at"] != claim.lease_expires_at
        ):
            raise CMSRemovalBlocked("removal.claim_lost")
        if row["lease_expires_at"] <= now:
            raise CMSRemovalBlocked("removal.lease_expired")
        if now < row["updated_at"]:
            raise CMSRemovalBlocked("removal.clock_regressed")
        return row
