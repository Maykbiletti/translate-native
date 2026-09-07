#!/usr/bin/env python3
"""Signed CMS ingress and all-locales publication outbox.

The host owns the SQLite connections, signature authorities, and transport.
This module accepts one authenticated content-change event, resumes its
idempotent queue insertion, and publishes only a complete signed locale bundle.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol


SCHEMA_VERSION = 1
CHANGE_SCHEMA = "blun.cms-content-change.v1"
PUBLICATION_SCHEMA = "blun.cms-localization-publication.v1"
ACK_SCHEMA = "blun.cms-localization-publication-ack.v1"
MAX_MESSAGE_BYTES = 4_000_000
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SIGNATURE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+=-]{1,4096}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_EVENT_COLUMNS = (
    "event_id", "event_sha256", "event_json", "signature_algorithm", "key_id",
    "signature", "plan_id", "status", "created_at", "updated_at",
)
_DELIVERY_COLUMNS = (
    "delivery_id", "event_id", "plan_id", "payload_json", "payload_sha256",
    "signature_algorithm", "key_id", "signature", "status", "attempts",
    "max_attempts", "next_attempt_at", "lease_owner", "lease_token",
    "lease_expires_at", "last_error_code", "last_error_detail_hash",
    "created_at", "updated_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required CMS dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_PLANNER = _load_module(
    "blun_website_localization_cms_planner",
    _ROOT / "integrations" / "website_localization.py",
)
_RELEASE = _load_module(
    "blun_website_localization_cms_release",
    _ROOT / "integrations" / "website_localization_release.py",
)
_QUEUE = _RELEASE._QUEUE


class CMSBridgeBlocked(RuntimeError):
    """Stable, content-free CMS bridge failure."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("CMS failure code is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CMSMessageSignature:
    algorithm: str
    key_id: str
    signature: str


class CMSMessageAuthority(Protocol):
    def sign(self, payload: bytes) -> CMSMessageSignature: ...
    def verify(self, payload: bytes, signature: CMSMessageSignature) -> bool: ...


class CMSPublisher(Protocol):
    def publish(self, request: "CMSPublicationRequest") -> Mapping[str, Any]: ...


class CMSPublishFailed(RuntimeError):
    """Provider-neutral transport failure with an explicit retry decision."""

    def __init__(self, code: str, *, retryable: bool, detail: str | None = None):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("publisher failure code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be boolean")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("publisher failure detail must be text")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.detail = detail


@dataclass(frozen=True)
class IngestedChange:
    event_id: str
    plan_id: str
    job_count: int
    inserted_jobs: int
    status: str


@dataclass(frozen=True)
class CMSPublicationRequest:
    delivery_id: str
    payload: dict[str, Any]
    payload_sha256: str
    signature: CMSMessageSignature


@dataclass(frozen=True)
class ClaimedDelivery:
    request: CMSPublicationRequest
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class DeliveryStatus:
    delivery_id: str
    event_id: str
    plan_id: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    last_error_code: str | None
    last_error_detail_hash: str | None
    payload_sha256: str


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    delivery_id: str | None = None
    attempt: int | None = None
    error_code: str | None = None


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise CMSBridgeBlocked("cms.json.invalid") from error
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise CMSBridgeBlocked("cms.message.too_large")
    return encoded


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: Any, code: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CMSBridgeBlocked(code)
    if len(value) > limit or "\x00" in value or not unicodedata.is_normalized("NFC", value):
        raise CMSBridgeBlocked(code)
    return value


def _token(value: Any, code: str) -> str:
    value = _text(value, code)
    if TOKEN.fullmatch(value) is None:
        raise CMSBridgeBlocked(code)
    return value


def _timestamp(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CMSBridgeBlocked(code)
    value = float(value)
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise CMSBridgeBlocked(code)
    return value


def _duration(value: Any, code: str, *, maximum: float = MAX_LEASE_SECONDS) -> float:
    value = _timestamp(value, code)
    if value <= 0 or value > maximum:
        raise CMSBridgeBlocked(code)
    return value


def _signature(value: Any, code: str = "cms.signature.invalid") -> CMSMessageSignature:
    if not isinstance(value, CMSMessageSignature):
        raise CMSBridgeBlocked(code)
    for part in (value.algorithm, value.key_id):
        if not isinstance(part, str) or TOKEN.fullmatch(part) is None:
            raise CMSBridgeBlocked(code)
    if not isinstance(value.signature, str) or SIGNATURE_VALUE.fullmatch(value.signature) is None:
        raise CMSBridgeBlocked(code)
    return value


def _verify(authority: Any, payload: bytes, signature: CMSMessageSignature, code: str) -> None:
    verify = getattr(authority, "verify", None)
    try:
        accepted = callable(verify) and verify(payload, signature) is True
    except Exception:
        accepted = False
    if not accepted:
        raise CMSBridgeBlocked(code)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise CMSBridgeBlocked("cms.transaction.external")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class WebsiteLocalizationCMSBridge:
    """Authenticated CMS change ingestion and complete-bundle delivery."""

    def __init__(self, connection: sqlite3.Connection, queue: Any, release_store: Any):
        if not isinstance(connection, sqlite3.Connection):
            raise CMSBridgeBlocked("cms.connection.invalid")
        if not isinstance(queue, _QUEUE.LocalizationQueue):
            raise CMSBridgeBlocked("cms.queue.invalid")
        if not isinstance(release_store, _RELEASE.LocalizationReleaseStore):
            raise CMSBridgeBlocked("cms.release_store.invalid")
        if release_store.queue is not queue:
            raise CMSBridgeBlocked("cms.queue_mismatch")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.queue = queue
        self.release_store = release_store
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise CMSBridgeBlocked("cms.schema.unsupported")
        if version == 0:
            self._create_schema()
        self._verify_schema()

    def _create_schema(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE cms_change_events (
                    event_id TEXT PRIMARY KEY,
                    event_sha256 TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    signature_algorithm TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('accepted', 'enqueued')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.connection.execute(f"""
                CREATE TABLE cms_publication_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    signature_algorithm TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'leased', 'retry_wait', 'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (
                        max_attempts BETWEEN 1 AND {MAX_ATTEMPTS}
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_error_detail_hash TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (event_id) REFERENCES cms_change_events (event_id)
                )
            """)
            self.connection.execute("""
                CREATE INDEX cms_publication_ready
                ON cms_publication_deliveries (status, next_attempt_at, created_at, delivery_id)
            """)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _verify_schema(self) -> None:
        event_columns = tuple(
            row["name"] for row in self.connection.execute("PRAGMA table_info(cms_change_events)")
        )
        delivery_columns = tuple(
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(cms_publication_deliveries)")
        )
        if event_columns != _EVENT_COLUMNS or delivery_columns != _DELIVERY_COLUMNS:
            raise CMSBridgeBlocked("cms.schema.altered")

    def _validated_event(self, event: Any) -> tuple[dict[str, Any], Any]:
        if not isinstance(event, dict):
            raise CMSBridgeBlocked("cms.event.invalid")
        expected = {"schema", "event_id", "site_id", "website_version", "localization"}
        if set(event) != expected or event.get("schema") != CHANGE_SCHEMA:
            raise CMSBridgeBlocked("cms.event.invalid")
        _token(event.get("event_id"), "cms.event_id.invalid")
        _token(event.get("site_id"), "cms.site_id.invalid")
        _token(event.get("website_version"), "cms.website_version.invalid")
        localization = event.get("localization")
        try:
            plan = _PLANNER.plan_from_mapping(localization)
        except _PLANNER.LocalizationPlanBlocked:
            raise CMSBridgeBlocked("cms.localization.invalid") from None
        return event, plan

    def ingest_change(
        self,
        event: Any,
        signature: CMSMessageSignature,
        verifier: CMSMessageAuthority,
        *,
        max_attempts: int = 3,
        now: float | int,
    ) -> IngestedChange:
        event, plan = self._validated_event(event)
        signature = _signature(signature)
        now = _timestamp(now, "cms.time.invalid")
        event_json = _canonical_json(event)
        event_hash = _hash(event_json)
        _verify(verifier, event_json.encode("utf-8"), signature, "cms.event.signature_rejected")
        event_id = event["event_id"]

        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT event_sha256, plan_id FROM cms_change_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                self.connection.execute("""
                    INSERT INTO cms_change_events VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """, (
                    event_id, event_hash, event_json, signature.algorithm, signature.key_id,
                    signature.signature, plan.plan_id, now, now,
                ))
            elif row["event_sha256"] != event_hash or row["plan_id"] != plan.plan_id:
                raise CMSBridgeBlocked("cms.event.idempotency_collision")

        try:
            inserted = self.queue.enqueue_plan(plan, max_attempts=max_attempts, now=now)
        except _QUEUE.LocalizationQueueBlocked:
            raise CMSBridgeBlocked("cms.queue.rejected") from None
        with _transaction(self.connection):
            updated = self.connection.execute("""
                UPDATE cms_change_events SET status = 'enqueued', updated_at = ?
                WHERE event_id = ? AND event_sha256 = ? AND plan_id = ?
            """, (now, event_id, event_hash, plan.plan_id))
            if updated.rowcount != 1:
                raise CMSBridgeBlocked("cms.event.identity_lost")
        return IngestedChange(event_id, plan.plan_id, len(plan.jobs), inserted, "enqueued")

    def _load_event(self, event_id: Any, verifier: CMSMessageAuthority) -> tuple[dict[str, Any], Any]:
        event_id = _token(event_id, "cms.event_id.invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_change_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None or row["status"] != "enqueued":
            raise CMSBridgeBlocked("cms.event.not_enqueued")
        if _hash(row["event_json"]) != row["event_sha256"]:
            raise CMSBridgeBlocked("cms.event.tampered")
        try:
            event = json.loads(row["event_json"])
        except json.JSONDecodeError:
            raise CMSBridgeBlocked("cms.event.tampered") from None
        if _canonical_json(event) != row["event_json"]:
            raise CMSBridgeBlocked("cms.event.tampered")
        signature = _signature(CMSMessageSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        _verify(verifier, row["event_json"].encode("utf-8"), signature, "cms.event.signature_rejected")
        event, plan = self._validated_event(event)
        if plan.plan_id != row["plan_id"]:
            raise CMSBridgeBlocked("cms.event.plan_mismatch")
        return event, plan

    def prepare_delivery(
        self,
        event_id: Any,
        event_verifier: CMSMessageAuthority,
        approval_authority: Any,
        publication_authority: CMSMessageAuthority,
        *,
        now: float | int,
        max_attempts: int = 5,
    ) -> CMSPublicationRequest:
        now = _timestamp(now, "cms.time.invalid")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise CMSBridgeBlocked("cms.max_attempts.invalid")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise CMSBridgeBlocked("cms.max_attempts.invalid")
        event, plan = self._load_event(event_id, event_verifier)
        try:
            bundle = self.release_store.publication_bundle(
                plan, approval_authority, now=now,
            )
        except _RELEASE.LocalizationReleaseBlocked:
            raise CMSBridgeBlocked("cms.website.not_ready") from None
        required = tuple(sorted(job.target.locale for job in plan.jobs))
        actual = tuple(item.target_locale for item in bundle)
        if actual != required:
            raise CMSBridgeBlocked("cms.bundle.incomplete")
        unsigned = {
            "schema": PUBLICATION_SCHEMA,
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "website_version": event["website_version"],
            "plan_id": plan.plan_id,
            "source_id": event["localization"]["source_id"],
            "source_revision": event["localization"]["source_revision"],
            "source_sha256": plan.source_hash,
            "localizations": [
                {
                    "locale": item.target_locale,
                    "target_text": item.candidate,
                    "target_sha256": item.target_sha256,
                    "approval_id": item.approval_id,
                    "approval_expires_at": item.expires_at,
                }
                for item in bundle
            ],
        }
        delivery_id = "blun-cms-delivery-" + _hash(_canonical_json(unsigned))
        payload = {**unsigned, "delivery_id": delivery_id}
        payload_json = _canonical_json(payload)
        payload_hash = _hash(payload_json)
        sign = getattr(publication_authority, "sign", None)
        try:
            signature = _signature(sign(payload_json.encode("utf-8"))) if callable(sign) else None
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.publication.signing_failed") from None
        if signature is None:
            raise CMSBridgeBlocked("cms.publication.authority_invalid")
        _verify(
            publication_authority,
            payload_json.encode("utf-8"),
            signature,
            "cms.publication.signature_rejected",
        )
        with _transaction(self.connection):
            prior = self.connection.execute(
                "SELECT delivery_id, payload_sha256 FROM cms_publication_deliveries WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if prior is not None:
                if prior["delivery_id"] != delivery_id or prior["payload_sha256"] != payload_hash:
                    raise CMSBridgeBlocked("cms.delivery.idempotency_collision")
            else:
                self.connection.execute("""
                    INSERT INTO cms_publication_deliveries (
                        delivery_id, event_id, plan_id, payload_json, payload_sha256,
                        signature_algorithm, key_id, signature, status, attempts,
                        max_attempts, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (
                    delivery_id, event["event_id"], plan.plan_id, payload_json, payload_hash,
                    signature.algorithm, signature.key_id, signature.signature,
                    max_attempts, now, now, now,
                ))
        return CMSPublicationRequest(delivery_id, payload, payload_hash, signature)

    def _request_from_row(
        self,
        row: sqlite3.Row,
        authority: CMSMessageAuthority,
        now: float,
    ) -> CMSPublicationRequest:
        if _hash(row["payload_json"]) != row["payload_sha256"]:
            raise CMSBridgeBlocked("cms.delivery.tampered")
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            raise CMSBridgeBlocked("cms.delivery.tampered") from None
        if _canonical_json(payload) != row["payload_json"] or payload.get("delivery_id") != row["delivery_id"]:
            raise CMSBridgeBlocked("cms.delivery.tampered")
        localizations = payload.get("localizations")
        if not isinstance(localizations, list) or not localizations:
            raise CMSBridgeBlocked("cms.delivery.tampered")
        expiries = [item.get("approval_expires_at") for item in localizations if isinstance(item, dict)]
        if len(expiries) != len(localizations):
            raise CMSBridgeBlocked("cms.delivery.tampered")
        if any(_timestamp(expiry, "cms.delivery.tampered") <= now for expiry in expiries):
            raise CMSBridgeBlocked("cms.delivery.approval_expired")
        signature = _signature(CMSMessageSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        _verify(
            authority,
            row["payload_json"].encode("utf-8"),
            signature,
            "cms.delivery.signature_invalid",
        )
        return CMSPublicationRequest(row["delivery_id"], payload, row["payload_sha256"], signature)

    def claim_delivery(
        self,
        worker_id: Any,
        authority: CMSMessageAuthority,
        *,
        now: float | int,
        lease_seconds: float | int = 300,
    ) -> ClaimedDelivery | None:
        worker_id = _token(worker_id, "cms.worker_id.invalid")
        now = _timestamp(now, "cms.time.invalid")
        lease_seconds = _duration(lease_seconds, "cms.lease.invalid")
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ? AND attempts >= max_attempts
            """, (now, now))
            self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = 'retry_wait', next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ? AND attempts < max_attempts
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_publication_deliveries
                WHERE status IN ('pending', 'retry_wait') AND next_attempt_at <= ?
                  AND attempts < max_attempts
                ORDER BY created_at, delivery_id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            request = self._request_from_row(row, authority, now)
            lease_token = secrets.token_urlsafe(32)
            expires = now + lease_seconds
            updated = self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = 'leased', attempts = attempts + 1, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?, last_error_code = NULL,
                    last_error_detail_hash = NULL, updated_at = ?
                WHERE delivery_id = ? AND status IN ('pending', 'retry_wait')
            """, (worker_id, lease_token, expires, now, row["delivery_id"]))
            if updated.rowcount != 1:
                raise CMSBridgeBlocked("cms.delivery.claim_lost")
            return ClaimedDelivery(
                request, int(row["attempts"]) + 1, int(row["max_attempts"]),
                worker_id, lease_token, expires,
            )

    def _live_delivery(self, claim: Any, now: float) -> sqlite3.Row:
        if not isinstance(claim, ClaimedDelivery):
            raise CMSBridgeBlocked("cms.delivery.claim_invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_publication_deliveries WHERE delivery_id = ?",
            (claim.request.delivery_id,),
        ).fetchone()
        if row is None or row["status"] != "leased":
            raise CMSBridgeBlocked("cms.delivery.lease_lost")
        if row["lease_owner"] != claim.lease_owner or row["lease_token"] != claim.lease_token:
            raise CMSBridgeBlocked("cms.delivery.lease_lost")
        if float(row["lease_expires_at"]) <= now:
            raise CMSBridgeBlocked("cms.delivery.lease_expired")
        return row

    def _finish(self, claim: ClaimedDelivery, *, now: float, error: CMSPublishFailed | None) -> DeliveryStatus:
        with _transaction(self.connection):
            row = self._live_delivery(claim, now)
            if error is None:
                status = "succeeded"
                next_attempt = now
                code = detail_hash = None
            else:
                terminal = not error.retryable or int(row["attempts"]) >= int(row["max_attempts"])
                status = "failed" if terminal else "retry_wait"
                next_attempt = now if terminal else now + min(3600.0, 5.0 * (2 ** (int(row["attempts"]) - 1)))
                code = error.code
                detail_hash = _hash(error.detail) if error.detail is not None else None
            updated = self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, last_error_detail_hash = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                status, next_attempt, code, detail_hash, now,
                claim.request.delivery_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise CMSBridgeBlocked("cms.delivery.finish_lost")
        return self.delivery_status(claim.request.delivery_id)

    def run_delivery(
        self,
        publisher: CMSPublisher,
        publication_authority: CMSMessageAuthority,
        *,
        worker_id: Any,
        clock: Callable[[], float] = time.time,
        lease_seconds: float | int = 300,
    ) -> DeliveryOutcome:
        claim = self.claim_delivery(
            worker_id, publication_authority, now=clock(), lease_seconds=lease_seconds,
        )
        if claim is None:
            return DeliveryOutcome("idle")
        publish = getattr(publisher, "publish", None)
        try:
            if not callable(publish):
                raise CMSPublishFailed("publisher.invalid", retryable=False)
            acknowledgement = publish(claim.request)
            expected = {
                "schema": ACK_SCHEMA,
                "delivery_id": claim.request.delivery_id,
                "payload_sha256": claim.request.payload_sha256,
                "status": "accepted",
            }
            if not isinstance(acknowledgement, Mapping) or dict(acknowledgement) != expected:
                raise CMSPublishFailed("publisher.ack_invalid", retryable=True)
        except CMSPublishFailed as error:
            status = self._finish(claim, now=_timestamp(clock(), "cms.time.invalid"), error=error)
            return DeliveryOutcome(status.status, status.delivery_id, status.attempts, error.code)
        except Exception:
            error = CMSPublishFailed("publisher.unavailable", retryable=True)
            status = self._finish(claim, now=_timestamp(clock(), "cms.time.invalid"), error=error)
            return DeliveryOutcome(status.status, status.delivery_id, status.attempts, error.code)
        status = self._finish(claim, now=_timestamp(clock(), "cms.time.invalid"), error=None)
        return DeliveryOutcome(status.status, status.delivery_id, status.attempts)

    def delivery_status(self, delivery_id: Any) -> DeliveryStatus:
        delivery_id = _token(delivery_id, "cms.delivery_id.invalid")
        row = self.connection.execute("""
            SELECT delivery_id, event_id, plan_id, status, attempts, max_attempts,
                   next_attempt_at, lease_expires_at, last_error_code,
                   last_error_detail_hash, payload_sha256
            FROM cms_publication_deliveries WHERE delivery_id = ?
        """, (delivery_id,)).fetchone()
        if row is None:
            raise CMSBridgeBlocked("cms.delivery.missing")
        return DeliveryStatus(**dict(row))
