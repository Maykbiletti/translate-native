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
        return _CAMPAIGN.run_next_benchmark_case(
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

    def status(self):
        return self.campaign_store.status(self.policy, self.campaign_id)

    def health(self, *, now: Any, stale_after_seconds: Any = 3600):
        return self.campaign_store.health(
            self.policy,
            self.campaign_id,
            self.evidence_authority,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )

    def summarize(self):
        return self.campaign_store.summarize(
            self.policy,
            self.campaign_id,
            self.evidence_authority,
            now=self.clock(),
        )
