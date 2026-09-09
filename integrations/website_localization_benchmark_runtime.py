#!/usr/bin/env python3
"""One fail-closed execution root for a durable benchmark campaign.

The host supplies four distinct SQLite connections and provider-neutral
adapters. This module owns the exact composition of durable candidate,
baseline, and qualified-native-reference artifacts before blind review.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark runtime dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CAMPAIGN = _load_module(
    "blun_website_localization_runtime_campaign",
    _ROOT / "integrations" / "website_localization_benchmark_campaign.py",
)
_CANDIDATE = _load_module(
    "blun_website_localization_runtime_candidate",
    _ROOT / "integrations" / "website_localization_benchmark_candidate.py",
)
_BASELINE = _load_module(
    "blun_website_localization_runtime_baseline",
    _ROOT / "integrations" / "website_localization_deepl_baseline.py",
)
_REFERENCE = _load_module(
    "blun_website_localization_runtime_reference",
    _ROOT / "integrations" / "website_localization_native_reference_store.py",
)
_REFERENCE_INTAKE = _load_module(
    "blun_website_localization_runtime_reference_intake",
    _ROOT / "integrations" / "website_localization_native_reference_intake.py",
)
_REFERENCE_QUEUE = _load_module(
    "blun_website_localization_runtime_reference_queue",
    _ROOT / "integrations" / "website_localization_native_reference_queue.py",
)
_REVIEW = _load_module(
    "blun_website_localization_runtime_review_store",
    _ROOT / "integrations" / "website_localization_benchmark_review_store.py",
)
_BENCHMARK = _CAMPAIGN._BENCHMARK


class BenchmarkRuntimeFailed(RuntimeError):
    """Content-free configuration or dependency failure."""

    benchmark_campaign_dependency_failure = True

    def __init__(self, code: str, *, retryable: bool = False):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("benchmark runtime error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("benchmark runtime retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class LeasedNativeReferenceWorkOrder:
    """One private lease plus its target-free editorial work order."""

    claim: Any
    work_order: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": "blun.website-localization-native-reference-lease.v1",
            "work_id": self.claim.work_id,
            "attempt": self.claim.attempt,
            "max_attempts": self.claim.max_attempts,
            "lease_token": self.claim.lease_token,
            "lease_expires_at": self.claim.lease_expires_at,
            "work_order": self.work_order,
        }


def _callable(value: Any, code: str) -> Any:
    if not callable(value):
        raise BenchmarkRuntimeFailed(code)
    return value


def _adapter(value: Any, method: str, code: str) -> Any:
    if not callable(getattr(value, method, None)):
        raise BenchmarkRuntimeFailed(code)
    return value


def _route(value: Any, code: str) -> str:
    if not isinstance(value, str) or _BENCHMARK.IDENTIFIER.fullmatch(value) is None:
        raise BenchmarkRuntimeFailed(code)
    return value


def _connection(value: Any) -> sqlite3.Connection:
    if not isinstance(value, sqlite3.Connection) or value.in_transaction:
        raise BenchmarkRuntimeFailed("benchmark.runtime.connection_invalid")
    return value


class _GuardedAuthority:
    def __init__(self, authority: Any, guard: Callable[[], None]):
        self._authority = authority
        self._guard = guard

    def sign(self, payload: bytes):
        self._guard()
        return self._authority.sign(payload)

    def verify(self, payload: bytes, signature: Any):
        self._guard()
        return self._authority.verify(payload, signature)


class _LazyCandidateProvider:
    def __init__(
        self,
        resolver: Callable[[dict[str, Any]], Any],
        job_payload: dict[str, Any],
        guard: Callable[[], None],
    ):
        self._resolver = resolver
        self._job_payload = job_payload
        self._guard = guard
        self._provider = None

    def invoke(self, request: Any):
        if self._provider is None:
            try:
                self._provider = DurableBenchmarkInputResolver._resolve(
                    self._resolver,
                    self._job_payload,
                    self._guard,
                    "benchmark.runtime.candidate_provider_unavailable",
                )
            except Exception as error:
                code = getattr(
                    error, "code", "benchmark.runtime.candidate_provider_unavailable",
                )
                retryable = getattr(error, "retryable", True)
                raise _CANDIDATE._WORKER.ProviderCallFailed(
                    code if isinstance(code, str) else "provider_unavailable",
                    retryable=retryable if isinstance(retryable, bool) else True,
                ) from None
        invoke = getattr(self._provider, "invoke", None)
        if not callable(invoke):
            raise _CANDIDATE._WORKER.ProviderCallFailed(
                "candidate_provider_invalid", retryable=False,
            )
        return invoke(request)


class DurableBenchmarkInputResolver:
    """Build one canonical input envelope from three exact durable stores."""

    def __init__(
        self,
        *,
        candidate_store: Any,
        baseline_store: Any,
        reference_store: Any,
        policy: Any,
        candidate_route_id: str,
        baseline_route_id: str,
        native_reference_route_id: str,
        assets_resolver: Callable[[dict[str, Any]], Any],
        candidate_provider_resolver: Callable[[dict[str, Any]], Any],
        baseline_acquirer: Callable[..., Any],
        native_reference_loader: Callable[[dict[str, Any]], Any],
        native_reference_verifier: Any,
        evidence_authority: Any,
        clock: Callable[[], float],
    ):
        self.candidate_store = candidate_store
        self.baseline_store = baseline_store
        self.reference_store = reference_store
        self.policy = policy
        self.candidate_route_id = candidate_route_id
        self.baseline_route_id = baseline_route_id
        self.native_reference_route_id = native_reference_route_id
        self.assets_resolver = assets_resolver
        self.candidate_provider_resolver = candidate_provider_resolver
        self.baseline_acquirer = baseline_acquirer
        self.native_reference_loader = native_reference_loader
        self.native_reference_verifier = native_reference_verifier
        self.evidence_authority = evidence_authority
        self.clock = clock

    def __call__(self, _: dict[str, Any]):
        raise BenchmarkRuntimeFailed("benchmark.runtime.operation_guard_missing")

    @staticmethod
    def _resolve(
        resolver: Callable[[dict[str, Any]], Any],
        job_payload: dict[str, Any],
        guard: Callable[[], None],
        code: str,
    ) -> Any:
        guard()
        try:
            value = resolver(job_payload)
        except Exception as error:
            if getattr(error, "benchmark_campaign_dependency_failure", None) is True:
                raise
            raise BenchmarkRuntimeFailed(code, retryable=True) from None
        guard()
        return value

    def resolve_with_operation_guard(
        self,
        job_payload: dict[str, Any],
        operation_guard: Callable[[], None],
    ) -> Any:
        _callable(operation_guard, "benchmark.runtime.operation_guard_invalid")
        assets = self._resolve(
            self.assets_resolver,
            job_payload,
            operation_guard,
            "benchmark.runtime.assets_unavailable",
        )
        try:
            candidate_assets = _CANDIDATE._WORKER.LocalizationAssets(
                glossary_version=assets.glossary_version,
                policy_version=assets.policy_version,
                audience=assets.audience,
                tone_profile=assets.tone_profile,
                glossary=tuple(
                    _CANDIDATE._WORKER.GlossaryTerm(
                        source=term.source,
                        target=term.target,
                        note=term.note,
                    )
                    for term in assets.glossary
                ),
                protected_terms=tuple(assets.protected_terms),
            )
        except (AttributeError, TypeError):
            raise BenchmarkRuntimeFailed(
                "benchmark.runtime.assets_invalid",
            ) from None
        candidate = _CANDIDATE.resolve_candidate_acquisition(
            self.candidate_store,
            job_payload,
            self.policy,
            self.candidate_route_id,
            candidate_assets,
            _LazyCandidateProvider(
                self.candidate_provider_resolver,
                job_payload,
                operation_guard,
            ),
            evidence_authority=self.evidence_authority,
            operation_guard=operation_guard,
            now=self.clock(),
        )
        guarded_authority = _GuardedAuthority(
            self.evidence_authority, operation_guard,
        )

        def acquire_baseline():
            operation_guard()
            try:
                return self.baseline_acquirer(
                    job_payload,
                    self.policy,
                    self.evidence_authority,
                    operation_guard,
                )
            except Exception as error:
                if getattr(error, "benchmark_campaign_dependency_failure", None) is True:
                    raise
                raise BenchmarkRuntimeFailed(
                    "benchmark.runtime.baseline_unavailable", retryable=True,
                ) from None

        baseline = _BASELINE.resolve_baseline_acquisition(
            self.baseline_store,
            job_payload,
            self.policy,
            self.baseline_route_id,
            acquire_baseline,
            evidence_authority=guarded_authority,
            now=self.clock(),
        )

        def load_reference():
            try:
                return self.native_reference_loader(job_payload)
            except Exception as error:
                if getattr(error, "benchmark_campaign_dependency_failure", None) is True:
                    raise
                raise BenchmarkRuntimeFailed(
                    "benchmark.runtime.reference_unavailable", retryable=True,
                ) from None

        reference = _REFERENCE.resolve_native_reference_artifact(
            self.reference_store,
            job_payload,
            self.policy,
            self.native_reference_route_id,
            load_reference,
            native_reference_verifier=self.native_reference_verifier,
            evidence_authority=guarded_authority,
            operation_guard=operation_guard,
            now=self.clock(),
        )
        return _CAMPAIGN.BenchmarkCaseInputs(
            candidate_result=candidate,
            baseline_artifact=baseline.artifact,
            assets=assets,
            native_reference_artifact=reference,
        )


class WebsiteLocalizationBenchmarkRuntime:
    """Preflight, persist, and execute one exact benchmark campaign."""

    def __init__(
        self,
        *,
        campaign_connection: sqlite3.Connection,
        candidate_connection: sqlite3.Connection,
        baseline_connection: sqlite3.Connection,
        native_reference_connection: sqlite3.Connection,
        review_connection: sqlite3.Connection,
        policy: Any,
        candidate_route_id: str,
        baseline_route_id: str,
        native_reference_route_id: str,
        reviewer_route_id: str,
        assets_resolver: Callable[[dict[str, Any]], Any],
        candidate_provider_resolver: Callable[[dict[str, Any]], Any],
        baseline_acquirer: Callable[..., Any],
        native_reference_loader: Callable[[dict[str, Any]], Any],
        reviewer: Any,
        native_reference_verifier: Any,
        evidence_authority: Any,
        blinding_key: bytes,
        worker_id: str,
        max_attempts: int = 3,
        clock: Callable[[], float] = time.time,
    ):
        connections = tuple(_connection(item) for item in (
            campaign_connection,
            candidate_connection,
            baseline_connection,
            native_reference_connection,
            review_connection,
        ))
        if len({id(item) for item in connections}) != len(connections):
            raise BenchmarkRuntimeFailed("benchmark.runtime.connection_reused")
        try:
            policy = _BENCHMARK._validate_policy(policy)
        except Exception:
            raise BenchmarkRuntimeFailed("benchmark.runtime.policy_invalid") from None
        candidate_route_id = _route(
            candidate_route_id, "benchmark.runtime.candidate_route_invalid",
        )
        baseline_route_id = _route(
            baseline_route_id, "benchmark.runtime.baseline_route_invalid",
        )
        native_reference_route_id = _route(
            native_reference_route_id, "benchmark.runtime.reference_route_invalid",
        )
        reviewer_route_id = _route(
            reviewer_route_id, "benchmark.runtime.reviewer_route_invalid",
        )
        _callable(assets_resolver, "benchmark.runtime.assets_resolver_invalid")
        _callable(
            candidate_provider_resolver,
            "benchmark.runtime.candidate_provider_resolver_invalid",
        )
        _callable(baseline_acquirer, "benchmark.runtime.baseline_acquirer_invalid")
        _callable(
            native_reference_loader, "benchmark.runtime.reference_loader_invalid",
        )
        _adapter(reviewer, "review", "benchmark.runtime.reviewer_invalid")
        _adapter(
            native_reference_verifier,
            "verify",
            "benchmark.runtime.reference_verifier_invalid",
        )
        _adapter(evidence_authority, "sign", "benchmark.runtime.authority_invalid")
        _adapter(evidence_authority, "verify", "benchmark.runtime.authority_invalid")
        if not isinstance(blinding_key, bytes) or len(blinding_key) < 32:
            raise BenchmarkRuntimeFailed("benchmark.runtime.blinding_key_invalid")
        try:
            worker_id = _CAMPAIGN._identifier(worker_id)
        except Exception:
            raise BenchmarkRuntimeFailed("benchmark.runtime.worker_invalid") from None
        _callable(clock, "benchmark.runtime.clock_invalid")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= _CAMPAIGN.MAX_ATTEMPTS
        ):
            raise BenchmarkRuntimeFailed("benchmark.runtime.attempts_invalid")
        try:
            initial_now = _CAMPAIGN._timestamp(clock())
        except Exception:
            raise BenchmarkRuntimeFailed("benchmark.runtime.clock_invalid") from None
        try:
            _CAMPAIGN._assert_policy_current(policy, initial_now)
        except Exception:
            raise BenchmarkRuntimeFailed(
                "benchmark.runtime.validity_expired",
            ) from None

        self.policy = policy
        self.reviewer = reviewer
        self.reviewer_route_id = reviewer_route_id
        self.native_reference_verifier = native_reference_verifier
        self.evidence_authority = evidence_authority
        self.blinding_key = blinding_key
        self.worker_id = worker_id
        self.clock = clock
        self.campaign_store = _CAMPAIGN.BenchmarkCampaignStore(connections[0])
        candidate_store = _CANDIDATE.CandidateAcquisitionStore(connections[1])
        baseline_store = _BASELINE.BaselineAcquisitionStore(connections[2])
        reference_store = _REFERENCE.NativeReferenceArtifactStore(connections[3])
        self.review_store = _REVIEW.BenchmarkReviewEvidenceStore(connections[4])
        self.campaign_id = self.campaign_store.create(
            policy, max_attempts=max_attempts, now=initial_now,
        )
        self.native_reference_queue = _REFERENCE_QUEUE.NativeReferenceWorkQueue(
            self.campaign_store,
        )
        self.native_reference_queue.ensure(
            policy,
            self.campaign_id,
            max_attempts=max_attempts,
            now=initial_now,
        )
        self.input_resolver = DurableBenchmarkInputResolver(
            candidate_store=candidate_store,
            baseline_store=baseline_store,
            reference_store=reference_store,
            policy=policy,
            candidate_route_id=candidate_route_id,
            baseline_route_id=baseline_route_id,
            native_reference_route_id=native_reference_route_id,
            assets_resolver=assets_resolver,
            candidate_provider_resolver=candidate_provider_resolver,
            baseline_acquirer=baseline_acquirer,
            native_reference_loader=native_reference_loader,
            native_reference_verifier=native_reference_verifier,
            evidence_authority=evidence_authority,
            clock=clock,
        )

    def run_once(
        self,
        *,
        operation_guard: Callable[[float], Any] | None = None,
        lease_seconds: Any = 300,
        retry_base_seconds: Any = 5,
        retry_max_seconds: Any = 3600,
    ):
        review_guard = (
            None
            if operation_guard is None
            else lambda: operation_guard(lease_seconds)
        )
        durable_reviewer = _REVIEW.DurableBenchmarkReviewer(
            store=self.review_store,
            policy=self.policy,
            route_id=self.reviewer_route_id,
            reviewer=self.reviewer,
            evidence_authority=self.evidence_authority,
            operation_guard=review_guard,
            clock=self.clock,
        )
        outcome = _CAMPAIGN.run_next_benchmark_case(
            self.campaign_store,
            self.policy,
            self.campaign_id,
            self.worker_id,
            self.input_resolver,
            durable_reviewer,
            blinding_key=self.blinding_key,
            native_reference_verifier=self.native_reference_verifier,
            evidence_authority=self.evidence_authority,
            clock=self.clock,
            operation_guard=operation_guard,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
        )
        report_outcome = None
        if (
            outcome is None or outcome.status == "succeeded"
        ) and self.campaign_store.report_finalization_required(
            self.policy, self.campaign_id,
        ):
            report_outcome = _CAMPAIGN.run_benchmark_report_finalization(
                self.campaign_store,
                self.policy,
                self.campaign_id,
                self.worker_id,
                self.evidence_authority,
                clock=self.clock,
                operation_guard=operation_guard,
                lease_seconds=lease_seconds,
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
            )
        return outcome if outcome is not None else report_outcome

    def status(self):
        return self.campaign_store.status(self.policy, self.campaign_id)

    @staticmethod
    def _retry_delay(attempt: int, base: Any, maximum: Any) -> float:
        base = _REFERENCE_QUEUE._duration(base)
        maximum = _REFERENCE_QUEUE._duration(maximum)
        return min(maximum, base * (2 ** max(0, attempt - 1)))

    def claim_native_reference_work_order(
        self,
        editor_id: Any,
        *,
        target_locale: Any = None,
        request_id: Any = None,
        operation_guard: Callable[[float], Any] | None = None,
        lease_seconds: Any = 3600,
        retry_base_seconds: Any = 30,
        retry_max_seconds: Any = 3600,
    ) -> LeasedNativeReferenceWorkOrder | None:
        """Lease the next unresolved exact job without persisting its prose."""
        if operation_guard is not None and not callable(operation_guard):
            raise BenchmarkRuntimeFailed(
                "benchmark.runtime.reference_queue.guard_invalid",
            )
        lease_seconds = _REFERENCE_QUEUE._duration(lease_seconds)
        self._retry_delay(1, retry_base_seconds, retry_max_seconds)
        expected_count = len(_CAMPAIGN._expected_work(self.policy))
        for _ in range(expected_count):
            try:
                claim = self.native_reference_queue.claim(
                    self.policy,
                    self.campaign_id,
                    editor_id,
                    now=self.clock(),
                    lease_seconds=lease_seconds,
                    target_locale=target_locale,
                    request_id=request_id,
                )
            except Exception as error:
                code = getattr(
                    error,
                    "code",
                    "benchmark.runtime.reference_queue.state_invalid",
                )
                if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                    code = "benchmark.runtime.reference_queue.state_invalid"
                raise BenchmarkRuntimeFailed(code) from None
            if claim is None:
                return None

            def guard() -> None:
                try:
                    if operation_guard is not None:
                        operation_guard(lease_seconds)
                    _CAMPAIGN._assert_policy_current(
                        self.policy, self.clock(),
                    )
                    self.native_reference_queue.assert_live(
                        claim, now=self.clock(),
                    )
                except _CAMPAIGN.BenchmarkCampaignBlocked as error:
                    if error.code == "benchmark.campaign.validity_expired":
                        raise _REFERENCE_INTAKE.NativeReferenceIntakeFailed(
                            "native_reference.queue.policy_expired",
                        ) from None
                    raise _REFERENCE_INTAKE.NativeReferenceIntakeFailed(
                        "native_reference.intake.operation_guard_failed",
                        retryable=True,
                    ) from None
                except _REFERENCE_INTAKE.NativeReferenceIntakeFailed:
                    raise
                except Exception:
                    raise _REFERENCE_INTAKE.NativeReferenceIntakeFailed(
                        "native_reference.intake.operation_guard_failed",
                        retryable=True,
                    ) from None

            guarded_verifier = _REFERENCE_INTAKE._GuardedAdapter(
                self.native_reference_verifier, guard,
            )
            guarded_authority = _REFERENCE_INTAKE._GuardedAdapter(
                self.evidence_authority, guard,
            )
            try:
                guard()
                artifact = self.input_resolver.reference_store.load(
                    claim.job_payload,
                    self.policy,
                    self.input_resolver.native_reference_route_id,
                    native_reference_verifier=guarded_verifier,
                    evidence_authority=guarded_authority,
                )
                guard()
                if artifact is None:
                    return LeasedNativeReferenceWorkOrder(
                        claim=claim,
                        work_order=self.create_native_reference_work_order(
                            claim.job_payload,
                        ),
                    )
                self.native_reference_queue.complete(
                    self.policy,
                    claim,
                    _REFERENCE_INTAKE._hash_json(artifact),
                    now=self.clock(),
                )
            except Exception as error:
                code = getattr(
                    error,
                    "code",
                    "native_reference.queue.reconciliation_failed",
                )
                retryable = getattr(error, "retryable", False)
                if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                    code = "native_reference.queue.reconciliation_failed"
                if not isinstance(retryable, bool):
                    retryable = False
                try:
                    outcome = self.native_reference_queue.transition_failure(
                        self.policy,
                        claim,
                        code,
                        retryable=retryable,
                        delay_seconds=self._retry_delay(
                            claim.attempt,
                            retry_base_seconds,
                            retry_max_seconds,
                        ),
                        now=self.clock(),
                    )
                except Exception as transition_error:
                    transition_code = getattr(
                        transition_error,
                        "code",
                        "benchmark.runtime.reference_queue.state_invalid",
                    )
                    if (
                        not isinstance(transition_code, str)
                        or ERROR_CODE.fullmatch(transition_code) is None
                    ):
                        transition_code = (
                            "benchmark.runtime.reference_queue.state_invalid"
                        )
                    raise BenchmarkRuntimeFailed(
                        code
                        if code == "native_reference.queue.policy_expired"
                        else transition_code,
                    ) from None
                raise BenchmarkRuntimeFailed(
                    outcome.error_code or code,
                    retryable=outcome.status == "retry_wait",
                ) from None
        raise BenchmarkRuntimeFailed(
            "benchmark.runtime.reference_queue.state_invalid",
        )

    def native_reference_lease_from_payload(
        self, payload: Any, *, editor_id: Any, target_locale: Any,
    ) -> LeasedNativeReferenceWorkOrder:
        """Reconstruct and verify one private lease at a stateless boundary."""
        keys = {
            "schema", "work_id", "attempt", "max_attempts",
            "lease_token", "lease_expires_at", "work_order",
        }
        try:
            if (
                not isinstance(payload, dict)
                or set(payload) != keys
                or payload.get("schema")
                != "blun.website-localization-native-reference-lease.v1"
                or not isinstance(payload.get("work_order"), dict)
            ):
                raise ValueError
            editor_id = _REFERENCE_QUEUE._identifier(editor_id)
            target_locale = _REFERENCE_QUEUE._identifier(target_locale)
            work_order = payload["work_order"]
            suite = work_order.get("suite")
            if (
                work_order.get("target_locale") != target_locale
                or not isinstance(suite, dict)
                or not isinstance(suite.get("case_key"), str)
            ):
                raise ValueError
            case_key = suite["case_key"]
            expected = {
                (locale, key): work_id
                for work_id, locale, key in _CAMPAIGN._expected_work(self.policy)
            }
            work_id = expected.get((target_locale, case_key))
            if work_id is None or payload.get("work_id") != work_id:
                raise ValueError
            attempt = payload.get("attempt")
            maximum = payload.get("max_attempts")
            if (
                isinstance(attempt, bool) or not isinstance(attempt, int)
                or isinstance(maximum, bool) or not isinstance(maximum, int)
                or not 1 <= attempt <= maximum <= _REFERENCE_QUEUE.MAX_ATTEMPTS
            ):
                raise ValueError
            token = _REFERENCE_QUEUE._identifier(payload.get("lease_token"))
            expires = _REFERENCE_QUEUE._timestamp(payload.get("lease_expires_at"))
            job_payload = _CAMPAIGN._job_payload(
                self.policy, target_locale, case_key,
            )
            expected_order = self.create_native_reference_work_order(job_payload)
            if _REFERENCE_INTAKE._canonical_json(
                work_order,
            ) != _REFERENCE_INTAKE._canonical_json(expected_order):
                raise ValueError
            claim = _REFERENCE_QUEUE.ClaimedNativeReference(
                work_id=work_id,
                campaign_id=self.campaign_id,
                target_locale=target_locale,
                suite_case_key=case_key,
                job_payload=job_payload,
                attempt=attempt,
                max_attempts=maximum,
                lease_owner=editor_id,
                lease_token=token,
                lease_expires_at=expires,
            )
            self.native_reference_queue.assert_live(claim, now=self.clock())
            return LeasedNativeReferenceWorkOrder(
                claim=claim, work_order=expected_order,
            )
        except BenchmarkRuntimeFailed:
            raise
        except Exception as error:
            code = getattr(
                error, "code", "benchmark.runtime.reference_queue.claim_invalid",
            )
            if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                code = "benchmark.runtime.reference_queue.claim_invalid"
            raise BenchmarkRuntimeFailed(code) from None

    def renew_native_reference_work_order(
        self,
        lease: Any,
        *,
        lease_seconds: Any = 3600,
        request_id: Any = None,
        request_sha256: Any = None,
    ) -> LeasedNativeReferenceWorkOrder:
        """Renew only the exact live editorial lease token."""
        if not isinstance(lease, LeasedNativeReferenceWorkOrder):
            raise BenchmarkRuntimeFailed(
                "benchmark.runtime.reference_queue.claim_invalid",
            )
        try:
            claim = self.native_reference_queue.renew(
                lease.claim,
                now=self.clock(),
                lease_seconds=lease_seconds,
                policy=self.policy if (
                    request_id is not None or request_sha256 is not None
                ) else None,
                request_id=request_id,
                request_sha256=request_sha256,
            )
        except Exception as error:
            code = getattr(
                error, "code", "benchmark.runtime.reference_queue.state_invalid",
            )
            raise BenchmarkRuntimeFailed(code) from None
        return LeasedNativeReferenceWorkOrder(
            claim=claim, work_order=lease.work_order,
        )

    def native_reference_http_request_replay(
        self,
        *,
        editor_id: Any,
        target_locale: Any,
        operation: Any,
        request_id: Any,
        request_sha256: Any,
    ) -> dict[str, Any] | None:
        """Load one exact completed HTTP mutation without touching prose."""
        try:
            return self.native_reference_queue.replay_http_request(
                self.policy,
                self.campaign_id,
                editor_id,
                target_locale,
                operation,
                request_id,
                request_sha256,
                now=self.clock(),
            )
        except Exception as error:
            code = getattr(
                error,
                "code",
                "benchmark.runtime.reference_queue.state_invalid",
            )
            if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                code = "benchmark.runtime.reference_queue.state_invalid"
            raise BenchmarkRuntimeFailed(code) from None

    def accept_leased_native_reference_submission(
        self,
        lease: Any,
        submission: Any,
        *,
        operation_guard: Callable[[float], Any] | None = None,
        lease_seconds: Any = 3600,
        retry_base_seconds: Any = 30,
        retry_max_seconds: Any = 3600,
        request_id: Any = None,
        request_sha256: Any = None,
    ):
        """Verify, store, and complete one exact leased editorial result."""
        if not isinstance(lease, LeasedNativeReferenceWorkOrder):
            raise BenchmarkRuntimeFailed(
                "benchmark.runtime.reference_queue.claim_invalid",
            )
        if operation_guard is not None and not callable(operation_guard):
            raise BenchmarkRuntimeFailed(
                "benchmark.runtime.reference_queue.guard_invalid",
            )
        lease_seconds = _REFERENCE_QUEUE._duration(lease_seconds)
        http_request = request_id is not None or request_sha256 is not None
        if http_request:
            try:
                replay = self.native_reference_queue.begin_http_submission_request(
                    self.policy,
                    lease.claim,
                    request_id,
                    request_sha256,
                    now=self.clock(),
                )
                if replay is not None:
                    return self.native_reference_queue.outcome_from_payload(replay)
            except Exception as error:
                code = getattr(
                    error,
                    "code",
                    "benchmark.runtime.reference_queue.state_invalid",
                )
                if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                    code = "benchmark.runtime.reference_queue.state_invalid"
                raise BenchmarkRuntimeFailed(code) from None

        def guard() -> None:
            try:
                if operation_guard is not None:
                    operation_guard(lease_seconds)
                _CAMPAIGN._assert_policy_current(
                    self.policy, self.clock(),
                )
                self.native_reference_queue.assert_live(
                    lease.claim, now=self.clock(),
                )
            except _CAMPAIGN.BenchmarkCampaignBlocked as error:
                if error.code == "benchmark.campaign.validity_expired":
                    raise _REFERENCE_INTAKE.NativeReferenceIntakeFailed(
                        "native_reference.queue.policy_expired",
                    ) from None
                raise _REFERENCE_INTAKE.NativeReferenceIntakeFailed(
                    "native_reference.intake.operation_guard_failed",
                    retryable=True,
                ) from None
            except _REFERENCE_INTAKE.NativeReferenceIntakeFailed:
                raise
            except Exception:
                raise _REFERENCE_INTAKE.NativeReferenceIntakeFailed(
                    "native_reference.intake.operation_guard_failed",
                    retryable=True,
                ) from None

        try:
            artifact = _REFERENCE_INTAKE.accept_native_reference_submission(
                self.input_resolver.reference_store,
                lease.work_order,
                submission,
                lease.claim.job_payload,
                self.policy,
                self.input_resolver.native_reference_route_id,
                native_reference_verifier=self.native_reference_verifier,
                evidence_authority=self.evidence_authority,
                operation_guard=guard,
                now=self.clock(),
            )
            guard()
            return self.native_reference_queue.complete(
                self.policy,
                lease.claim,
                _REFERENCE_INTAKE._hash_json(artifact),
                now=self.clock(),
                request_id=request_id,
                request_sha256=request_sha256,
            )
        except Exception as error:
            code = getattr(
                error, "code", "native_reference.intake.adapter_invalid",
            )
            retryable = getattr(error, "retryable", False)
            if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                code = "native_reference.intake.adapter_invalid"
            if not isinstance(retryable, bool):
                retryable = False
            try:
                return self.native_reference_queue.transition_failure(
                    self.policy,
                    lease.claim,
                    code,
                    retryable=retryable,
                    delay_seconds=self._retry_delay(
                        lease.claim.attempt,
                        retry_base_seconds,
                        retry_max_seconds,
                    ),
                    now=self.clock(),
                    request_id=request_id,
                    request_sha256=request_sha256,
                )
            except Exception as transition_error:
                transition_code = getattr(
                    transition_error,
                    "code",
                    "benchmark.runtime.reference_queue.state_invalid",
                )
                if (
                    not isinstance(transition_code, str)
                    or ERROR_CODE.fullmatch(transition_code) is None
                ):
                    transition_code = (
                        "benchmark.runtime.reference_queue.state_invalid"
                    )
                raise BenchmarkRuntimeFailed(
                    code
                    if code == "native_reference.queue.policy_expired"
                    else transition_code,
                ) from None

    def native_reference_queue_status(self):
        """Return content-free intake counts and stable failure codes."""
        return self.native_reference_queue.status(
            self.policy, self.campaign_id,
        )

    def native_reference_queue_health(
        self, *, now: Any, stale_after_seconds: Any = 3600,
    ):
        """Reverify every completed digest in a content-free health snapshot."""
        health = self.native_reference_queue.health(
            self.policy,
            self.campaign_id,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
        if health.status == "blocked":
            return health
        try:
            rows = self.native_reference_queue.connection.execute("""
                SELECT target_locale, suite_case_key, artifact_sha256
                FROM benchmark_native_reference_queue
                WHERE campaign_id = ? AND status = 'succeeded'
                ORDER BY target_locale, suite_case_key
            """, (self.campaign_id,)).fetchall()
            for row in rows:
                payload = _CAMPAIGN._job_payload(
                    self.policy, row["target_locale"], row["suite_case_key"],
                )
                artifact = self.input_resolver.reference_store.load(
                    payload,
                    self.policy,
                    self.input_resolver.native_reference_route_id,
                    native_reference_verifier=self.native_reference_verifier,
                    evidence_authority=self.evidence_authority,
                )
                if (
                    artifact is None
                    or _REFERENCE_INTAKE._hash_json(artifact)
                    != row["artifact_sha256"]
                ):
                    raise ValueError
        except Exception:
            return _REFERENCE_QUEUE.NativeReferenceQueueHealth(
                campaign_id=self.campaign_id,
                status="blocked",
                reasons=("native_reference.queue.state_invalid",),
                counts=health.counts,
                work_count=health.work_count,
                last_progress_at=health.last_progress_at,
            )
        return health

    def create_native_reference_work_order(self, job_payload: Any):
        """Export one current target-free order for qualified native review."""
        return _REFERENCE_INTAKE.create_native_reference_work_order(
            job_payload,
            self.policy,
            self.input_resolver.native_reference_route_id,
        )

    def native_reference_verification_request(
        self,
        work_order: Any,
        job_payload: Any,
        target_text: Any,
        *,
        reviewer_id: Any,
        reviewer_version: Any,
    ):
        """Return the exact request that the qualification receipt must bind."""
        return _REFERENCE_INTAKE.native_reference_verification_request_for_work_order(
            work_order,
            job_payload,
            self.policy,
            self.input_resolver.native_reference_route_id,
            target_text,
            reviewer_id=reviewer_id,
            reviewer_version=reviewer_version,
        )

    def accept_native_reference_submission(
        self,
        work_order: Any,
        submission: Any,
        job_payload: Any,
        *,
        operation_guard: Callable[[], Any] | None = None,
    ):
        """Verify and persist one current qualified-native submission."""
        return _REFERENCE_INTAKE.accept_native_reference_submission(
            self.input_resolver.reference_store,
            work_order,
            submission,
            job_payload,
            self.policy,
            self.input_resolver.native_reference_route_id,
            native_reference_verifier=self.native_reference_verifier,
            evidence_authority=self.evidence_authority,
            operation_guard=operation_guard,
            now=self.clock(),
        )

    def health(self, *, now: Any, stale_after_seconds: Any = 3600):
        return self.campaign_store.health(
            self.policy,
            self.campaign_id,
            self.evidence_authority,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )

    def load_report(self):
        """Return the stored, reverified report without signing or writes."""
        return self.campaign_store.load_report(
            self.policy,
            self.campaign_id,
            self.evidence_authority,
            now=self.clock(),
        )

    def summarize(
        self, *, operation_guard: Callable[[float], Any] | None = None,
        lease_seconds: Any = 300,
    ):
        if operation_guard is not None and not callable(operation_guard):
            raise TypeError("operation_guard must be callable")
        lease_seconds = _CAMPAIGN._duration(lease_seconds)
        report_guard = (
            None
            if operation_guard is None
            else lambda: operation_guard(lease_seconds)
        )
        return self.campaign_store.summarize(
            self.policy,
            self.campaign_id,
            self.evidence_authority,
            now=self.clock(),
            operation_guard=report_guard,
        )
