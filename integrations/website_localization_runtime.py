#!/usr/bin/env python3
"""Provider-neutral composition root for the website-localization service.

The host owns five distinct SQLite connections and every external capability.
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
    "human_review_verifier", "result_cache",
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
    if len(connections) != 5 or any(
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
) -> None:
    longest_operation_lease = max(
        float(dependencies.get(name, default))
        for name, default in OPERATION_LEASE_DEFAULTS.items()
    )
    if float(supervisor_policy.lease_seconds) <= longest_operation_lease:
        raise LocalizationRuntimeBlocked("runtime.lease_hierarchy.invalid")


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
        supervisor_policy: Any = None,
        supervisor_stale_after_seconds: float | int = 30,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ):
        connections = (
            queue_connection, release_connection, cms_connection,
            evidence_connection, supervisor_connection,
        )
        _validate_connections(connections)
        validated = _validate_dependencies(dependencies)
        supervisor_worker_id = _identifier(
            supervisor_worker_id, "runtime.supervisor_worker_id.invalid",
        )
        if not callable(clock):
            raise LocalizationRuntimeBlocked("runtime.clock.invalid")
        if token_factory is not None and not callable(token_factory):
            raise LocalizationRuntimeBlocked("runtime.token_factory.invalid")
        supervisor_policy = _supervisor_policy(supervisor_policy)
        _validate_lease_hierarchy(validated, supervisor_policy)
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
        self.evidence_state = _COORDINATOR.QualityEvidenceStateStore(
            evidence_connection,
        )

        def tick():
            return _SERVICE.run_service_tick(
                self.bridge,
                self.evidence_state,
                clock=self._clock,
                operation_guard=self.supervisor.renew_active_lease,
                **self._dependencies,
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
