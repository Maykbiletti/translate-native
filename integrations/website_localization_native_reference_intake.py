#!/usr/bin/env python3
"""Bound intake contract for qualified-native benchmark references.

The host can export a target-free work order to an independent editorial
workflow, obtain a receipt over the exact completed verification request, and
accept the result into the durable reference store.  No translation is
generated locally and no unverified target is persisted.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


WORK_ORDER_SCHEMA = "blun.website-localization-native-reference-work-order.v1"
SUBMISSION_SCHEMA = "blun.website-localization-native-reference-submission.v1"
MAX_ENVELOPE_BYTES = 4 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native-reference intake dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_STORE = _load_module(
    "blun_website_localization_native_reference_intake_store",
    _ROOT / "integrations" / "website_localization_native_reference_store.py",
)
_BENCHMARK = _STORE._BENCHMARK
_SUITE = _STORE._SUITE


class NativeReferenceIntakeFailed(RuntimeError):
    """Content-free intake failure safe for benchmark orchestration."""

    benchmark_campaign_dependency_failure = True

    def __init__(self, code: str, *, retryable: bool = False):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("native-reference intake error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("native-reference intake retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.envelope_invalid",
        ) from None
    if not encoded or len(encoded) > MAX_ENVELOPE_BYTES:
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.envelope_invalid",
        )
    return encoded.decode("utf-8")


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _work_order_payload(
    job_payload: Any,
    policy: Any,
    route_id: Any,
) -> dict[str, Any]:
    try:
        identity, job, validated_policy = _STORE._binding(
            job_payload, policy, route_id,
        )
        benchmark_case = _SUITE.case_for_job(job)
        core = {
            "schema": WORK_ORDER_SCHEMA,
            "binding": {
                "reference_id": identity[0],
                "route_id": identity[1],
                "policy_sha256": identity[2],
                "job_sha256": identity[3],
            },
            "reference_revision": validated_policy.native_reference_revision,
            "suite": {
                "version": validated_policy.suite_version,
                "sha256": validated_policy.suite_sha256,
                "case_key": benchmark_case["key"],
            },
            "source": {
                "locale": job["source"]["locale"],
                "text": job["source"]["text"],
                "sha256": job["source"]["sha256"],
            },
            "target_locale": job["target"]["locale"],
            "content_type": job["content_type"],
            "quality_profile": _BENCHMARK._quality_profile_binding(
                job["target"]["locale"],
            ),
            "localization_policy": {
                "glossary_version": validated_policy.candidate_glossary_version,
                "policy_version": validated_policy.candidate_policy_version,
            },
            "qualification": {
                "method": "qualified_native_human",
                "verifier_id": validated_policy.native_reference_verifier_id,
                "verifier_version": (
                    validated_policy.native_reference_verifier_version
                ),
            },
        }
        return json.loads(_canonical_json(core))
    except NativeReferenceIntakeFailed:
        raise
    except Exception:
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.binding_invalid",
        ) from None


def create_native_reference_work_order(
    job_payload: Any,
    policy: Any,
    route_id: Any,
) -> dict[str, Any]:
    """Return one canonical target-free work order for an independent editor."""
    core = _work_order_payload(job_payload, policy, route_id)
    result = dict(core)
    result["work_order_id"] = "native-reference-work-order:" + _hash_json(core)
    return json.loads(_canonical_json(result))


def _current_work_order(
    work_order: Any,
    job_payload: Any,
    policy: Any,
    route_id: Any,
) -> dict[str, Any]:
    if not isinstance(work_order, Mapping):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.work_order_invalid",
        )
    candidate = dict(work_order)
    expected = create_native_reference_work_order(job_payload, policy, route_id)
    if _canonical_json(candidate) != _canonical_json(expected):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.work_order_stale",
        )
    return expected


def native_reference_verification_request_for_work_order(
    work_order: Any,
    job_payload: Any,
    policy: Any,
    route_id: Any,
    target_text: Any,
    *,
    reviewer_id: Any,
    reviewer_version: Any,
) -> dict[str, Any]:
    """Build the exact receipt payload after checking the current work order."""
    _current_work_order(work_order, job_payload, policy, route_id)
    try:
        return _BENCHMARK.native_reference_verification_request(
            job_payload,
            target_text,
            policy,
            reviewer_id=reviewer_id,
            reviewer_version=reviewer_version,
        )
    except Exception:
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.verification_request_invalid",
        ) from None


class _GuardedAdapter:
    def __init__(self, adapter: Any, guard: Callable[[], None]):
        self._adapter = adapter
        self._guard = guard
        self.guard_failed = False

    def _check(self) -> None:
        try:
            self._guard()
        except NativeReferenceIntakeFailed:
            self.guard_failed = True
            raise

    def verify(self, *args):
        self._check()
        return self._adapter.verify(*args)

    def sign(self, *args):
        self._check()
        return self._adapter.sign(*args)


def accept_native_reference_submission(
    store: Any,
    work_order: Any,
    submission: Any,
    job_payload: Any,
    policy: Any,
    route_id: Any,
    *,
    native_reference_verifier: Any,
    evidence_authority: Any,
    operation_guard: Callable[[], Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Verify, attest, and durably save one exact independent submission."""
    current = _current_work_order(work_order, job_payload, policy, route_id)
    expected_submission_keys = {
        "schema", "work_order_id", "work_order_sha256",
        "verification_request", "qualification_receipt",
    }
    if (
        not isinstance(submission, Mapping)
        or set(submission) != expected_submission_keys
        or submission.get("schema") != SUBMISSION_SCHEMA
        or submission.get("work_order_id") != current["work_order_id"]
        or not isinstance(submission.get("work_order_sha256"), str)
        or SHA256.fullmatch(submission["work_order_sha256"]) is None
        or submission["work_order_sha256"] != _hash_json(current)
        or not isinstance(submission.get("verification_request"), Mapping)
    ):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.submission_invalid",
        )
    request = dict(submission["verification_request"])
    qualification = request.get("qualification")
    expected_request = native_reference_verification_request_for_work_order(
        current,
        job_payload,
        policy,
        route_id,
        request.get("target_text"),
        reviewer_id=(
            qualification.get("reviewer_id")
            if isinstance(qualification, Mapping) else None
        ),
        reviewer_version=(
            qualification.get("reviewer_version")
            if isinstance(qualification, Mapping) else None
        ),
    )
    if _canonical_json(request) != _canonical_json(expected_request):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.verification_request_mismatch",
        )
    if operation_guard is not None and not callable(operation_guard):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.operation_guard_invalid",
        )
    if not callable(getattr(native_reference_verifier, "verify", None)):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.verifier_invalid",
        )
    if any(
        not callable(getattr(evidence_authority, method, None))
        for method in ("sign", "verify")
    ):
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.authority_invalid",
        )

    def guard() -> None:
        if operation_guard is None:
            return
        try:
            operation_guard()
        except Exception:
            raise NativeReferenceIntakeFailed(
                "native_reference.intake.operation_guard_failed",
                retryable=True,
            ) from None

    verifier = _GuardedAdapter(native_reference_verifier, guard)
    authority = _GuardedAdapter(evidence_authority, guard)
    try:
        artifact = _BENCHMARK.create_native_reference_artifact(
            job_payload,
            request["target_text"],
            policy,
            reviewer_id=request["qualification"]["reviewer_id"],
            reviewer_version=request["qualification"]["reviewer_version"],
            qualification_receipt=submission["qualification_receipt"],
            native_reference_verifier=verifier,
            evidence_authority=authority,
        )
        return store.save(
            job_payload,
            policy,
            route_id,
            artifact,
            native_reference_verifier=verifier,
            evidence_authority=authority,
            now=now,
        )
    except NativeReferenceIntakeFailed:
        raise
    except _STORE.NativeReferenceStoreFailed as error:
        if verifier.guard_failed or authority.guard_failed:
            raise NativeReferenceIntakeFailed(
                "native_reference.intake.operation_guard_failed",
                retryable=True,
            ) from None
        raise NativeReferenceIntakeFailed(
            error.code, retryable=error.retryable,
        ) from None
    except _BENCHMARK.BenchmarkBlocked as error:
        if verifier.guard_failed or authority.guard_failed:
            raise NativeReferenceIntakeFailed(
                "native_reference.intake.operation_guard_failed",
                retryable=True,
            ) from None
        retryable = error.code in {
            "benchmark.attestation.sign_failed",
            "benchmark.attestation.verify_failed",
            "benchmark.native_reference.verify_failed",
        }
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.verification_unavailable"
            if retryable else "native_reference.intake.submission_rejected",
            retryable=retryable,
        ) from None
    except Exception as error:
        if verifier.guard_failed or authority.guard_failed:
            raise NativeReferenceIntakeFailed(
                "native_reference.intake.operation_guard_failed",
                retryable=True,
            ) from None
        code = getattr(error, "code", None)
        retryable = getattr(error, "retryable", None)
        if (
            isinstance(code, str)
            and ERROR_CODE.fullmatch(code) is not None
            and code.startswith("native_reference.store.")
            and isinstance(retryable, bool)
        ):
            raise NativeReferenceIntakeFailed(
                code, retryable=retryable,
            ) from None
        raise NativeReferenceIntakeFailed(
            "native_reference.intake.adapter_invalid",
        ) from None
