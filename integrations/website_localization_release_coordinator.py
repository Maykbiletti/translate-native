#!/usr/bin/env python3
"""Idempotent bridge from completed locale results to signed CMS delivery."""

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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


SCHEMA = "blun.website-localization-release-coordinator.v1"
EVIDENCE_REQUEST_SCHEMA = "blun.localization-quality-evidence-request.v4"
EVIDENCE_RESPONSE_SCHEMA = "blun.localization-quality-evidence-response.v2"
INDEPENDENT_MODEL_REVIEW_SCHEMA = "blun.independent-model-review.v1"
EVIDENCE_STATE_SCHEMA = "blun.localization-quality-evidence-state.v1"
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_RECEIPT_LENGTH = 16_384
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 3_600
EVIDENCE_STATES = {"pending", "leased", "retry_wait", "succeeded", "failed"}
_EVIDENCE_COLUMNS = (
    "request_id", "event_id", "plan_id", "job_id", "result_sha256",
    "evidence_revision", "status", "attempts", "max_attempts",
    "next_attempt_at", "lease_owner", "lease_token", "lease_expires_at",
    "last_error_code", "created_at", "updated_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load release coordinator dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CMS = _load_module(
    "blun_website_localization_release_coordinator_cms",
    _ROOT / "integrations" / "website_localization_cms.py",
)


class LocalizationReleaseCoordinatorBlocked(RuntimeError):
    """Stable failure that never contains source, target, receipt, or provider prose."""

    def __init__(self, code: str, *, retryable: bool = False):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("coordinator failure code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("coordinator retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class QualityEvidenceUnavailable(RuntimeError):
    """Adapter-declared, content-free evidence-provider failure."""

    localization_evidence_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("evidence failure code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("evidence retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class _ReceiptVerificationGuardUnavailable(RuntimeError):
    localization_receipt_verification_failure = True

    def __init__(self):
        super().__init__("operation_guard")
        self.code = "operation_guard"
        self.retryable = True


class _GuardedReceiptVerifier:
    def __init__(self, verifier: Any, guard: Callable[[float], Any], lease_seconds: float):
        self.verifier = verifier
        self.guard = guard
        self.lease_seconds = lease_seconds

    def verify(self, **values):
        try:
            self.guard(self.lease_seconds)
        except Exception:
            raise _ReceiptVerificationGuardUnavailable() from None
        return self.verifier.verify(**values)


def _guarded_verifier(
    verifier: Any | None,
    operation_guard: Callable[[float], Any] | None,
    lease_seconds: float,
) -> Any | None:
    if verifier is None or operation_guard is None:
        return verifier
    return _GuardedReceiptVerifier(verifier, operation_guard, lease_seconds)


@dataclass(frozen=True)
class QualityEvidenceRequest:
    schema: str
    request_id: str
    evidence_revision: str
    event_id: str
    plan_id: str
    job_id: str
    result_sha256: str
    source_sha256: str
    target_sha256: str
    source_locale: str
    target_locale: str
    content_type: str
    glossary_version: str
    policy_version: str
    provider: dict[str, Any]
    software_version: str
    source_text: str
    target_text: str
    review_confidence: dict[str, str]
    quality_profile: dict[str, str]
    commercial_profile: str | None
    human_review_required: bool
    independent_review_required: bool

    def as_payload(self) -> dict[str, Any]:
        return json.loads(_canonical_json(asdict(self)))


class QualityEvidenceProvider(Protocol):
    def obtain(self, request: QualityEvidenceRequest) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class QualityEvidenceClaim:
    request_id: str
    lease_owner: str
    lease_token: str
    lease_expires_at: float
    attempt: int
    max_attempts: int


@dataclass(frozen=True)
class QualityEvidenceStatus:
    schema: str
    request_id: str
    event_id: str
    plan_id: str
    job_id: str
    result_sha256: str
    evidence_revision: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    last_error_code: str | None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReleaseCoordinatorOutcome:
    schema: str
    status: str
    event_id: str
    plan_id: str
    job_id: str | None = None
    target_locale: str | None = None
    approval_id: str | None = None
    delivery_id: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise LocalizationReleaseCoordinatorBlocked("coordinator.json.invalid") from None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token(value: Any, code: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise LocalizationReleaseCoordinatorBlocked(code)
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalizationReleaseCoordinatorBlocked("coordinator.time.invalid")
    value = float(value)
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise LocalizationReleaseCoordinatorBlocked("coordinator.time.invalid")
    return value


def _positive_duration(value: Any, code: str, maximum: float) -> float:
    value = _timestamp(value)
    if value <= 0 or value > maximum:
        raise LocalizationReleaseCoordinatorBlocked(code)
    return value


def _attempt_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ATTEMPTS:
        raise LocalizationReleaseCoordinatorBlocked("evidence.max_attempts.invalid")
    return value


def _stored_integer(value: Any, code: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LocalizationReleaseCoordinatorBlocked(code)
    return value


@contextmanager
def _transaction(connection: sqlite3.Connection):
    if connection.in_transaction:
        raise LocalizationReleaseCoordinatorBlocked("evidence.state.transaction_active")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        connection.rollback()
        raise


class QualityEvidenceStateStore:
    """Durable, content-free leases and bounded attempts for quality evidence."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise LocalizationReleaseCoordinatorBlocked("evidence.state.connection_invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS localization_quality_evidence_state (
                    request_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL,
                    evidence_revision TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'leased', 'retry_wait', 'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (event_id, plan_id, job_id, result_sha256, evidence_revision)
                )
            """)
            self.connection.execute("""
                CREATE INDEX IF NOT EXISTS localization_quality_evidence_due
                ON localization_quality_evidence_state (
                    event_id, status, next_attempt_at, created_at, request_id
                )
            """)
        columns = tuple(
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(localization_quality_evidence_state)"
            )
        )
        if columns != _EVIDENCE_COLUMNS:
            raise LocalizationReleaseCoordinatorBlocked("evidence.state.schema_altered")

    @staticmethod
    def _binding(request: QualityEvidenceRequest) -> tuple[str, ...]:
        if not isinstance(request, QualityEvidenceRequest):
            raise LocalizationReleaseCoordinatorBlocked("evidence.request.invalid")
        values = (
            _token(request.request_id, "evidence.request_id.invalid"),
            _token(request.event_id, "coordinator.event_id.invalid"),
            _token(request.plan_id, "evidence.plan_id.invalid"),
            _token(request.job_id, "evidence.job_id.invalid"),
            request.result_sha256,
            _token(request.evidence_revision, "coordinator.evidence_revision.invalid"),
        )
        if not isinstance(values[4], str) or SHA256.fullmatch(values[4]) is None:
            raise LocalizationReleaseCoordinatorBlocked("evidence.result_sha256.invalid")
        return values

    @staticmethod
    def _claim_values(claim: Any) -> QualityEvidenceClaim:
        if not isinstance(claim, QualityEvidenceClaim):
            raise LocalizationReleaseCoordinatorBlocked("evidence.claim.invalid")
        _token(claim.request_id, "evidence.request_id.invalid")
        _token(claim.lease_owner, "evidence.worker_id.invalid")
        _token(claim.lease_token, "evidence.lease_token.invalid")
        return claim

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> QualityEvidenceStatus:
        request_id = _token(row["request_id"], "evidence.state.invalid")
        event_id = _token(row["event_id"], "evidence.state.invalid")
        plan_id = _token(row["plan_id"], "evidence.state.invalid")
        job_id = _token(row["job_id"], "evidence.state.invalid")
        result_sha256 = row["result_sha256"]
        if not isinstance(result_sha256, str) or SHA256.fullmatch(result_sha256) is None:
            raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
        evidence_revision = _token(row["evidence_revision"], "evidence.state.invalid")
        status = row["status"]
        if status not in EVIDENCE_STATES:
            raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
        max_attempts = _stored_integer(
            row["max_attempts"], "evidence.state.invalid", minimum=1, maximum=MAX_ATTEMPTS,
        )
        attempts = _stored_integer(
            row["attempts"], "evidence.state.invalid", minimum=0, maximum=max_attempts,
        )
        next_attempt_at = _timestamp(row["next_attempt_at"])
        lease_values = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        if status == "leased":
            lease_owner = _token(lease_values[0], "evidence.state.invalid")
            lease_token = _token(lease_values[1], "evidence.state.invalid")
            lease_expires_at = _timestamp(lease_values[2])
            if lease_expires_at <= next_attempt_at:
                raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
            del lease_owner, lease_token
        elif lease_values != (None, None, None):
            raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
        else:
            lease_expires_at = None
        error_code = row["last_error_code"]
        if error_code is not None and (
            not isinstance(error_code, str) or ERROR_CODE.fullmatch(error_code) is None
        ):
            raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
        if status in {"pending", "leased", "succeeded"} and error_code is not None:
            raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
        _timestamp(row["created_at"])
        _timestamp(row["updated_at"])
        return QualityEvidenceStatus(
            EVIDENCE_STATE_SCHEMA,
            request_id,
            event_id,
            plan_id,
            job_id,
            result_sha256,
            evidence_revision,
            status,
            attempts,
            max_attempts,
            next_attempt_at,
            lease_expires_at,
            error_code,
        )

    def claim(
        self,
        request: QualityEvidenceRequest,
        *,
        worker_id: Any,
        now: float | int,
        lease_seconds: float | int,
        max_attempts: int,
    ) -> QualityEvidenceClaim | None:
        binding = self._binding(request)
        worker_id = _token(worker_id, "evidence.worker_id.invalid")
        now = _timestamp(now)
        lease_seconds = _positive_duration(
            lease_seconds, "evidence.lease.invalid", MAX_LEASE_SECONDS,
        )
        max_attempts = _attempt_limit(max_attempts)
        blocked_code = None
        claimed = None
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM localization_quality_evidence_state WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is None:
                self.connection.execute("""
                    INSERT INTO localization_quality_evidence_state (
                        request_id, event_id, plan_id, job_id, result_sha256,
                        evidence_revision, status, attempts, max_attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (*binding, max_attempts, now, now, now))
                row = self.connection.execute(
                    "SELECT * FROM localization_quality_evidence_state WHERE request_id = ?",
                    (request.request_id,),
                ).fetchone()
            self._status_from_row(row)
            actual = tuple(row[name] for name in _EVIDENCE_COLUMNS[:6])
            if actual != binding:
                raise LocalizationReleaseCoordinatorBlocked("evidence.state.binding_mismatch")
            if row["max_attempts"] != max_attempts:
                raise LocalizationReleaseCoordinatorBlocked("evidence.state.attempt_policy_changed")
            status = row["status"]
            if status not in EVIDENCE_STATES:
                raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
            if status == "leased" and _timestamp(row["lease_expires_at"]) <= now:
                terminal = row["attempts"] >= row["max_attempts"]
                status = "failed" if terminal else "retry_wait"
                self.connection.execute("""
                    UPDATE localization_quality_evidence_state
                    SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_code = 'lease_expired', updated_at = ?
                    WHERE request_id = ? AND status = 'leased'
                """, (status, now, now, request.request_id))
            if status == "succeeded":
                blocked_code = "evidence.revision.already_consumed"
            elif status == "failed":
                blocked_code = "evidence.attempts_exhausted"
            else:
                row = self.connection.execute(
                    "SELECT * FROM localization_quality_evidence_state WHERE request_id = ?",
                    (request.request_id,),
                ).fetchone()
                if row["status"] != "leased" and _timestamp(row["next_attempt_at"]) <= now:
                    lease_token = secrets.token_urlsafe(32)
                    expires_at = now + lease_seconds
                    updated = self.connection.execute("""
                        UPDATE localization_quality_evidence_state
                        SET status = 'leased', attempts = attempts + 1,
                            lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                            last_error_code = NULL, updated_at = ?
                        WHERE request_id = ? AND status IN ('pending', 'retry_wait')
                          AND next_attempt_at <= ? AND attempts < max_attempts
                    """, (
                        worker_id, lease_token, expires_at, now, request.request_id, now,
                    ))
                    if updated.rowcount != 1:
                        raise LocalizationReleaseCoordinatorBlocked(
                            "evidence.claim.lost", retryable=True,
                        )
                    claimed = QualityEvidenceClaim(
                        request.request_id,
                        worker_id,
                        lease_token,
                        expires_at,
                        row["attempts"] + 1,
                        row["max_attempts"],
                    )
        if blocked_code is not None:
            raise LocalizationReleaseCoordinatorBlocked(blocked_code)
        return claimed

    def _live_row(self, claim: Any, now: float) -> sqlite3.Row:
        claim = self._claim_values(claim)
        row = self.connection.execute(
            "SELECT * FROM localization_quality_evidence_state WHERE request_id = ?",
            (claim.request_id,),
        ).fetchone()
        if (
            row is None or row["status"] != "leased"
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
        ):
            raise LocalizationReleaseCoordinatorBlocked("evidence.lease_lost")
        if _timestamp(row["lease_expires_at"]) <= now:
            raise LocalizationReleaseCoordinatorBlocked("evidence.lease_expired")
        return row

    def succeed(self, claim: QualityEvidenceClaim, *, now: float | int) -> None:
        now = _timestamp(now)
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM localization_quality_evidence_state WHERE request_id = ?",
                (self._claim_values(claim).request_id,),
            ).fetchone()
            if row is not None and row["status"] == "succeeded":
                self._status_from_row(row)
                return
            self._live_row(claim, now)
            updated = self.connection.execute("""
                UPDATE localization_quality_evidence_state
                SET status = 'succeeded', next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL, updated_at = ?
                WHERE request_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                now, now, claim.request_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LocalizationReleaseCoordinatorBlocked("evidence.finish_lost")

    def fail(
        self,
        claim: QualityEvidenceClaim,
        error: LocalizationReleaseCoordinatorBlocked,
        *,
        now: float | int,
    ) -> None:
        if not isinstance(error, LocalizationReleaseCoordinatorBlocked):
            raise LocalizationReleaseCoordinatorBlocked("evidence.failure.invalid")
        now = _timestamp(now)
        with _transaction(self.connection):
            current = self.connection.execute(
                "SELECT * FROM localization_quality_evidence_state WHERE request_id = ?",
                (self._claim_values(claim).request_id,),
            ).fetchone()
            if current is not None and current["status"] == "succeeded":
                self._status_from_row(current)
                return
            row = self._live_row(claim, now)
            terminal = not error.retryable or row["attempts"] >= row["max_attempts"]
            status = "failed" if terminal else "retry_wait"
            next_attempt = now if terminal else now + min(
                3_600.0, 5.0 * (2 ** (row["attempts"] - 1)),
            )
            updated = self.connection.execute("""
                UPDATE localization_quality_evidence_state
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, updated_at = ?
                WHERE request_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                status, next_attempt, error.code, now,
                claim.request_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise LocalizationReleaseCoordinatorBlocked("evidence.finish_lost")

    def mark_approved(
        self,
        event_id: Any,
        plan_id: Any,
        job_id: Any,
        result_sha256: Any,
        *,
        now: float | int,
    ) -> None:
        event_id = _token(event_id, "coordinator.event_id.invalid")
        plan_id = _token(plan_id, "evidence.plan_id.invalid")
        job_id = _token(job_id, "evidence.job_id.invalid")
        if not isinstance(result_sha256, str) or SHA256.fullmatch(result_sha256) is None:
            raise LocalizationReleaseCoordinatorBlocked("evidence.result_sha256.invalid")
        now = _timestamp(now)
        with _transaction(self.connection):
            rows = self.connection.execute("""
                SELECT * FROM localization_quality_evidence_state
                WHERE event_id = ? AND plan_id = ? AND job_id = ?
            """, (event_id, plan_id, job_id)).fetchall()
            for row in rows:
                self._status_from_row(row)
                if row["result_sha256"] != result_sha256:
                    raise LocalizationReleaseCoordinatorBlocked(
                        "evidence.state.binding_mismatch"
                    )
            self.connection.execute("""
                UPDATE localization_quality_evidence_state
                SET status = 'succeeded', next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL, updated_at = ?
                WHERE event_id = ? AND plan_id = ? AND job_id = ?
                  AND result_sha256 = ?
                  AND status != 'succeeded'
            """, (now, now, event_id, plan_id, job_id, result_sha256))

    def statuses(self, event_id: Any) -> tuple[QualityEvidenceStatus, ...]:
        event_id = _token(event_id, "coordinator.event_id.invalid")
        rows = self.connection.execute("""
            SELECT * FROM localization_quality_evidence_state
            WHERE event_id = ? ORDER BY created_at, request_id
        """, (event_id,)).fetchall()
        return tuple(self._status_from_row(row) for row in rows)


def _receipt(value: Any, code: str) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > MAX_RECEIPT_LENGTH or "\x00" in value
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise LocalizationReleaseCoordinatorBlocked(code)
    return value


def _external_code(error: Exception, fallback: str) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and ERROR_CODE.fullmatch(code) is not None:
        combined = code if code.startswith(fallback + ".") else fallback + "." + code
        if len(combined) <= 128:
            return combined
    return fallback + ".failed"


def _load_event(bridge: Any, event_id: str, event_verifier: Any):
    loader = getattr(bridge, "_load_event", None)
    if not callable(loader):
        raise LocalizationReleaseCoordinatorBlocked("coordinator.bridge.invalid")
    try:
        event, plan = loader(event_id, event_verifier)
    except Exception as error:
        raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "cms")) from None
    if not isinstance(event, dict) or not isinstance(getattr(plan, "jobs", None), tuple):
        raise LocalizationReleaseCoordinatorBlocked("coordinator.bridge.invalid")
    return event, plan


def _validated_result(store: Any, plan: Any, job: Any) -> tuple[dict[str, Any], str]:
    validated_result = getattr(store, "validated_result", None)
    if not callable(validated_result):
        raise LocalizationReleaseCoordinatorBlocked("coordinator.release_store.invalid")
    try:
        result = validated_result(plan, job.job_id)
    except Exception as error:
        raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "result")) from None
    result_json = _canonical_json(result)
    return result, _hash(result_json)


def _request(
    event: dict[str, Any],
    plan: Any,
    job: Any,
    result: dict[str, Any],
    result_sha256: str,
    evidence_revision: str,
) -> QualityEvidenceRequest:
    binding = {
        "schema": EVIDENCE_REQUEST_SCHEMA,
        "evidence_revision": evidence_revision,
        "event_id": event["event_id"],
        "plan_id": plan.plan_id,
        "job_id": job.job_id,
        "result_sha256": result_sha256,
        "source_sha256": result["source_sha256"],
        "target_sha256": result["target_sha256"],
        "source_locale": result["source_locale"],
        "target_locale": result["target_locale"],
        "content_type": result["content_type"],
        "glossary_version": result["glossary_version"],
        "policy_version": result["policy_version"],
        "provider": json.loads(_canonical_json(result["provider"])),
        "software_version": result["software_version"],
        "review_confidence": json.loads(_canonical_json(result["review_confidence"])),
        "quality_profile": json.loads(_canonical_json(result["quality_profile"])),
        "commercial_profile": job.as_payload().get("commercial_profile"),
        "human_review_required": result["human_review_required"],
        "independent_review_required": result["independent_review_required"],
    }
    request_id = "blun-l10n-evidence-" + _hash(_canonical_json(binding))
    return QualityEvidenceRequest(
        **binding,
        request_id=request_id,
        source_text=job.as_payload()["source"]["text"],
        target_text=result["candidate"],
    )


def _obtain_evidence(
    provider: Any,
    request: QualityEvidenceRequest,
) -> tuple[str, str | None, dict[str, Any] | None]:
    obtain = getattr(provider, "obtain", None)
    if not callable(obtain):
        raise LocalizationReleaseCoordinatorBlocked("evidence.provider.invalid")
    request_hash = _hash(_canonical_json(request.as_payload()))
    try:
        response = obtain(request)
    except Exception as error:
        if getattr(type(error), "localization_evidence_failure", None) is True:
            retryable = getattr(error, "retryable", None)
            if isinstance(retryable, bool):
                raise LocalizationReleaseCoordinatorBlocked(
                    _external_code(error, "evidence"), retryable=retryable,
                ) from None
        raise LocalizationReleaseCoordinatorBlocked("evidence.unavailable", retryable=True) from None
    if _hash(_canonical_json(request.as_payload())) != request_hash:
        raise LocalizationReleaseCoordinatorBlocked("evidence.request_mutated")
    expected = {
        "schema", "request_id", "result_sha256", "quality_receipt",
        "human_review_receipt", "independent_model_review",
    }
    try:
        if not isinstance(response, Mapping) or set(response) != expected:
            raise ValueError
        response = dict(response)
    except Exception:
        raise LocalizationReleaseCoordinatorBlocked("evidence.response.invalid") from None
    if (
        response["schema"] != EVIDENCE_RESPONSE_SCHEMA
        or response["request_id"] != request.request_id
        or response["result_sha256"] != request.result_sha256
    ):
        raise LocalizationReleaseCoordinatorBlocked("evidence.response.binding_mismatch")
    quality_receipt = _receipt(response["quality_receipt"], "evidence.quality_receipt.invalid")
    human_receipt = response["human_review_receipt"]
    independent = response["independent_model_review"]
    if not (request.human_review_required or request.independent_review_required):
        if human_receipt is not None or independent is not None:
            raise LocalizationReleaseCoordinatorBlocked("evidence.review_escalation.unexpected")
        return quality_receipt, None, None
    if request.human_review_required:
        if independent is not None:
            raise LocalizationReleaseCoordinatorBlocked("evidence.independent_model_review.legal_forbidden")
        human_receipt = _receipt(human_receipt, "evidence.human_receipt.required")
        return quality_receipt, human_receipt, None
    if (human_receipt is None) == (independent is None):
        raise LocalizationReleaseCoordinatorBlocked("evidence.review_escalation.required")
    if human_receipt is not None:
        return quality_receipt, _receipt(
            human_receipt, "evidence.human_receipt.required",
        ), None
    if not isinstance(independent, Mapping) or set(independent) != {"schema", "provider", "receipt"}:
        raise LocalizationReleaseCoordinatorBlocked("evidence.independent_model_review.invalid")
    independent = dict(independent)
    if independent["schema"] != INDEPENDENT_MODEL_REVIEW_SCHEMA:
        raise LocalizationReleaseCoordinatorBlocked("evidence.independent_model_review.invalid")
    reviewer = independent["provider"]
    if not isinstance(reviewer, Mapping) or set(reviewer) != {"id", "model_id", "model_version"}:
        raise LocalizationReleaseCoordinatorBlocked("evidence.independent_model_review.provider.invalid")
    reviewer = {
        name: _token(reviewer[name], "evidence.independent_model_review.provider.invalid")
        for name in ("id", "model_id", "model_version")
    }
    if reviewer["id"] == request.provider["id"]:
        raise LocalizationReleaseCoordinatorBlocked("evidence.independent_model_review.not_independent")
    return quality_receipt, None, {
        "schema": INDEPENDENT_MODEL_REVIEW_SCHEMA,
        "provider": reviewer,
        "receipt": _receipt(
            independent["receipt"], "evidence.independent_model_review.receipt.required",
        ),
    }


def _outcome(status: str, event: dict[str, Any], plan: Any, **values) -> ReleaseCoordinatorOutcome:
    return ReleaseCoordinatorOutcome(
        schema=SCHEMA,
        status=status,
        event_id=event["event_id"],
        plan_id=plan.plan_id,
        **values,
    )


def _existing_delivery(bridge: Any, event_id: str, authority: Any, now: float):
    connection = getattr(bridge, "connection", None)
    request_from_row = getattr(bridge, "_request_from_row", None)
    if connection is None or not callable(request_from_row):
        raise LocalizationReleaseCoordinatorBlocked("coordinator.bridge.invalid")
    try:
        row = connection.execute(
            "SELECT * FROM cms_publication_deliveries WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        status = row["status"]
        if status not in {"pending", "leased", "retry_wait", "succeeded", "failed"}:
            raise LocalizationReleaseCoordinatorBlocked("cms.delivery.invalid")
        # A completed delivery is immutable history. A delivery that has not
        # succeeded must still hold current approvals before being called ready.
        request = request_from_row(row, authority, 0.0 if status == "succeeded" else now)
    except Exception as error:
        if isinstance(error, LocalizationReleaseCoordinatorBlocked):
            raise
        raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "cms")) from None
    return request, status


def run_next_release(
    bridge: Any,
    event_id: str,
    event_verifier: Any,
    evidence_provider: QualityEvidenceProvider,
    quality_verifier: Any,
    approval_authority: Any,
    publication_authority: Any,
    *,
    evidence_state: QualityEvidenceStateStore,
    evidence_revision: str,
    evidence_worker_id: str,
    evidence_lease_seconds: float | int = 300,
    evidence_max_attempts: int = 5,
    now: float | int | None = None,
    approval_ttl_seconds: float | int = 2_592_000,
    delivery_max_attempts: int = 5,
    human_review_verifier: Any | None = None,
    independent_model_review_verifier: Any | None = None,
    operation_guard: Callable[[float], Any] | None = None,
    clock: Callable[[], float] = time.time,
) -> ReleaseCoordinatorOutcome:
    """Approve at most one completed locale and prepare only a complete bundle."""

    event_id = _token(event_id, "coordinator.event_id.invalid")
    evidence_revision = _token(evidence_revision, "coordinator.evidence_revision.invalid")
    if not isinstance(evidence_state, QualityEvidenceStateStore):
        raise LocalizationReleaseCoordinatorBlocked("evidence.state.invalid")
    if operation_guard is not None and not callable(operation_guard):
        raise LocalizationReleaseCoordinatorBlocked("evidence.operation_guard.invalid")
    fixed_now = now is not None
    now = _timestamp(clock() if now is None else now)

    def current_time() -> float:
        return now if fixed_now else _timestamp(clock())

    event, plan = _load_event(bridge, event_id, event_verifier)
    queue = getattr(bridge, "queue", None)
    store = getattr(bridge, "release_store", None)
    if queue is None or store is None:
        raise LocalizationReleaseCoordinatorBlocked("coordinator.bridge.invalid")
    existing_delivery = _existing_delivery(bridge, event_id, publication_authority, now)
    if existing_delivery is not None:
        delivery, delivery_status = existing_delivery
        if delivery_status == "failed":
            raise LocalizationReleaseCoordinatorBlocked("cms.delivery.failed")
        return _outcome(
            "delivered" if delivery_status == "succeeded" else "delivery_ready",
            event,
            plan,
            delivery_id=delivery.delivery_id,
        )

    selected = None
    for job in sorted(plan.jobs, key=lambda item: item.as_payload()["target"]["locale"]):
        try:
            status = queue.status(job.job_id)
        except Exception as error:
            raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "queue")) from None
        if status.status != "succeeded":
            continue
        try:
            store.lookup(plan, job.job_id, approval_authority, now=now)
        except Exception as error:
            code = getattr(error, "code", None)
            if code not in {"approval.missing", "approval.expired"}:
                raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "approval")) from None
            if selected is None:
                selected = job
        else:
            _, approved_result_sha256 = _validated_result(store, plan, job)
            evidence_state.mark_approved(
                event["event_id"],
                plan.plan_id,
                job.job_id,
                approved_result_sha256,
                now=now,
            )

    if selected is None:
        try:
            readiness = store.readiness(plan, approval_authority, now=now)
        except Exception as error:
            raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "approval")) from None
        if not readiness.ready:
            return _outcome("waiting", event, plan)
        try:
            delivery = bridge.prepare_delivery(
                event_id,
                event_verifier,
                approval_authority,
                publication_authority,
                now=now,
                max_attempts=delivery_max_attempts,
            )
        except Exception as error:
            raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "cms")) from None
        return _outcome("delivery_ready", event, plan, delivery_id=delivery.delivery_id)

    result, result_sha256 = _validated_result(store, plan, selected)
    request = _request(event, plan, selected, result, result_sha256, evidence_revision)
    claim = evidence_state.claim(
        request,
        worker_id=evidence_worker_id,
        now=now,
        lease_seconds=evidence_lease_seconds,
        max_attempts=evidence_max_attempts,
    )
    if claim is None:
        return _outcome(
            "waiting",
            event,
            plan,
            job_id=selected.job_id,
            target_locale=request.target_locale,
        )
    if operation_guard is not None:
        try:
            operation_guard(float(evidence_lease_seconds))
        except Exception:
            raise LocalizationReleaseCoordinatorBlocked(
                "evidence.operation_guard.failed",
            ) from None
    try:
        quality_receipt, human_receipt, independent_review = _obtain_evidence(
            evidence_provider, request,
        )
    except LocalizationReleaseCoordinatorBlocked as error:
        evidence_state.fail(claim, error, now=current_time())
        raise
    approval_now = current_time()
    guarded_quality_verifier = _guarded_verifier(
        quality_verifier, operation_guard, float(evidence_lease_seconds),
    )
    guarded_human_verifier = _guarded_verifier(
        human_review_verifier, operation_guard, float(evidence_lease_seconds),
    )
    guarded_independent_verifier = _guarded_verifier(
        independent_model_review_verifier,
        operation_guard,
        float(evidence_lease_seconds),
    )
    try:
        approved = store.approve(
            plan,
            selected.job_id,
            quality_receipt,
            guarded_quality_verifier,
            approval_authority,
            now=approval_now,
            ttl_seconds=approval_ttl_seconds,
            human_review_receipt=human_receipt,
            human_review_verifier=(
                guarded_human_verifier if human_receipt is not None else None
            ),
            independent_model_review=independent_review,
            independent_model_review_verifier=(
                guarded_independent_verifier if independent_review is not None else None
            ),
        )
    except Exception as error:
        code = _external_code(error, "release")
        blocked = LocalizationReleaseCoordinatorBlocked(
            code,
            retryable=(
                code == "release.approval.signing.failed"
                or getattr(error, "retryable", False) is True
            ),
        )
        evidence_state.fail(claim, blocked, now=current_time())
        raise blocked from None
    evidence_state.succeed(claim, now=current_time())

    try:
        readiness = store.readiness(plan, approval_authority, now=approval_now)
    except Exception as error:
        raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "approval")) from None
    if not readiness.ready:
        return _outcome(
            "approved",
            event,
            plan,
            job_id=selected.job_id,
            target_locale=request.target_locale,
            approval_id=approved.approval_id,
        )
    try:
        delivery = bridge.prepare_delivery(
            event_id,
            event_verifier,
            approval_authority,
            publication_authority,
            now=current_time(),
            max_attempts=delivery_max_attempts,
        )
    except Exception as error:
        raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "cms")) from None
    return _outcome(
        "delivery_ready",
        event,
        plan,
        job_id=selected.job_id,
        target_locale=request.target_locale,
        approval_id=approved.approval_id,
        delivery_id=delivery.delivery_id,
    )
