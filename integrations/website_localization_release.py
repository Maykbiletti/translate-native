#!/usr/bin/env python3
"""Signed, version-bound translation memory and publication-readiness gate.

The host owns the SQLite connection, quality-receipt verifier, and isolated
approval authority. This module never reads a signing key and never publishes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol


SCHEMA_VERSION = 1
APPROVAL_SCHEMA = "blun.website-localization-approval.v1"
MAX_TEXT_BYTES = 2_000_000
MAX_RECEIPT_LENGTH = 16_384
MAX_TTL_SECONDS = 31_536_000.0
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_COLUMNS = (
    "approval_id", "job_id", "target_locale", "target_sha256",
    "result_json", "result_sha256", "approval_json", "approval_sha256",
    "signature_algorithm", "key_id", "signature", "approved_at", "expires_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required release dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_QUEUE = _load_module(
    "blun_website_localization_release_queue",
    _ROOT / "integrations" / "website_localization_queue.py",
)
_WORKER = _load_module(
    "blun_website_localization_release_worker",
    _ROOT / "integrations" / "website_localization_worker.py",
)


class LocalizationReleaseBlocked(RuntimeError):
    """Stable, content-free release failure."""

    def __init__(self, code: str):
        if not isinstance(code, str) or TOKEN.fullmatch(code) is None:
            raise ValueError("release failure code is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ApprovalSignature:
    algorithm: str
    key_id: str
    signature: str


class ApprovalAuthority(Protocol):
    def sign(self, payload: bytes) -> ApprovalSignature: ...
    def verify(self, payload: bytes, signature: ApprovalSignature) -> bool: ...


class QualityReceiptVerifier(Protocol):
    def verify(
        self,
        *,
        source_text: str,
        target_text: str,
        target_locale: str,
        receipt: str,
    ) -> bool: ...


@dataclass(frozen=True)
class ApprovedLocalization:
    approval_id: str
    job_id: str
    target_locale: str
    target_sha256: str
    candidate: str
    approved_at: float
    expires_at: float


@dataclass(frozen=True)
class WebsiteReadiness:
    plan_id: str
    ready: bool
    required_locales: tuple[str, ...]
    approved_locales: tuple[str, ...]
    blocked: tuple[tuple[str, str], ...]


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise LocalizationReleaseBlocked("release.json.invalid") from error


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: Any, code: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LocalizationReleaseBlocked(code)
    if len(value) > limit or "\x00" in value or not unicodedata.is_normalized("NFC", value):
        raise LocalizationReleaseBlocked(code)
    return value


def _timestamp(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalizationReleaseBlocked(code)
    value = float(value)
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise LocalizationReleaseBlocked(code)
    return value


def _receipt(value: Any, code: str) -> str:
    return _text(value, code, limit=MAX_RECEIPT_LENGTH)


def _signature(value: Any) -> ApprovalSignature:
    if not isinstance(value, ApprovalSignature):
        raise LocalizationReleaseBlocked("approval.signature.invalid")
    for item in (value.algorithm, value.key_id, value.signature):
        if not isinstance(item, str) or TOKEN.fullmatch(item) is None:
            raise LocalizationReleaseBlocked("approval.signature.invalid")
    return value


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise LocalizationReleaseBlocked("release.transaction.external")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _plan_job(plan: Any, job_id: str) -> tuple[str, dict[str, Any]]:
    plan_id = _text(getattr(plan, "plan_id", None), "plan.invalid")
    jobs = getattr(plan, "jobs", None)
    if not isinstance(jobs, tuple) or not jobs:
        raise LocalizationReleaseBlocked("plan.invalid")
    matches = [job for job in jobs if getattr(job, "job_id", None) == job_id]
    if len(matches) != 1 or not callable(getattr(matches[0], "as_payload", None)):
        raise LocalizationReleaseBlocked("plan.job.missing")
    payload = matches[0].as_payload()
    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        raise LocalizationReleaseBlocked("plan.job.invalid")
    try:
        payload = _WORKER._validated_job(payload)
    except _WORKER.LocalizationWorkerBlocked:
        raise LocalizationReleaseBlocked("plan.job.invalid") from None
    return plan_id, payload


def _validate_result(job: dict[str, Any], result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise LocalizationReleaseBlocked("result.invalid")
    expected = {
        "schema", "worker_schema", "job_id", "source_sha256", "target_sha256",
        "source_locale", "target_locale", "content_type", "glossary_version",
        "policy_version", "provider", "software_version", "candidate",
        "quality_passes", "integrity", "human_review_required", "release_required",
    }
    if set(result) != expected:
        raise LocalizationReleaseBlocked("result.invalid")
    candidate = result.get("candidate")
    if not isinstance(candidate, str) or not candidate or len(candidate.encode("utf-8")) > MAX_TEXT_BYTES:
        raise LocalizationReleaseBlocked("result.invalid")
    if "\x00" in candidate or not unicodedata.is_normalized("NFC", candidate):
        raise LocalizationReleaseBlocked("result.invalid")
    binding = {
        "job_id": job["job_id"],
        "source_sha256": job["source"]["sha256"],
        "source_locale": job["source"]["locale"],
        "target_locale": job["target"]["locale"],
        "content_type": job["content_type"],
        "glossary_version": job["glossary_version"],
        "policy_version": job["policy_version"],
        "provider": job["provider"],
        "software_version": job["software_version"],
    }
    if any(result.get(name) != value for name, value in binding.items()):
        raise LocalizationReleaseBlocked("result.binding_mismatch")
    if result.get("schema") != _WORKER.RESULT_SCHEMA or result.get("worker_schema") != _WORKER.WORKER_SCHEMA:
        raise LocalizationReleaseBlocked("result.schema.invalid")
    if result.get("target_sha256") != _hash_text(candidate):
        raise LocalizationReleaseBlocked("result.target_hash.invalid")
    phases = result.get("quality_passes")
    if not isinstance(phases, list) or [item.get("phase") for item in phases if isinstance(item, dict)] != list(_WORKER.PHASES):
        raise LocalizationReleaseBlocked("result.quality.invalid")
    for item in phases:
        if set(item) != {"phase", "request_sha256", "response_sha256", "status"}:
            raise LocalizationReleaseBlocked("result.quality.invalid")
        if item["status"] != "PASS" or HEX64.fullmatch(str(item["request_sha256"])) is None:
            raise LocalizationReleaseBlocked("result.quality.invalid")
        if HEX64.fullmatch(str(item["response_sha256"])) is None:
            raise LocalizationReleaseBlocked("result.quality.invalid")
    if result.get("integrity") != {"status": "PASS", "guard": "translate-native-structure-and-token-gate"}:
        raise LocalizationReleaseBlocked("result.integrity.invalid")
    if result.get("release_required") is not True:
        raise LocalizationReleaseBlocked("result.release.invalid")
    if result.get("human_review_required") is not (job["content_type"] == "legal"):
        raise LocalizationReleaseBlocked("result.human_review.invalid")
    return result


class LocalizationReleaseStore:
    """Append-only approvals and exact-version translation-memory lookups."""

    def __init__(self, connection: sqlite3.Connection, queue: Any):
        if not isinstance(connection, sqlite3.Connection):
            raise LocalizationReleaseBlocked("release.connection.invalid")
        if not isinstance(queue, _QUEUE.LocalizationQueue):
            raise LocalizationReleaseBlocked("release.queue.invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.queue = queue
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise LocalizationReleaseBlocked("release.schema.unsupported")
        if version == 0:
            with _transaction(connection):
                connection.execute("""
                    CREATE TABLE localization_approvals (
                        approval_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        target_locale TEXT NOT NULL,
                        target_sha256 TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        result_sha256 TEXT NOT NULL,
                        approval_json TEXT NOT NULL,
                        approval_sha256 TEXT NOT NULL,
                        signature_algorithm TEXT NOT NULL,
                        key_id TEXT NOT NULL,
                        signature TEXT NOT NULL,
                        approved_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE INDEX localization_approvals_job
                    ON localization_approvals (job_id, approved_at DESC)
                """)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        columns = tuple(row["name"] for row in connection.execute("PRAGMA table_info(localization_approvals)"))
        if columns != _COLUMNS:
            raise LocalizationReleaseBlocked("release.schema.altered")

    def approve(
        self,
        plan: Any,
        job_id: str,
        quality_receipt: str,
        quality_verifier: QualityReceiptVerifier,
        authority: ApprovalAuthority,
        *,
        now: float | int,
        ttl_seconds: float | int = 2_592_000,
        human_review_receipt: str | None = None,
        human_review_verifier: QualityReceiptVerifier | None = None,
    ) -> ApprovedLocalization:
        job_id = _text(job_id, "job.id.invalid")
        _, job = _plan_job(plan, job_id)
        result = _validate_result(job, self.queue.result(job_id))
        now = _timestamp(now, "approval.time.invalid")
        ttl = _timestamp(ttl_seconds, "approval.ttl.invalid")
        if ttl <= 0 or ttl > MAX_TTL_SECONDS:
            raise LocalizationReleaseBlocked("approval.ttl.invalid")
        quality_receipt = _receipt(quality_receipt, "quality.receipt.invalid")
        verifier = getattr(quality_verifier, "verify", None)
        if not callable(verifier):
            raise LocalizationReleaseBlocked("quality.verifier.invalid")
        try:
            quality_ok = verifier(
                source_text=job["source"]["text"],
                target_text=result["candidate"],
                target_locale=job["target"]["locale"],
                receipt=quality_receipt,
            ) is True
        except Exception:
            quality_ok = False
        if not quality_ok:
            raise LocalizationReleaseBlocked("quality.receipt.rejected")

        human_hash = None
        if result["human_review_required"]:
            human_review_receipt = _receipt(human_review_receipt, "human.receipt.required")
            human_verify = getattr(human_review_verifier, "verify", None)
            if not callable(human_verify):
                raise LocalizationReleaseBlocked("human.verifier.required")
            try:
                human_ok = human_verify(
                    source_text=job["source"]["text"],
                    target_text=result["candidate"],
                    target_locale=job["target"]["locale"],
                    receipt=human_review_receipt,
                ) is True
            except Exception:
                human_ok = False
            if not human_ok:
                raise LocalizationReleaseBlocked("human.receipt.rejected")
            human_hash = _hash_text(human_review_receipt)
        elif human_review_receipt is not None or human_review_verifier is not None:
            raise LocalizationReleaseBlocked("human.receipt.unexpected")

        result_json = _canonical_json(result)
        result_hash = _hash_text(result_json)
        immutable = {
            "schema": APPROVAL_SCHEMA,
            "job_id": job_id,
            "source_sha256": result["source_sha256"],
            "target_sha256": result["target_sha256"],
            "source_locale": result["source_locale"],
            "target_locale": result["target_locale"],
            "content_type": result["content_type"],
            "glossary_version": result["glossary_version"],
            "policy_version": result["policy_version"],
            "provider": result["provider"],
            "software_version": result["software_version"],
            "worker_schema": result["worker_schema"],
            "result_sha256": result_hash,
            "quality_receipt_sha256": _hash_text(quality_receipt),
            "human_review_receipt_sha256": human_hash,
        }
        approval_id = "blun-l10n-approval-" + _hash_text(_canonical_json(immutable))
        payload = {
            **immutable,
            "approval_id": approval_id,
            "approved_at": now,
            "expires_at": now + ttl,
        }
        approval_json = _canonical_json(payload)
        approval_bytes = approval_json.encode("utf-8")
        prior = self.connection.execute(
            "SELECT target_sha256 FROM localization_approvals WHERE job_id = ? LIMIT 1",
            (job_id,),
        ).fetchone()
        if prior is not None and prior["target_sha256"] != result["target_sha256"]:
            raise LocalizationReleaseBlocked("translation_memory.immutable")
        sign = getattr(authority, "sign", None)
        verify = getattr(authority, "verify", None)
        if not callable(sign) or not callable(verify):
            raise LocalizationReleaseBlocked("approval.authority.invalid")
        try:
            signed = _signature(sign(approval_bytes))
            verified = verify(approval_bytes, signed) is True
        except LocalizationReleaseBlocked:
            raise
        except Exception:
            raise LocalizationReleaseBlocked("approval.signing.failed") from None
        if not verified:
            raise LocalizationReleaseBlocked("approval.signature.rejected")

        with _transaction(self.connection):
            prior = self.connection.execute(
                "SELECT target_sha256 FROM localization_approvals WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
            if prior is not None and prior["target_sha256"] != result["target_sha256"]:
                raise LocalizationReleaseBlocked("translation_memory.immutable")
            existing = self.connection.execute(
                "SELECT * FROM localization_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if existing is None:
                self.connection.execute("""
                    INSERT INTO localization_approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    approval_id, job_id, result["target_locale"], result["target_sha256"],
                    result_json, result_hash, approval_json, _hash_text(approval_json),
                    signed.algorithm, signed.key_id, signed.signature, now, now + ttl,
                ))
            else:
                return self._approved_from_row(existing, plan, authority, now)
        return self.lookup(plan, job_id, authority, now=now)

    def _approved_from_row(
        self,
        row: sqlite3.Row,
        plan: Any,
        authority: ApprovalAuthority,
        now: float,
    ) -> ApprovedLocalization:
        _, job = _plan_job(plan, row["job_id"])
        if row["expires_at"] <= now:
            raise LocalizationReleaseBlocked("approval.expired")
        if _hash_text(row["result_json"]) != row["result_sha256"]:
            raise LocalizationReleaseBlocked("translation_memory.result_tampered")
        if _hash_text(row["approval_json"]) != row["approval_sha256"]:
            raise LocalizationReleaseBlocked("approval.payload.tampered")
        try:
            result = json.loads(row["result_json"])
            payload = json.loads(row["approval_json"])
        except json.JSONDecodeError:
            raise LocalizationReleaseBlocked("translation_memory.json.invalid") from None
        result = _validate_result(job, result)
        expected_keys = {
            "schema", "job_id", "source_sha256", "target_sha256", "source_locale",
            "target_locale", "content_type", "glossary_version", "policy_version",
            "provider", "software_version", "worker_schema", "result_sha256",
            "quality_receipt_sha256", "human_review_receipt_sha256", "approval_id",
            "approved_at", "expires_at",
        }
        if set(payload) != expected_keys:
            raise LocalizationReleaseBlocked("approval.binding_mismatch")
        binding = {
            "schema": APPROVAL_SCHEMA,
            "job_id": row["job_id"],
            "source_sha256": result["source_sha256"],
            "target_sha256": result["target_sha256"],
            "source_locale": result["source_locale"],
            "target_locale": result["target_locale"],
            "content_type": result["content_type"],
            "glossary_version": result["glossary_version"],
            "policy_version": result["policy_version"],
            "provider": result["provider"],
            "software_version": result["software_version"],
            "worker_schema": result["worker_schema"],
            "result_sha256": row["result_sha256"],
            "approved_at": row["approved_at"],
            "expires_at": row["expires_at"],
        }
        if any(payload.get(name) != value for name, value in binding.items()):
            raise LocalizationReleaseBlocked("approval.binding_mismatch")
        if payload.get("approval_id") != row["approval_id"]:
            raise LocalizationReleaseBlocked("approval.binding_mismatch")
        if HEX64.fullmatch(str(payload.get("quality_receipt_sha256"))) is None:
            raise LocalizationReleaseBlocked("approval.binding_mismatch")
        human_hash = payload.get("human_review_receipt_sha256")
        if (result["human_review_required"] and HEX64.fullmatch(str(human_hash)) is None) or (
            not result["human_review_required"] and human_hash is not None
        ):
            raise LocalizationReleaseBlocked("approval.binding_mismatch")
        immutable = {
            key: value
            for key, value in payload.items()
            if key not in {"approval_id", "approved_at", "expires_at"}
        }
        expected_id = "blun-l10n-approval-" + _hash_text(_canonical_json(immutable))
        if expected_id != row["approval_id"]:
            raise LocalizationReleaseBlocked("approval.binding_mismatch")
        if row["target_locale"] != result["target_locale"] or row["target_sha256"] != result["target_sha256"]:
            raise LocalizationReleaseBlocked("approval.binding_mismatch")
        signature = _signature(ApprovalSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        verify = getattr(authority, "verify", None)
        try:
            valid = callable(verify) and verify(row["approval_json"].encode("utf-8"), signature) is True
        except Exception:
            valid = False
        if not valid:
            raise LocalizationReleaseBlocked("approval.signature.invalid")
        return ApprovedLocalization(
            approval_id=row["approval_id"],
            job_id=row["job_id"],
            target_locale=row["target_locale"],
            target_sha256=row["target_sha256"],
            candidate=result["candidate"],
            approved_at=float(row["approved_at"]),
            expires_at=float(row["expires_at"]),
        )

    def lookup(
        self,
        plan: Any,
        job_id: str,
        authority: ApprovalAuthority,
        *,
        now: float | int,
    ) -> ApprovedLocalization:
        job_id = _text(job_id, "job.id.invalid")
        _plan_job(plan, job_id)
        now = _timestamp(now, "approval.time.invalid")
        row = self.connection.execute("""
            SELECT * FROM localization_approvals
            WHERE job_id = ? ORDER BY approved_at DESC, approval_id DESC LIMIT 1
        """, (job_id,)).fetchone()
        if row is None:
            raise LocalizationReleaseBlocked("approval.missing")
        return self._approved_from_row(row, plan, authority, now)

    def readiness(
        self,
        plan: Any,
        authority: ApprovalAuthority,
        *,
        now: float | int,
    ) -> WebsiteReadiness:
        plan_id = _text(getattr(plan, "plan_id", None), "plan.invalid")
        jobs = getattr(plan, "jobs", None)
        if not isinstance(jobs, tuple) or not jobs:
            raise LocalizationReleaseBlocked("plan.invalid")
        now = _timestamp(now, "approval.time.invalid")
        required = tuple(sorted(job.as_payload()["target"]["locale"] for job in jobs))
        approved: list[str] = []
        blocked: list[tuple[str, str]] = []
        for job in jobs:
            locale = job.as_payload()["target"]["locale"]
            try:
                self.lookup(plan, job.job_id, authority, now=now)
            except LocalizationReleaseBlocked as error:
                blocked.append((locale, error.code))
            else:
                approved.append(locale)
        return WebsiteReadiness(
            plan_id=plan_id,
            ready=not blocked and tuple(sorted(approved)) == required,
            required_locales=required,
            approved_locales=tuple(sorted(approved)),
            blocked=tuple(sorted(blocked)),
        )

    def publication_bundle(
        self,
        plan: Any,
        authority: ApprovalAuthority,
        *,
        now: float | int,
    ) -> tuple[ApprovedLocalization, ...]:
        status = self.readiness(plan, authority, now=now)
        if not status.ready:
            raise LocalizationReleaseBlocked("website.not_ready")
        return tuple(sorted(
            (
                self.lookup(plan, job.job_id, authority, now=now)
                for job in plan.jobs
            ),
            key=lambda item: item.target_locale,
        ))
