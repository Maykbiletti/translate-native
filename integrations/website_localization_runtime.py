#!/usr/bin/env python3
"""Provider-neutral composition root for the website-localization service.

The host owns distinct SQLite connections and every external capability.
This module validates those inputs before constructing the durable queue,
release, CMS, evidence, supervisor, and read-only health components.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sqlite3
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping


SCHEMA = "blun.website-localization-runtime.v1"
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
REQUIRED_TICK_KEYS = frozenset({
    "provider_resolver", "assets_resolver", "evidence_provider",
    "quality_verifier", "event_verifier", "approval_authority",
    "publication_authority", "publisher", "translation_worker_id",
    "evidence_worker_id", "delivery_worker_id", "evidence_revision",
})
OPTIONAL_TICK_KEYS = frozenset({
    "translation_lease_seconds", "translation_retry_base_seconds",
    "translation_retry_max_seconds", "evidence_lease_seconds",
    "evidence_max_attempts", "approval_ttl_seconds",
    "delivery_lease_seconds", "delivery_max_attempts",
    "human_review_verifier", "independent_model_review_verifier", "result_cache",
})
OPERATION_LEASE_DEFAULTS = {
    "translation_lease_seconds": 300.0,
    "evidence_lease_seconds": 300.0,
    "delivery_lease_seconds": 300.0,
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required runtime dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_SERVICE = _load_module(
    "blun_website_localization_runtime_service",
    _ROOT / "integrations" / "website_localization_service.py",
)
_SUPERVISOR = _load_module(
    "blun_website_localization_runtime_supervisor",
    _ROOT / "integrations" / "website_localization_supervisor.py",
)
_HEALTH = _load_module(
    "blun_website_localization_runtime_health",
    _ROOT / "integrations" / "website_localization_health.py",
)
_BENCHMARK_RUNTIME = _load_module(
    "blun_website_localization_runtime_benchmark_execution",
    _ROOT / "integrations" / "website_localization_benchmark_runtime.py",
)
_API = _load_module(
    "blun_website_localization_runtime_api",
    _ROOT / "integrations" / "website_localization_api.py",
)
_CMS = _SERVICE._CMS
_QUEUE = _CMS._QUEUE
_RELEASE = _CMS._RELEASE
_COORDINATOR = _SERVICE._COORDINATOR


class LocalizationRuntimeBlocked(RuntimeError):
    """Stable host-composition failure without customer or secret values."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise LocalizationRuntimeBlocked(code)
    return value


def _number(
    value: Any,
    code: str,
    *,
    maximum: float | None = None,
    allow_zero: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalizationRuntimeBlocked(code)
    number = float(value)
    if (
        not math.isfinite(number)
        or (number < 0 if allow_zero else number <= 0)
        or (maximum is not None and number > maximum)
    ):
        raise LocalizationRuntimeBlocked(code)
    return number


def _capability(value: Any, method: str, code: str) -> None:
    if not callable(getattr(value, method, None)):
        raise LocalizationRuntimeBlocked(code)


def _validate_dependencies(values: Mapping[str, Any]) -> MappingProxyType:
    if not isinstance(values, Mapping):
        raise LocalizationRuntimeBlocked("runtime.dependencies.invalid")
    copied = dict(values)
    keys = set(copied)
    if keys - REQUIRED_TICK_KEYS - OPTIONAL_TICK_KEYS or REQUIRED_TICK_KEYS - keys:
        raise LocalizationRuntimeBlocked("runtime.dependencies.invalid")
    if "result_cache" in copied:
        raise LocalizationRuntimeBlocked("runtime.result_cache.external_forbidden")
    if not callable(copied["provider_resolver"]):
        raise LocalizationRuntimeBlocked("runtime.provider_resolver.invalid")
    if not callable(copied["assets_resolver"]):
        raise LocalizationRuntimeBlocked("runtime.assets_resolver.invalid")
    for name, method in (
        ("evidence_provider", "obtain"),
        ("quality_verifier", "verify"),
        ("event_verifier", "verify"),
        ("approval_authority", "sign"),
        ("approval_authority", "verify"),
        ("publication_authority", "sign"),
        ("publication_authority", "verify"),
        ("publisher", "publish"),
    ):
        _capability(copied[name], method, f"runtime.{name}.invalid")
    for name in (
        "translation_worker_id", "evidence_worker_id", "delivery_worker_id",
        "evidence_revision",
    ):
        _identifier(copied[name], f"runtime.{name}.invalid")
    human = copied.get("human_review_verifier")
    if human is not None:
        _capability(human, "verify", "runtime.human_review_verifier.invalid")
    independent = copied.get("independent_model_review_verifier")
    if independent is not None:
        _capability(
            independent, "verify", "runtime.independent_model_review_verifier.invalid",
        )
    bounds = {
        "translation_lease_seconds": (86_400.0, False),
        "translation_retry_base_seconds": (86_400.0, True),
        "translation_retry_max_seconds": (86_400.0, True),
        "evidence_lease_seconds": (3_600.0, False),
        "approval_ttl_seconds": (31_536_000.0, False),
        "delivery_lease_seconds": (86_400.0, False),
    }
    for name, (maximum, allow_zero) in bounds.items():
        if name in copied:
            _number(
                copied[name], f"runtime.{name}.invalid",
                maximum=maximum, allow_zero=allow_zero,
            )
    for name in ("evidence_max_attempts", "delivery_max_attempts"):
        if name in copied and (
            isinstance(copied[name], bool)
            or not isinstance(copied[name], int)
            or not 1 <= copied[name] <= 20
        ):
            raise LocalizationRuntimeBlocked(f"runtime.{name}.invalid")
    if (
        "translation_retry_base_seconds" in copied
        and "translation_retry_max_seconds" in copied
        and float(copied["translation_retry_base_seconds"])
        > float(copied["translation_retry_max_seconds"])
    ):
        raise LocalizationRuntimeBlocked("runtime.translation_retry_policy.invalid")
    return MappingProxyType(copied)


def _validate_connections(connections: tuple[Any, ...]) -> None:
    if len(connections) not in {5, 6, 10} or any(
        not isinstance(connection, sqlite3.Connection) for connection in connections
    ):
        raise LocalizationRuntimeBlocked("runtime.connections.invalid")
    if len({id(connection) for connection in connections}) != len(connections):
        raise LocalizationRuntimeBlocked("runtime.connections.not_distinct")
    if any(connection.in_transaction for connection in connections):
        raise LocalizationRuntimeBlocked("runtime.connections.transaction_active")


def _supervisor_policy(value: Any):
    if value is None:
        return _SUPERVISOR.SupervisorPolicy(lease_seconds=360.0)
    if not isinstance(value, Mapping):
        raise LocalizationRuntimeBlocked("runtime.supervisor_policy.invalid")
    expected = {
        "lease_seconds", "active_delay_seconds", "idle_delay_seconds",
        "blocked_base_seconds", "blocked_max_seconds", "stop_poll_seconds",
    }
    if set(value) != expected:
        raise LocalizationRuntimeBlocked("runtime.supervisor_policy.invalid")
    try:
        return _SUPERVISOR.SupervisorPolicy(**dict(value)).validated()
    except Exception:
        raise LocalizationRuntimeBlocked("runtime.supervisor_policy.invalid") from None


def _validate_lease_hierarchy(
    dependencies: Mapping[str, Any],
    supervisor_policy: Any,
    *,
    benchmark_lease_seconds: float | None = None,
) -> None:
    leases = [
        float(dependencies.get(name, default))
        for name, default in OPERATION_LEASE_DEFAULTS.items()
    ]
    if benchmark_lease_seconds is not None:
        leases.append(benchmark_lease_seconds)
    longest_operation_lease = max(leases)
    if float(supervisor_policy.lease_seconds) <= longest_operation_lease:
        raise LocalizationRuntimeBlocked("runtime.lease_hierarchy.invalid")


BENCHMARK_EXECUTION_KEYS = frozenset({
    "candidate_connection", "baseline_connection",
    "native_reference_connection", "review_connection", "candidate_route_id",
    "baseline_route_id", "native_reference_route_id", "reviewer_route_id",
    "assets_resolver",
    "candidate_provider_resolver", "baseline_acquirer",
    "native_reference_loader", "reviewer", "native_reference_verifier",
    "blinding_key", "worker_id", "max_attempts", "lease_seconds",
    "retry_base_seconds", "retry_max_seconds",
})


def _benchmark_execution(value: Any) -> MappingProxyType | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != BENCHMARK_EXECUTION_KEYS:
        raise LocalizationRuntimeBlocked("runtime.benchmark.execution.invalid")
    copied = dict(value)
    try:
        for name in (
            "candidate_connection", "baseline_connection",
            "native_reference_connection", "review_connection",
        ):
            _BENCHMARK_RUNTIME._connection(copied[name])
        for name in (
            "candidate_route_id", "baseline_route_id",
            "native_reference_route_id", "reviewer_route_id",
        ):
            _BENCHMARK_RUNTIME._route(
                copied[name], "benchmark.runtime.route_invalid",
            )
        for name in (
            "assets_resolver", "candidate_provider_resolver",
            "baseline_acquirer", "native_reference_loader",
        ):
            _BENCHMARK_RUNTIME._callable(
                copied[name], "benchmark.runtime.resolver_invalid",
            )
        _BENCHMARK_RUNTIME._adapter(
            copied["reviewer"], "review", "benchmark.runtime.reviewer_invalid",
        )
        _BENCHMARK_RUNTIME._adapter(
            copied["native_reference_verifier"],
            "verify",
            "benchmark.runtime.reference_verifier_invalid",
        )
        if (
            not isinstance(copied["blinding_key"], bytes)
            or len(copied["blinding_key"]) < 32
        ):
            raise ValueError
        _BENCHMARK_RUNTIME._CAMPAIGN._identifier(copied["worker_id"])
        if (
            isinstance(copied["max_attempts"], bool)
            or not isinstance(copied["max_attempts"], int)
            or not 1 <= copied["max_attempts"] <= (
                _BENCHMARK_RUNTIME._CAMPAIGN.MAX_ATTEMPTS
            )
        ):
            raise ValueError
    except Exception as error:
        code = getattr(error, "code", "runtime.benchmark.execution.invalid")
        if not isinstance(code, str) or re.fullmatch(
            r"[a-z][a-z0-9_.-]{0,127}", code,
        ) is None:
            code = "runtime.benchmark.execution.invalid"
        raise LocalizationRuntimeBlocked(code) from None
    try:
        copied["lease_seconds"] = _BENCHMARK_RUNTIME._CAMPAIGN._duration(
            copied["lease_seconds"],
        )
        copied["retry_base_seconds"] = (
            _BENCHMARK_RUNTIME._CAMPAIGN._duration(
                copied["retry_base_seconds"], allow_zero=True,
            )
        )
        copied["retry_max_seconds"] = (
            _BENCHMARK_RUNTIME._CAMPAIGN._duration(
                copied["retry_max_seconds"], allow_zero=True,
            )
        )
    except Exception:
        raise LocalizationRuntimeBlocked(
            "runtime.benchmark.execution.retry_policy.invalid",
        ) from None
    if copied["retry_base_seconds"] > copied["retry_max_seconds"]:
        raise LocalizationRuntimeBlocked(
            "runtime.benchmark.execution.retry_policy.invalid",
        )
    return MappingProxyType(copied)


class WebsiteLocalizationRuntime:
    """Compose one host-owned localization runtime from validated capabilities."""

    def __init__(
        self,
        *,
        queue_connection: sqlite3.Connection,
        release_connection: sqlite3.Connection,
        cms_connection: sqlite3.Connection,
        evidence_connection: sqlite3.Connection,
        supervisor_connection: sqlite3.Connection,
        dependencies: Mapping[str, Any],
        supervisor_worker_id: str,
        cms_api_max_attempts: int = 3,
        supervisor_policy: Any = None,
        supervisor_stale_after_seconds: float | int = 30,
        benchmark_connection: sqlite3.Connection | None = None,
        benchmark_policy: Any | None = None,
        benchmark_campaign_id: str | None = None,
        benchmark_evidence_authority: Any | None = None,
        benchmark_stale_after_seconds: float | int = 3600,
        benchmark_execution: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ):
        benchmark_values = (
            benchmark_connection, benchmark_policy, benchmark_campaign_id,
            benchmark_evidence_authority,
        )
        benchmark_enabled = any(value is not None for value in benchmark_values)
        if benchmark_enabled and any(value is None for value in benchmark_values):
            raise LocalizationRuntimeBlocked("runtime.benchmark.incomplete")
        if benchmark_execution is not None and not benchmark_enabled:
            raise LocalizationRuntimeBlocked("runtime.benchmark.incomplete")
        benchmark_execution = _benchmark_execution(benchmark_execution)
        if benchmark_enabled:
            if not isinstance(benchmark_campaign_id, str) or re.fullmatch(
                r"benchmark-campaign-[0-9a-f]{64}", benchmark_campaign_id,
            ) is None:
                raise LocalizationRuntimeBlocked("runtime.benchmark.campaign_id.invalid")
            _capability(
                benchmark_evidence_authority,
                "verify",
                "runtime.benchmark.authority.invalid",
            )
            try:
                benchmark_policy = (
                    _HEALTH._CAMPAIGN._BENCHMARK._validate_policy(benchmark_policy)
                )
                expected_campaign_id = _HEALTH._CAMPAIGN._campaign_identity(
                    benchmark_policy,
                )[0]
            except Exception:
                raise LocalizationRuntimeBlocked("runtime.benchmark.policy.invalid") from None
            if expected_campaign_id != benchmark_campaign_id:
                raise LocalizationRuntimeBlocked("runtime.benchmark.binding.invalid")
            if benchmark_execution is not None:
                _capability(
                    benchmark_evidence_authority,
                    "sign",
                    "runtime.benchmark.authority.invalid",
                )
        _number(
            benchmark_stale_after_seconds,
            "runtime.benchmark.stale_after_seconds.invalid",
            maximum=_HEALTH._CAMPAIGN.MAX_STALE_SECONDS,
        )
        if (
            isinstance(cms_api_max_attempts, bool)
            or not isinstance(cms_api_max_attempts, int)
            or not 1 <= cms_api_max_attempts <= 20
        ):
            raise LocalizationRuntimeBlocked("runtime.cms_api.max_attempts.invalid")
        connections = (
            queue_connection, release_connection, cms_connection,
            evidence_connection, supervisor_connection,
        ) + ((benchmark_connection,) if benchmark_enabled else ()) + (
            (
                benchmark_execution["candidate_connection"],
                benchmark_execution["baseline_connection"],
                benchmark_execution["native_reference_connection"],
                benchmark_execution["review_connection"],
            ) if benchmark_execution is not None else ()
        )
        _validate_connections(connections)
        validated = _validate_dependencies(dependencies)
        supervisor_worker_id = _identifier(
            supervisor_worker_id, "runtime.supervisor_worker_id.invalid",
        )
        if not callable(clock):
            raise LocalizationRuntimeBlocked("runtime.clock.invalid")
        if benchmark_execution is not None:
            try:
                benchmark_now = _BENCHMARK_RUNTIME._CAMPAIGN._timestamp(clock())
            except Exception:
                raise LocalizationRuntimeBlocked("runtime.clock.invalid") from None
            if benchmark_now > benchmark_policy.valid_until:
                raise LocalizationRuntimeBlocked(
                    "runtime.benchmark.validity_expired",
                )
        if token_factory is not None and not callable(token_factory):
            raise LocalizationRuntimeBlocked("runtime.token_factory.invalid")
        supervisor_policy = _supervisor_policy(supervisor_policy)
        _validate_lease_hierarchy(
            validated,
            supervisor_policy,
            benchmark_lease_seconds=(
                benchmark_execution["lease_seconds"]
                if benchmark_execution is not None else None
            ),
        )
        _number(
            supervisor_stale_after_seconds,
            "runtime.supervisor_stale_after_seconds.invalid",
        )

        self._clock = clock
        self.queue = _QUEUE.LocalizationQueue(queue_connection)
        self.release_store = _RELEASE.LocalizationReleaseStore(
            release_connection, self.queue,
        )
        runtime_dependencies = dict(validated)
        runtime_dependencies["result_cache"] = (
            self.release_store.verified_result_cache(
                validated["approval_authority"],
            )
        )
        self._dependencies = MappingProxyType(runtime_dependencies)
        self.bridge = _CMS.WebsiteLocalizationCMSBridge(
            cms_connection, self.queue, self.release_store,
        )
        self.cms_api = _API.WebsiteLocalizationAPI(
            self.bridge,
            validated["event_verifier"],
            clock=self._clock,
            max_attempts=cms_api_max_attempts,
        )
        self.evidence_state = _COORDINATOR.QualityEvidenceStateStore(
            evidence_connection,
        )
        self.benchmark_runtime = None
        self._benchmark_execution = benchmark_execution
        if benchmark_execution is not None:
            try:
                self.benchmark_runtime = (
                    _BENCHMARK_RUNTIME.WebsiteLocalizationBenchmarkRuntime(
                        campaign_connection=benchmark_connection,
                        candidate_connection=(
                            benchmark_execution["candidate_connection"]
                        ),
                        baseline_connection=(
                            benchmark_execution["baseline_connection"]
                        ),
                        native_reference_connection=(
                            benchmark_execution["native_reference_connection"]
                        ),
                        review_connection=benchmark_execution["review_connection"],
                        policy=benchmark_policy,
                        candidate_route_id=(
                            benchmark_execution["candidate_route_id"]
                        ),
                        baseline_route_id=benchmark_execution["baseline_route_id"],
                        native_reference_route_id=(
                            benchmark_execution["native_reference_route_id"]
                        ),
                        reviewer_route_id=(
                            benchmark_execution["reviewer_route_id"]
                        ),
                        assets_resolver=benchmark_execution["assets_resolver"],
                        candidate_provider_resolver=(
                            benchmark_execution["candidate_provider_resolver"]
                        ),
                        baseline_acquirer=(
                            benchmark_execution["baseline_acquirer"]
                        ),
                        native_reference_loader=(
                            benchmark_execution["native_reference_loader"]
                        ),
                        reviewer=benchmark_execution["reviewer"],
                        native_reference_verifier=(
                            benchmark_execution["native_reference_verifier"]
                        ),
                        evidence_authority=benchmark_evidence_authority,
                        blinding_key=benchmark_execution["blinding_key"],
                        worker_id=benchmark_execution["worker_id"],
                        max_attempts=benchmark_execution["max_attempts"],
                        clock=self._clock,
                    )
                )
            except Exception as error:
                code = getattr(error, "code", None)
                if not isinstance(code, str) or re.fullmatch(
                    r"[a-z][a-z0-9_.-]{0,127}", code,
                ) is None:
                    code = "runtime.benchmark.execution.invalid"
                raise LocalizationRuntimeBlocked(code) from None
            if self.benchmark_runtime.campaign_id != benchmark_campaign_id:
                raise LocalizationRuntimeBlocked(
                    "runtime.benchmark.execution.binding.invalid",
                )
        self.benchmark_store = (
            _HEALTH._CAMPAIGN.BenchmarkCampaignStore(benchmark_connection)
            if benchmark_enabled else None
        )
        self._benchmark_policy = benchmark_policy
        self._benchmark_campaign_id = benchmark_campaign_id
        self._benchmark_evidence_authority = benchmark_evidence_authority

        def tick():
            service_tick = _SERVICE.run_service_tick(
                self.bridge,
                self.evidence_state,
                clock=self._clock,
                operation_guard=self.supervisor.renew_active_lease,
                **self._dependencies,
            )
            if (
                service_tick.phase != "idle"
                or service_tick.status != "idle"
                or self.benchmark_runtime is None
            ):
                return service_tick
            try:
                benchmark_tick = self.benchmark_runtime.run_once(
                    operation_guard=self.supervisor.renew_active_lease,
                    lease_seconds=self._benchmark_execution["lease_seconds"],
                    retry_base_seconds=(
                        self._benchmark_execution["retry_base_seconds"]
                    ),
                    retry_max_seconds=(
                        self._benchmark_execution["retry_max_seconds"]
                    ),
                )
            except Exception as error:
                return _SERVICE._runtime_error("benchmark", error)
            if benchmark_tick is None:
                return service_tick
            return _SERVICE._outcome(
                "benchmark",
                benchmark_tick.status,
                job_id=getattr(benchmark_tick, "work_id", None),
                target_locale=getattr(benchmark_tick, "target_locale", None),
                attempt=getattr(benchmark_tick, "attempt", None),
                error_code=getattr(benchmark_tick, "error_code", None),
            )

        self.supervisor = _SUPERVISOR.LocalizationServiceSupervisor(
            supervisor_connection,
            tick,
            worker_id=supervisor_worker_id,
            policy=supervisor_policy,
            clock=self._clock,
            token_factory=token_factory,
        )
        self.health_monitor = _HEALTH.LocalizationHealthMonitor(
            self.bridge,
            self.evidence_state,
            self.supervisor,
            supervisor_stale_after_seconds=supervisor_stale_after_seconds,
            benchmark_store=self.benchmark_store,
            benchmark_policy=benchmark_policy,
            benchmark_campaign_id=benchmark_campaign_id,
            benchmark_evidence_authority=benchmark_evidence_authority,
            benchmark_stale_after_seconds=benchmark_stale_after_seconds,
            benchmark_review_store=(
                self.benchmark_runtime.review_store
                if self.benchmark_runtime is not None else None
            ),
            benchmark_reviewer_route_id=(
                self.benchmark_runtime.reviewer_route_id
                if self.benchmark_runtime is not None else None
            ),
            benchmark_reference_queue=(
                self.benchmark_runtime
                if self.benchmark_runtime is not None
                and "native_reference_queue" in getattr(
                    self.benchmark_runtime, "__dict__", {},
                )
                else None
            ),
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} schema={SCHEMA}>"

    def run_once(self, *, now: float | int | None = None):
        """Run or safely skip one supervised service tick."""
        return self.supervisor.run_once(now=now)

    def run_forever(
        self,
        *,
        stop_requested: Callable[[], bool],
        sleeper: Callable[[float], Any] = time.sleep,
    ):
        """Run until the host requests a stop between service ticks."""
        return self.supervisor.run_forever(
            stop_requested=stop_requested,
            sleeper=sleeper,
        )

    def health(self, *, provider_probe: Any = None, now: float | int | None = None):
        """Return the existing content-free, read-only health report."""
        checked_at = self._clock() if now is None else now
        return self.health_monitor.check(
            event_verifier=self._dependencies["event_verifier"],
            approval_authority=self._dependencies["approval_authority"],
            publication_authority=self._dependencies["publication_authority"],
            provider_probe=provider_probe,
            now=checked_at,
        )

    def load_benchmark_report(
        self, *, now: float | int | None = None,
    ) -> dict[str, Any]:
        """Return the stored, verified benchmark report without mutating it."""
        if self.benchmark_store is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        checked_at = self._clock() if now is None else now
        try:
            return self.benchmark_store.load_report(
                self._benchmark_policy,
                self._benchmark_campaign_id,
                self._benchmark_evidence_authority,
                now=checked_at,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.report.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def benchmark_campaign_status(self) -> dict[str, Any]:
        """Return content-free progress for the configured benchmark campaign."""
        if self.benchmark_store is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        try:
            return self.benchmark_store.status(
                self._benchmark_policy,
                self._benchmark_campaign_id,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.status.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def claim_native_reference_work_order(
        self,
        editor_id: Any,
        **options: Any,
    ):
        """Lease one exact benchmark source to a qualified native editor."""
        if self.benchmark_runtime is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        try:
            return self.benchmark_runtime.claim_native_reference_work_order(
                editor_id, **options,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.reference_queue.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def renew_native_reference_work_order(self, lease: Any, **options: Any):
        """Renew one exact live native-editor lease."""
        if self.benchmark_runtime is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        try:
            return self.benchmark_runtime.renew_native_reference_work_order(
                lease, **options,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.reference_queue.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def native_reference_lease_from_payload(
        self, payload: Any, *, editor_id: Any, target_locale: Any,
    ):
        """Reconstruct one authenticated editor lease without trusting input."""
        if self.benchmark_runtime is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        try:
            return self.benchmark_runtime.native_reference_lease_from_payload(
                payload,
                editor_id=editor_id,
                target_locale=target_locale,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.reference_queue.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def native_reference_http_request_replay(self, **options: Any):
        """Return one exact completed private mutation for safe HTTP retry."""
        if self.benchmark_runtime is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        try:
            return self.benchmark_runtime.native_reference_http_request_replay(
                **options,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.reference_queue.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def accept_native_reference_submission(
        self,
        lease: Any,
        submission: Any,
        **options: Any,
    ):
        """Verify and persist one exact leased qualified-native submission."""
        if self.benchmark_runtime is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        try:
            return self.benchmark_runtime.accept_leased_native_reference_submission(
                lease, submission, **options,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.reference_queue.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def native_reference_queue_status(self):
        """Return content-free editorial progress for the active benchmark."""
        if self.benchmark_runtime is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        try:
            return self.benchmark_runtime.native_reference_queue_status()
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.reference_queue.invalid"
            raise LocalizationRuntimeBlocked(code) from None

    def native_reference_queue_health(
        self,
        *,
        now: float | int | None = None,
        stale_after_seconds: Any = 3600,
    ):
        """Return a read-only, content-free native-reference queue check."""
        if self.benchmark_runtime is None:
            raise LocalizationRuntimeBlocked("runtime.benchmark.unavailable")
        checked_at = self._clock() if now is None else now
        try:
            return self.benchmark_runtime.native_reference_queue_health(
                now=checked_at,
                stale_after_seconds=stale_after_seconds,
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,127}", code,
            ) is None:
                code = "runtime.benchmark.reference_queue.invalid"
            raise LocalizationRuntimeBlocked(code) from None
