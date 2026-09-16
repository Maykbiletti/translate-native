#!/usr/bin/env python3
"""Durable caller-owned outbox for the public website submission client.

The CMS backend commits one immutable change, cancellation, or tombstone before
network access. One leased attempt performs one client call. A crash after
remote acceptance safely replays the same request identity after lease expiry;
the public website edge remains the idempotency authority.
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


SCHEMA_VERSION = 2
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
MAX_DELAY_SECONDS = 86_400.0
STATUSES = ("pending", "leased", "retry_wait", "accepted", "failed")
OPERATIONS = ("change", "cancellation", "tombstone")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_META_COLUMNS = (
    "singleton", "schema_version", "expected_capabilities_sha256",
)
_COLUMNS = (
    "operation", "request_id", "event_id", "site_id", "payload_json",
    "payload_sha256", "commercial_contract_binding_json",
    "source_max_attempts", "delivery_max_attempts",
    "client_max_attempts", "status", "attempts", "next_attempt_at",
    "lease_owner", "lease_token", "lease_expires_at", "last_error_code",
    "remote_status", "remote_attempts", "remote_capabilities_sha256",
    "remote_binding_sha256", "response_json", "response_sha256", "created_at",
    "updated_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load submission dispatch dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CLIENT = _load_module(
    "blun_website_localization_submission_dispatch_client",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_client.py",
)


class CMSSourceDeliverySubmissionDispatchBlocked(RuntimeError):
    """Stable content-free failure of the caller-owned submission outbox."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("submission dispatch error code is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SubmissionDispatchClaim:
    operation: str
    request_id: str
    event_id: str
    site_id: str
    payload_sha256: str
    commercial_contract_binding: dict[str, Any] | None
    source_max_attempts: int
    delivery_max_attempts: int
    attempt: int
    client_max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float
    payload_json: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class SubmissionDispatchStatus:
    operation: str
    request_id: str
    event_id: str
    site_id: str
    payload_sha256: str
    commercial_contract_binding: dict[str, Any] | None
    source_max_attempts: int
    delivery_max_attempts: int
    status: str
    attempts: int
    client_max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    remote_status: str | None
    remote_attempts: int | None
    remote_capabilities_sha256: str | None
    remote_binding_sha256: str | None
    remote_website_capability_binding: dict[str, Any] | None
    response_sha256: str | None


@dataclass(frozen=True)
class SubmissionDispatchOutcome:
    operation: str
    request_id: str
    status: str
    attempt: int
    client_max_attempts: int
    next_attempt_at: float
    error_code: str | None


@dataclass(frozen=True)
class SubmissionDispatchHealth:
    status: str
    counts: dict[str, int]
    operations: dict[str, int]
    due: int
    expired_leases: int
    failed: int
    expected_capabilities_sha256: str


@dataclass(frozen=True)
class SubmissionDispatchLifecycle:
    dispatch_status: SubmissionDispatchStatus
    source_lifecycle: dict[str, Any]


@dataclass(frozen=True)
class SubmissionDispatchCommercialProfile:
    commercial_profile: dict[str, Any]
    commercial_rendering_registry: dict[str, Any]
    website_capability_binding: dict[str, Any]


def _blocked(code: str) -> CMSSourceDeliverySubmissionDispatchBlocked:
    return CMSSourceDeliverySubmissionDispatchBlocked(
        "source_delivery_submission_dispatch." + code
    )


def _token(value: Any, code: str, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or TOKEN.fullmatch(value) is None
    ):
        raise _blocked(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise _blocked(code)
    return value


def _count(value: Any, code: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ATTEMPTS
    ):
        raise _blocked(code)
    return value


def _timestamp(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _blocked(code)
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise _blocked(code)
    return result


def _duration(value: Any, code: str, *, maximum: float) -> float:
    result = _timestamp(value, code)
    if result <= 0 or result > maximum:
        raise _blocked(code)
    return result


def _canonical(value: Any, code: str) -> str:
    try:
        raw = _CLIENT._canonical(value)
        return raw.decode("utf-8")
    except Exception:
        raise _blocked(code) from None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _commercial_contract() -> dict[str, dict[str, Any]]:
    """Build and validate the content-free public commercial generation."""

    try:
        commercial = _CLIENT._CMS._COMMERCIAL
        planner = _CLIENT._CMS._PLANNER
        profile = commercial.public_profile(planner.COMMERCIAL_PROFILE)
        registry = planner.commercial_rendering_registry()
        profile_unsigned = dict(profile)
        profile_sha256 = profile_unsigned.pop("sha256")
        registry_unsigned = dict(registry)
        registry_sha256 = registry_unsigned.pop("sha256")
        expected_locales = [item.locale for item in planner.EU_OFFICIAL_LOCALES]
        if (
            profile.get("schema") != commercial.PUBLIC_PROFILE_SCHEMA
            or profile.get("profile") != planner.COMMERCIAL_PROFILE
            or [item.get("name") for item in profile.get("dimensions", ())]
            != list(commercial.DIMENSIONS)
            or profile.get("protected_terms") != "project-configuration-only"
            or profile_sha256
            != _digest(_canonical(profile_unsigned, "commercial_profile_invalid"))
            or registry.get("schema")
            != planner.COMMERCIAL_RENDERING_REGISTRY_SCHEMA
            or registry.get("commercial_profile") != planner.COMMERCIAL_PROFILE
            or registry.get("content_policy") != {
                "source_text": False,
                "target_text": False,
                "project_prices": False,
                "project_brands": False,
                "credentials": False,
            }
            or [item.get("locale") for item in registry.get("locales", ())]
            != expected_locales
            or len(set(expected_locales)) != 24
            or registry_sha256
            != _digest(_canonical(registry_unsigned, "commercial_profile_invalid"))
        ):
            raise ValueError
        copied = json.loads(_canonical({
            "commercial_profile": profile,
            "commercial_rendering_registry": registry,
        }, "commercial_profile_invalid"))
        return copied
    except CMSSourceDeliverySubmissionDispatchBlocked:
        raise
    except Exception:
        raise _blocked("commercial_profile_invalid") from None


def _commercial_contract_binding() -> dict[str, str]:
    contract = _commercial_contract()
    return {
        "schema": (
            "blun.cms-public-submission-dispatch-"
            "commercial-contract-binding.v1"
        ),
        "commercial_profile": contract["commercial_profile"]["profile"],
        "commercial_profile_sha256": contract["commercial_profile"]["sha256"],
        "commercial_rendering_registry_sha256": contract[
            "commercial_rendering_registry"
        ]["sha256"],
    }


def _commercial_binding_for_payload(payload: Mapping[str, Any]) -> dict[str, str] | None:
    commercial = (
        payload.get("schema") == _CLIENT._CMS.CHANGE_SCHEMA
        and isinstance(payload.get("localization"), Mapping)
        and payload["localization"].get("content_type") == "commercial"
    )
    return _commercial_contract_binding() if commercial else None


def _stored_commercial_binding(
    value: Any, payload: Mapping[str, Any], code: str,
) -> dict[str, str] | None:
    expected = _commercial_binding_for_payload(payload)
    if expected is None:
        if value is not None:
            raise _blocked(code)
        return None
    if not isinstance(value, str):
        raise _blocked(code)
    try:
        copied = json.loads(value)
        if (
            copied != expected
            or _canonical(copied, code) != value
        ):
            raise ValueError
    except CMSSourceDeliverySubmissionDispatchBlocked:
        raise
    except Exception:
        raise _blocked(code) from None
    return expected


def _payload(value: Any) -> tuple[dict[str, Any], dict[str, str], str]:
    if not isinstance(value, Mapping):
        raise _blocked("request_invalid")
    try:
        copied, _raw = _CLIENT._copy_payload(
            value, change=value.get("schema") == _CLIENT._CMS.CHANGE_SCHEMA,
        )
        identity = _CLIENT._identity(
            copied, change=copied.get("schema") == _CLIENT._CMS.CHANGE_SCHEMA,
        )
    except Exception:
        raise _blocked("request_invalid") from None
    return copied, identity, _canonical(copied, "request_invalid")


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise _blocked("transaction_nested")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall())


def _normalized_schema(
    snapshot: tuple[tuple[Any, ...], ...],
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (*row[:3], " ".join(row[3].split()) if isinstance(row[3], str) else row[3])
        for row in snapshot
    )


def _initialize_legacy_v1_schema(
    connection: sqlite3.Connection, expected_capabilities_sha256: str,
) -> None:
    connection.execute("""
            CREATE TABLE cms_public_submission_outbox_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                expected_capabilities_sha256 TEXT NOT NULL
            )
        """)
    connection.execute("""
            INSERT INTO cms_public_submission_outbox_meta
            VALUES (1, 1, ?)
        """, (expected_capabilities_sha256,))
    connection.execute("""
            CREATE TABLE cms_public_submission_outbox (
                operation TEXT NOT NULL CHECK (
                    operation IN ('change', 'cancellation', 'tombstone')
                ),
                request_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                site_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                source_max_attempts INTEGER NOT NULL CHECK (
                    source_max_attempts BETWEEN 1 AND 20
                ),
                delivery_max_attempts INTEGER NOT NULL CHECK (
                    delivery_max_attempts BETWEEN 1 AND 20
                ),
                client_max_attempts INTEGER NOT NULL CHECK (
                    client_max_attempts BETWEEN 1 AND 20
                ),
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'leased', 'retry_wait',
                               'accepted', 'failed')
                ),
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                next_attempt_at REAL NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at REAL,
                last_error_code TEXT,
                remote_status TEXT,
                remote_attempts INTEGER,
                remote_capabilities_sha256 TEXT,
                remote_binding_sha256 TEXT,
                response_json TEXT,
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
    connection.execute("""
            CREATE INDEX cms_public_submission_outbox_due
            ON cms_public_submission_outbox (
                status, next_attempt_at, created_at, operation, request_id
            )
        """)
    connection.commit()


def _legacy_v1_schema(expected_capabilities_sha256: str) -> tuple[tuple[Any, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        _initialize_legacy_v1_schema(connection, expected_capabilities_sha256)
        return _schema_snapshot(connection)
    finally:
        connection.close()


def _is_empty_legacy_v1(
    connection: sqlite3.Connection, expected_capabilities_sha256: str,
) -> bool:
    try:
        if _normalized_schema(_schema_snapshot(connection)) != _normalized_schema(
            _legacy_v1_schema(expected_capabilities_sha256)
        ):
            return False
        meta = connection.execute(
            "SELECT * FROM cms_public_submission_outbox_meta"
        ).fetchall()
        count = connection.execute(
            "SELECT COUNT(*) FROM cms_public_submission_outbox"
        ).fetchone()[0]
        return (
            len(meta) == 1
            and tuple(meta[0]) == (1, 1, expected_capabilities_sha256)
            and count == 0
        )
    except Exception:
        return False


class DurableCMSSourceDeliverySubmissionDispatcher:
    """Persist and submit exact public website requests at least once."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        expected_capabilities_sha256: str,
        *,
        base_delay_seconds: float | int = 5,
        max_delay_seconds: float | int = 300,
    ):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.expected_capabilities_sha256 = _sha256(
            expected_capabilities_sha256, "capabilities_invalid",
        )
        self.base_delay_seconds = _duration(
            base_delay_seconds, "delay_invalid", maximum=MAX_DELAY_SECONDS,
        )
        self.max_delay_seconds = _duration(
            max_delay_seconds, "delay_invalid", maximum=MAX_DELAY_SECONDS,
        )
        if self.base_delay_seconds > self.max_delay_seconds:
            raise _blocked("delay_invalid")
        self._initialize()

    def _initialize(self) -> None:
        existing = _schema_snapshot(self.connection)
        if existing:
            if not _is_empty_legacy_v1(
                self.connection, self.expected_capabilities_sha256
            ):
                self._validate_schema()
                return
        with _transaction(self.connection):
            if existing:
                self.connection.execute(
                    "DROP TABLE cms_public_submission_outbox"
                )
                self.connection.execute(
                    "DROP TABLE cms_public_submission_outbox_meta"
                )
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_public_submission_outbox_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 2),
                    expected_capabilities_sha256 TEXT NOT NULL
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO cms_public_submission_outbox_meta
                VALUES (1, 2, ?)
            """, (self.expected_capabilities_sha256,))
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cms_public_submission_outbox (
                    operation TEXT NOT NULL CHECK (
                        operation IN ('change', 'cancellation', 'tombstone')
                    ),
                    request_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    commercial_contract_binding_json TEXT,
                    source_max_attempts INTEGER NOT NULL CHECK (
                        source_max_attempts BETWEEN 1 AND 20
                    ),
                    delivery_max_attempts INTEGER NOT NULL CHECK (
                        delivery_max_attempts BETWEEN 1 AND 20
                    ),
                    client_max_attempts INTEGER NOT NULL CHECK (
                        client_max_attempts BETWEEN 1 AND 20
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'leased', 'retry_wait',
                                   'accepted', 'failed')
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    remote_status TEXT,
                    remote_attempts INTEGER,
                    remote_capabilities_sha256 TEXT,
                    remote_binding_sha256 TEXT,
                    response_json TEXT,
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
                CREATE INDEX IF NOT EXISTS cms_public_submission_outbox_due
                ON cms_public_submission_outbox (
                    status, next_attempt_at, created_at, operation, request_id
                )
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_public_submission_outbox_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(cms_public_submission_outbox)"
        ).fetchall())
        meta = self.connection.execute(
            "SELECT * FROM cms_public_submission_outbox_meta"
        ).fetchall()
        if (
            meta_columns != _META_COLUMNS
            or columns != _COLUMNS
            or len(meta) != 1
            or tuple(meta[0])
            != (1, SCHEMA_VERSION, self.expected_capabilities_sha256)
        ):
            raise _blocked("schema_altered")

    def enqueue(
        self,
        payload: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
        client_max_attempts: int = 5,
        now: float | int,
    ) -> SubmissionDispatchStatus:
        self._validate_schema()
        source_max_attempts = _count(source_max_attempts, "attempts_invalid")
        delivery_max_attempts = _count(delivery_max_attempts, "attempts_invalid")
        client_max_attempts = _count(client_max_attempts, "attempts_invalid")
        now = _timestamp(now, "time_invalid")
        copied, identity, payload_json = _payload(payload)
        commercial_binding = _commercial_binding_for_payload(copied)
        commercial_binding_json = (
            None if commercial_binding is None
            else _canonical(commercial_binding, "commercial_profile_invalid")
        )
        operation = identity["operation"]
        request_id = identity["request_id"]
        with _transaction(self.connection):
            row = self.connection.execute("""
                SELECT payload_sha256, commercial_contract_binding_json,
                       source_max_attempts,
                       delivery_max_attempts, client_max_attempts
                FROM cms_public_submission_outbox
                WHERE operation = ? AND request_id = ?
            """, (operation, request_id)).fetchone()
            expected = (
                identity["payload_sha256"], commercial_binding_json,
                source_max_attempts,
                delivery_max_attempts, client_max_attempts,
            )
            if row is not None and tuple(row) != expected:
                raise _blocked("idempotency_collision")
            if row is None:
                self.connection.execute("""
                    INSERT INTO cms_public_submission_outbox (
                        operation, request_id, event_id, site_id, payload_json,
                        payload_sha256, commercial_contract_binding_json,
                        source_max_attempts,
                        delivery_max_attempts, client_max_attempts, status,
                        attempts, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """, (
                    operation, request_id, identity["event_id"],
                    identity["site_id"], payload_json,
                    identity["payload_sha256"], commercial_binding_json,
                    source_max_attempts,
                    delivery_max_attempts, client_max_attempts, now, now, now,
                ))
        return self.status(operation, request_id, now=now)

    def claim(
        self,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> SubmissionDispatchClaim | None:
        self._validate_schema()
        worker_id = _token(worker_id, "worker_invalid", limit=128)
        now = _timestamp(now, "time_invalid")
        lease_seconds = _duration(
            lease_seconds, "lease_invalid", maximum=MAX_LEASE_SECONDS,
        )
        claim = None
        integrity_error = False
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_public_submission_outbox
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'lease_expired',
                    updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts >= client_max_attempts
            """, (now, now))
            self.connection.execute("""
                UPDATE cms_public_submission_outbox
                SET status = 'retry_wait', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'lease_expired',
                    updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ?
                  AND attempts < client_max_attempts
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_public_submission_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND next_attempt_at <= ? AND attempts < client_max_attempts
                ORDER BY created_at,
                         CASE WHEN operation = 'change' THEN 1 ELSE 0 END,
                         operation, request_id LIMIT 1
            """, (now,)).fetchone()
            if row is not None:
                try:
                    self._validated_row(row)
                except CMSSourceDeliverySubmissionDispatchBlocked:
                    self.connection.execute("""
                        UPDATE cms_public_submission_outbox
                        SET status = 'failed',
                            last_error_code = 'payload_integrity', updated_at = ?
                        WHERE operation = ? AND request_id = ?
                    """, (now, row["operation"], row["request_id"]))
                    integrity_error = True
                else:
                    lease_token = secrets.token_urlsafe(32)
                    lease_expires_at = now + lease_seconds
                    updated = self.connection.execute("""
                        UPDATE cms_public_submission_outbox
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
                        raise _blocked("claim_lost")
                    claim = SubmissionDispatchClaim(
                        operation=row["operation"],
                        request_id=row["request_id"], event_id=row["event_id"],
                        site_id=row["site_id"],
                        payload_sha256=row["payload_sha256"],
                        commercial_contract_binding=_stored_commercial_binding(
                            row["commercial_contract_binding_json"],
                            json.loads(row["payload_json"]), "state_invalid",
                        ),
                        source_max_attempts=row["source_max_attempts"],
                        delivery_max_attempts=row["delivery_max_attempts"],
                        attempt=row["attempts"] + 1,
                        client_max_attempts=row["client_max_attempts"],
                        lease_owner=worker_id, lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        payload_json=row["payload_json"],
                    )
        if integrity_error:
            raise _blocked("payload_integrity")
        return claim

    def run_once(
        self,
        client: Any,
        worker_id: str,
        *,
        now: float | int,
        lease_seconds: float | int = 600,
    ) -> SubmissionDispatchOutcome | None:
        self._validate_client(client, lease_seconds)
        claim = self.claim(worker_id, now=now, lease_seconds=lease_seconds)
        if claim is None:
            return None
        try:
            payload = json.loads(claim.payload_json)
            if claim.operation == "change":
                response = client.submit_change(
                    payload,
                    source_max_attempts=claim.source_max_attempts,
                    delivery_max_attempts=claim.delivery_max_attempts,
                )
            else:
                response = client.submit_removal(
                    payload,
                    source_max_attempts=claim.source_max_attempts,
                    delivery_max_attempts=claim.delivery_max_attempts,
                )
            status = self.complete(claim, response, now=now)
        except Exception as error:
            client_failure = getattr(
                error, "cms_source_delivery_submission_client_failure", False,
            ) is True
            code = getattr(error, "code", "client_failure")
            if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                code = "client_failure"
            retryable = client_failure and getattr(error, "retryable", False) is True
            status = self._finish_error(claim, code, retryable=retryable, now=now)
        return SubmissionDispatchOutcome(
            operation=status.operation, request_id=status.request_id,
            status=status.status, attempt=status.attempts,
            client_max_attempts=status.client_max_attempts,
            next_attempt_at=status.next_attempt_at,
            error_code=status.last_error_code,
        )

    def _validate_client(self, client: Any, lease_seconds: Any) -> None:
        if (
            not callable(getattr(client, "submit_change", None))
            or not callable(getattr(client, "submit_removal", None))
            or getattr(client, "expected_capabilities_sha256", None)
            != self.expected_capabilities_sha256
        ):
            raise _blocked("client_invalid")
        lease_seconds = _duration(
            lease_seconds, "lease_invalid", maximum=MAX_LEASE_SECONDS,
        )
        timeout = getattr(client, "timeout", None)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise _blocked("client_invalid")
        if lease_seconds <= float(timeout):
            raise _blocked("lease_too_short")

    def complete(
        self, claim: SubmissionDispatchClaim, response: Any, *, now: float | int,
    ) -> SubmissionDispatchStatus:
        self._validate_schema()
        claim = self._claim(claim)
        now = _timestamp(now, "time_invalid")
        if not isinstance(response, Mapping):
            raise _blocked("response_invalid")
        response_json = _canonical(dict(response), "response_invalid")
        copied = json.loads(response_json)
        fields = {
            "schema", "api_schema", "operation", "request_id", "event_id",
            "site_id", "payload_sha256", "status", "attempts",
            "delivery_max_attempts", "source_max_attempts",
            "capabilities_sha256", "website_capability_binding",
            "accepted_implies_publication",
        }
        binding = copied.get("website_capability_binding")
        expected_schema = (
            _CLIENT._HTTP.CHANGE_RESPONSE_SCHEMA
            if claim.operation == "change"
            else _CLIENT._HTTP.REMOVAL_RESPONSE_SCHEMA
        )
        try:
            normalized_binding = _CLIENT._HTTP._binding(binding)
        except Exception:
            raise _blocked("response_invalid") from None
        valid = (
            set(copied) == fields
            and copied.get("schema") == expected_schema
            and copied.get("api_schema") == _CLIENT._HTTP.API_SCHEMA
            and copied.get("operation") == claim.operation
            and copied.get("request_id") == claim.request_id
            and copied.get("event_id") == claim.event_id
            and copied.get("site_id") == claim.site_id
            and copied.get("payload_sha256") == claim.payload_sha256
            and copied.get("source_max_attempts") == claim.source_max_attempts
            and copied.get("delivery_max_attempts") == claim.delivery_max_attempts
            and copied.get("accepted_implies_publication") is False
            and copied.get("status") in _CLIENT._HTTP.STATUSES
            and isinstance(copied.get("attempts"), int)
            and not isinstance(copied.get("attempts"), bool)
            and 0 <= copied["attempts"] <= claim.delivery_max_attempts
            and _sha256(copied.get("capabilities_sha256"), "response_invalid")
            == copied.get("capabilities_sha256")
            and isinstance(binding, dict)
            and normalized_binding == binding
            and copied.get("capabilities_sha256")
            == binding.get("delivery_capabilities_sha256")
            and (
                claim.commercial_contract_binding is None
                or binding.get("commercial_rendering_registry_sha256")
                == claim.commercial_contract_binding[
                    "commercial_rendering_registry_sha256"
                ]
            )
        )
        if not valid:
            raise _blocked("response_invalid")
        binding_json = _canonical(binding, "response_invalid")
        with _transaction(self.connection):
            self._require_live_claim(claim, now)
            updated = self.connection.execute("""
                UPDATE cms_public_submission_outbox
                SET status = 'accepted', next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    remote_status = ?, remote_attempts = ?,
                    remote_capabilities_sha256 = ?,
                    remote_binding_sha256 = ?, response_json = ?,
                    response_sha256 = ?,
                    updated_at = ?
                WHERE operation = ? AND request_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                now, copied["status"], copied["attempts"],
                copied["capabilities_sha256"], _digest(binding_json),
                response_json, _digest(response_json), now, claim.operation,
                claim.request_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise _blocked("completion_lost")
        return self.status(claim.operation, claim.request_id, now=now)

    def _finish_error(
        self, claim: SubmissionDispatchClaim, code: str, *, retryable: bool,
        now: float | int,
    ) -> SubmissionDispatchStatus:
        now = _timestamp(now, "time_invalid")
        self._claim(claim)
        terminal = not retryable or claim.attempt >= claim.client_max_attempts
        status = "failed" if terminal else "retry_wait"
        delay = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, claim.attempt - 1)),
        )
        next_attempt_at = now if terminal else now + delay
        with _transaction(self.connection):
            self._require_live_claim(claim, now)
            updated = self.connection.execute("""
                UPDATE cms_public_submission_outbox
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, updated_at = ?
                WHERE operation = ? AND request_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                status, next_attempt_at, code, now, claim.operation,
                claim.request_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise _blocked("failure_lost")
        return self.status(claim.operation, claim.request_id, now=now)

    def status(
        self, operation: str, request_id: str, *, now: float | int,
    ) -> SubmissionDispatchStatus:
        self._validate_schema()
        if operation not in OPERATIONS:
            raise _blocked("identity_invalid")
        request_id = _token(request_id, "identity_invalid")
        now = _timestamp(now, "time_invalid")
        row = self.connection.execute("""
            SELECT * FROM cms_public_submission_outbox
            WHERE operation = ? AND request_id = ?
        """, (operation, request_id)).fetchone()
        if row is None:
            raise _blocked("submission_missing")
        self._validated_row(row)
        lease_expires_at = row["lease_expires_at"]
        remote_binding = None
        if row["status"] == "accepted":
            try:
                response = json.loads(row["response_json"])
                remote_binding = _CLIENT._HTTP._binding(
                    response["website_capability_binding"]
                )
                if (
                    response["website_capability_binding"] != remote_binding
                    or _digest(_canonical(remote_binding, "state_invalid"))
                    != row["remote_binding_sha256"]
                ):
                    raise ValueError
            except Exception:
                raise _blocked("state_invalid") from None
        return SubmissionDispatchStatus(
            operation=row["operation"], request_id=row["request_id"],
            event_id=row["event_id"], site_id=row["site_id"],
            payload_sha256=row["payload_sha256"],
            commercial_contract_binding=_stored_commercial_binding(
                row["commercial_contract_binding_json"],
                json.loads(row["payload_json"]), "state_invalid",
            ),
            source_max_attempts=row["source_max_attempts"],
            delivery_max_attempts=row["delivery_max_attempts"],
            status=row["status"], attempts=row["attempts"],
            client_max_attempts=row["client_max_attempts"],
            next_attempt_at=float(row["next_attempt_at"]),
            lease_expires_at=(
                None if lease_expires_at is None else float(lease_expires_at)
            ),
            lease_expired=(
                row["status"] == "leased" and lease_expires_at <= now
            ),
            last_error_code=row["last_error_code"],
            remote_status=row["remote_status"],
            remote_attempts=row["remote_attempts"],
            remote_capabilities_sha256=row["remote_capabilities_sha256"],
            remote_binding_sha256=row["remote_binding_sha256"],
            remote_website_capability_binding=remote_binding,
            response_sha256=row["response_sha256"],
        )

    def lifecycle(
        self,
        client: Any,
        operation: str,
        request_id: str,
        *,
        now: float | int,
    ) -> SubmissionDispatchLifecycle:
        """Read the verified downstream lifecycle after durable website intake."""

        self._validate_read_client(client)
        status = self.status(operation, request_id, now=now)
        if status.status != "accepted":
            raise _blocked("lifecycle_not_accepted")
        identity = {
            "operation": status.operation,
            "request_id": status.request_id,
            "event_id": status.event_id,
            "site_id": status.site_id,
            "payload_sha256": status.payload_sha256,
        }
        try:
            response = client.submission_lifecycle(**identity)
            if (
                not isinstance(response, Mapping)
                or set(response) != {
                    "schema", "api_schema", "result",
                    "accepted_implies_publication",
                }
                or response.get("schema")
                != _CLIENT._HTTP._SUBMISSION.LIFECYCLE_HTTP_RESPONSE_SCHEMA
                or response.get("api_schema") != _CLIENT._HTTP.API_SCHEMA
                or response.get("accepted_implies_publication") is not False
                or not isinstance(response.get("result"), Mapping)
            ):
                raise ValueError
            normalized = _CLIENT._HTTP._submission_lifecycle_payload(
                _CLIENT._PayloadView(response["result"]), identity,
            )
            if (
                normalized != response["result"]
                or normalized["website_capability_binding"]
                != status.remote_website_capability_binding
            ):
                raise ValueError
            copied = json.loads(_canonical(dict(response), "lifecycle_invalid"))
        except CMSSourceDeliverySubmissionDispatchBlocked:
            raise
        except Exception:
            raise _blocked("lifecycle_invalid") from None
        return SubmissionDispatchLifecycle(
            dispatch_status=status,
            source_lifecycle=copied,
        )

    def _validate_read_client(self, client: Any) -> None:
        if (
            not callable(getattr(client, "submission_lifecycle", None))
            or getattr(client, "expected_capabilities_sha256", None)
            != self.expected_capabilities_sha256
        ):
            raise _blocked("client_invalid")
        timeout = getattr(client, "timeout", None)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise _blocked("client_invalid")

    def commercial_profile(
        self, client: Any,
    ) -> SubmissionDispatchCommercialProfile:
        """Return the public offer contract bound to the live website generation."""

        if (
            not callable(getattr(client, "capabilities", None))
            or getattr(client, "expected_capabilities_sha256", None)
            != self.expected_capabilities_sha256
        ):
            raise _blocked("client_invalid")
        timeout = getattr(client, "timeout", None)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise _blocked("client_invalid")
        try:
            response = client.capabilities()
            if (
                not isinstance(response, Mapping)
                or set(response) != {"schema", "api_schema", "capabilities"}
                or response.get("schema")
                != _CLIENT._HTTP._SUBMISSION.CAPABILITIES_HTTP_RESPONSE_SCHEMA
                or response.get("api_schema")
                != _CLIENT._HTTP._CAPABILITIES.API_SCHEMA
                or not isinstance(response.get("capabilities"), Mapping)
            ):
                raise ValueError
            normalized = _CLIENT._HTTP._CAPABILITIES._capabilities(
                _CLIENT._PayloadView(response["capabilities"])
            )
            if (
                normalized != response["capabilities"]
                or normalized["sha256"] != self.expected_capabilities_sha256
            ):
                raise ValueError
            contract = _commercial_contract()
            binding = normalized["website_capability_binding"]
            if (
                binding["commercial_rendering_registry_sha256"]
                != contract["commercial_rendering_registry"]["sha256"]
            ):
                raise ValueError
            return SubmissionDispatchCommercialProfile(
                commercial_profile=contract["commercial_profile"],
                commercial_rendering_registry=(
                    contract["commercial_rendering_registry"]
                ),
                website_capability_binding=dict(binding),
            )
        except CMSSourceDeliverySubmissionDispatchBlocked:
            raise
        except Exception:
            raise _blocked("commercial_profile_invalid") from None

    def health(self, *, now: float | int) -> SubmissionDispatchHealth:
        now = _timestamp(now, "time_invalid")
        self._validate_schema()
        quick = self.connection.execute("PRAGMA quick_check").fetchall()
        if len(quick) != 1 or tuple(quick[0]) != ("ok",):
            raise _blocked("database_integrity")
        rows = self.connection.execute(
            "SELECT * FROM cms_public_submission_outbox "
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
            row["status"] in {"pending", "retry_wait"}
            and row["next_attempt_at"] <= now
            for row in rows
        )
        expired = sum(
            row["status"] == "leased" and row["lease_expires_at"] <= now
            for row in rows
        )
        return SubmissionDispatchHealth(
            status="blocked" if expired or counts["failed"] else "ok",
            counts=counts, operations=operations, due=due,
            expired_leases=expired, failed=counts["failed"],
            expected_capabilities_sha256=self.expected_capabilities_sha256,
        )

    def _validated_row(self, row: sqlite3.Row) -> None:
        try:
            copied, identity, payload_json = _payload(json.loads(row["payload_json"]))
            _stored_commercial_binding(
                row["commercial_contract_binding_json"], copied, "state_invalid",
            )
            operation = identity["operation"]
            request_id = identity["request_id"]
            created = _timestamp(row["created_at"], "state_invalid")
            updated = _timestamp(row["updated_at"], "state_invalid")
            next_at = _timestamp(row["next_attempt_at"], "state_invalid")
            source_attempts = _count(row["source_max_attempts"], "state_invalid")
            delivery_attempts = _count(row["delivery_max_attempts"], "state_invalid")
            client_attempts = _count(row["client_max_attempts"], "state_invalid")
        except Exception:
            raise _blocked("state_invalid") from None
        status = row["status"]
        attempts = row["attempts"]
        lease = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        remote = (
            row["remote_status"], row["remote_attempts"],
            row["remote_capabilities_sha256"], row["remote_binding_sha256"],
            row["response_json"], row["response_sha256"],
        )
        error = row["last_error_code"]
        valid_remote = status == "accepted"
        if (
            operation != row["operation"] or request_id != row["request_id"]
            or identity["event_id"] != row["event_id"]
            or identity["site_id"] != row["site_id"]
            or identity["payload_sha256"] != row["payload_sha256"]
            or payload_json != row["payload_json"]
            or source_attempts != row["source_max_attempts"]
            or delivery_attempts != row["delivery_max_attempts"]
            or client_attempts != row["client_max_attempts"]
            or status not in STATUSES
            or isinstance(attempts, bool) or not isinstance(attempts, int)
            or not 0 <= attempts <= client_attempts
            or next_at < created or updated < created
            or (status == "leased") != all(value is not None for value in lease)
            or (status != "leased" and any(value is not None for value in lease))
            or (status == "leased" and (
                _token(lease[0], "state_invalid", limit=128) != lease[0]
                or _token(lease[1], "state_invalid") != lease[1]
                or _timestamp(lease[2], "state_invalid") <= updated
            ))
            or (status in {"retry_wait", "failed"}) != (error is not None)
            or (error is not None and (
                not isinstance(error, str) or ERROR_CODE.fullmatch(error) is None
            ))
            or valid_remote != all(value is not None for value in remote)
            or (not valid_remote and any(value is not None for value in remote))
            or (valid_remote and (
                remote[0] not in _CLIENT._HTTP.STATUSES
                or isinstance(remote[1], bool)
                or not isinstance(remote[1], int)
                or not 0 <= remote[1] <= delivery_attempts
                or any(
                    not isinstance(value, str) or SHA256.fullmatch(value) is None
                    for value in (remote[2], remote[3], remote[5])
                )
                or not isinstance(remote[4], str)
                or not self._valid_stored_response(row, remote[4])
            ))
        ):
            raise _blocked("state_invalid")

    def _valid_stored_response(self, row: sqlite3.Row, response_json: str) -> bool:
        try:
            response = json.loads(response_json)
            payload = json.loads(row["payload_json"])
            commercial_binding = _stored_commercial_binding(
                row["commercial_contract_binding_json"], payload,
                "state_invalid",
            )
            binding = _CLIENT._HTTP._binding(
                response.get("website_capability_binding")
            )
            expected_schema = (
                _CLIENT._HTTP.CHANGE_RESPONSE_SCHEMA
                if row["operation"] == "change"
                else _CLIENT._HTTP.REMOVAL_RESPONSE_SCHEMA
            )
            fields = {
                "schema", "api_schema", "operation", "request_id", "event_id",
                "site_id", "payload_sha256", "status", "attempts",
                "delivery_max_attempts", "source_max_attempts",
                "capabilities_sha256", "website_capability_binding",
                "accepted_implies_publication",
            }
            return (
                isinstance(response, dict)
                and set(response) == fields
                and _canonical(response, "state_invalid") == response_json
                and _digest(response_json) == row["response_sha256"]
                and response["schema"] == expected_schema
                and response["api_schema"] == _CLIENT._HTTP.API_SCHEMA
                and response["operation"] == row["operation"]
                and response["request_id"] == row["request_id"]
                and response["event_id"] == row["event_id"]
                and response["site_id"] == row["site_id"]
                and response["payload_sha256"] == row["payload_sha256"]
                and response["source_max_attempts"]
                == row["source_max_attempts"]
                and response["delivery_max_attempts"]
                == row["delivery_max_attempts"]
                and response["status"] == row["remote_status"]
                and response["attempts"] == row["remote_attempts"]
                and response["capabilities_sha256"]
                == row["remote_capabilities_sha256"]
                and response["capabilities_sha256"]
                == binding["delivery_capabilities_sha256"]
                and (
                    commercial_binding is None
                    or binding["commercial_rendering_registry_sha256"]
                    == commercial_binding[
                        "commercial_rendering_registry_sha256"
                    ]
                )
                and _digest(_canonical(binding, "state_invalid"))
                == row["remote_binding_sha256"]
                and response["website_capability_binding"] == binding
                and response["accepted_implies_publication"] is False
            )
        except Exception:
            return False

    def _claim(self, claim: Any) -> SubmissionDispatchClaim:
        if not isinstance(claim, SubmissionDispatchClaim):
            raise _blocked("claim_invalid")
        try:
            payload, identity, payload_json = _payload(json.loads(claim.payload_json))
            expected_commercial_binding = _commercial_binding_for_payload(payload)
        except Exception:
            raise _blocked("claim_invalid") from None
        if (
            claim.operation != identity["operation"]
            or claim.request_id != identity["request_id"]
            or claim.event_id != identity["event_id"]
            or claim.site_id != identity["site_id"]
            or claim.payload_sha256 != identity["payload_sha256"]
            or claim.payload_json != payload_json
            or claim.commercial_contract_binding != expected_commercial_binding
            or isinstance(claim.attempt, bool)
            or not isinstance(claim.attempt, int)
            or not 1 <= claim.attempt <= claim.client_max_attempts <= MAX_ATTEMPTS
        ):
            raise _blocked("claim_invalid")
        _token(claim.lease_owner, "claim_invalid", limit=128)
        _token(claim.lease_token, "claim_invalid")
        _timestamp(claim.lease_expires_at, "claim_invalid")
        _count(claim.source_max_attempts, "claim_invalid")
        _count(claim.delivery_max_attempts, "claim_invalid")
        _count(claim.client_max_attempts, "claim_invalid")
        return claim

    def _require_live_claim(
        self, claim: SubmissionDispatchClaim, now: float,
    ) -> None:
        row = self.connection.execute("""
            SELECT status, lease_owner, lease_token, lease_expires_at,
                   commercial_contract_binding_json
            FROM cms_public_submission_outbox
            WHERE operation = ? AND request_id = ?
        """, (claim.operation, claim.request_id)).fetchone()
        if (
            row is None or row["status"] != "leased"
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
            or row["lease_expires_at"] != claim.lease_expires_at
            or row["lease_expires_at"] <= now
            or row["commercial_contract_binding_json"] != (
                None if claim.commercial_contract_binding is None else
                _canonical(claim.commercial_contract_binding, "claim_lost")
            )
        ):
            raise _blocked("claim_lost")
