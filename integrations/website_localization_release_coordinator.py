#!/usr/bin/env python3
"""Idempotent bridge from completed locale results to signed CMS delivery."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


SCHEMA = "blun.website-localization-release-coordinator.v1"
EVIDENCE_REQUEST_SCHEMA = "blun.localization-quality-evidence-request.v1"
EVIDENCE_RESPONSE_SCHEMA = "blun.localization-quality-evidence-response.v1"
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
MAX_RECEIPT_LENGTH = 16_384


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
    human_review_required: bool

    def as_payload(self) -> dict[str, Any]:
        return json.loads(_canonical_json(asdict(self)))


class QualityEvidenceProvider(Protocol):
    def obtain(self, request: QualityEvidenceRequest) -> Mapping[str, Any]: ...


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
        "human_review_required": result["human_review_required"],
    }
    request_id = "blun-l10n-evidence-" + _hash(_canonical_json(binding))
    return QualityEvidenceRequest(
        **binding,
        request_id=request_id,
        source_text=job.as_payload()["source"]["text"],
        target_text=result["candidate"],
    )


def _obtain_evidence(provider: Any, request: QualityEvidenceRequest) -> tuple[str, str | None]:
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
    expected = {"schema", "request_id", "result_sha256", "quality_receipt", "human_review_receipt"}
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
    if request.human_review_required:
        human_receipt = _receipt(human_receipt, "evidence.human_receipt.required")
    elif human_receipt is not None:
        raise LocalizationReleaseCoordinatorBlocked("evidence.human_receipt.unexpected")
    return quality_receipt, human_receipt


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
    evidence_revision: str,
    now: float | int | None = None,
    approval_ttl_seconds: float | int = 2_592_000,
    delivery_max_attempts: int = 5,
    human_review_verifier: Any | None = None,
    clock: Callable[[], float] = time.time,
) -> ReleaseCoordinatorOutcome:
    """Approve at most one completed locale and prepare only a complete bundle."""

    event_id = _token(event_id, "coordinator.event_id.invalid")
    evidence_revision = _token(evidence_revision, "coordinator.evidence_revision.invalid")
    now = _timestamp(clock() if now is None else now)
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
    quality_receipt, human_receipt = _obtain_evidence(evidence_provider, request)
    try:
        approved = store.approve(
            plan,
            selected.job_id,
            quality_receipt,
            quality_verifier,
            approval_authority,
            now=now,
            ttl_seconds=approval_ttl_seconds,
            human_review_receipt=human_receipt,
            human_review_verifier=human_review_verifier if request.human_review_required else None,
        )
    except Exception as error:
        raise LocalizationReleaseCoordinatorBlocked(_external_code(error, "release")) from None

    try:
        readiness = store.readiness(plan, approval_authority, now=now)
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
            now=now,
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
