#!/usr/bin/env python3
"""Durable source-CMS service for automatic localization lifecycle delivery.

The source website owns three independent SQLite connections and the configured
HTTPS client. One tick performs at most one network operation, prioritizing
removals before new changes and lifecycle polling. Successful change dispatches
are reconciled into the lifecycle monitor before more network work is started,
so a crash between remote acceptance and local registration is recoverable.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA = "blun.cms-source-service-tick.v1"
HEALTH_SCHEMA = "blun.cms-source-service-health.v1"
STATUS_SCHEMA = "blun.cms-source-service-status.v1"
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source service dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_LIFECYCLE = _load_module(
    "blun_website_localization_cms_source_service_lifecycle",
    _ROOT / "integrations" / "website_localization_cms_lifecycle_monitor.py",
)
_DISPATCH = _LIFECYCLE._DISPATCH
_REMOVAL = _load_module(
    "blun_website_localization_cms_source_service_removal",
    _ROOT / "integrations" / "website_localization_cms_removal_dispatch.py",
)


class CMSSourceServiceBlocked(RuntimeError):
    """Stable source-service failure containing no customer content."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            code = "source_service.blocked"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CMSSourceTickOutcome:
    schema: str
    phase: str
    status: str
    operation: str | None = None
    event_id: str | None = None
    request_id: str | None = None
    attempt: int | None = None
    error_code: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CMSSourceServiceHealth:
    schema: str
    status: str
    pending_lifecycle_registrations: int
    changes: Mapping[str, Any]
    removals: Mapping[str, Any]
    lifecycle: Mapping[str, Any]
    error_code: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "pending_lifecycle_registrations": (
                self.pending_lifecycle_registrations
            ),
            "changes": dict(self.changes),
            "removals": dict(self.removals),
            "lifecycle": dict(self.lifecycle),
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class CMSSourceServiceStatus:
    schema: str
    event_id: str
    site_id: str
    website_version: str
    source_sequence: int
    change_sha256: str
    dispatch_status: str
    dispatch_attempts: int
    dispatch_max_attempts: int
    dispatch_error_code: str | None
    plan_id: str | None
    job_count: int | None
    lifecycle_state: str | None
    lifecycle_poll_attempts: int
    lifecycle_error_code: str | None
    remote_status: str | None
    lifecycle_sha256: str | None
    required_locales: tuple[str, ...]
    approved_locales: tuple[str, ...]
    blocked_locales: tuple[tuple[str, str], ...]
    queue_counts: Mapping[str, int]

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "site_id": self.site_id,
            "website_version": self.website_version,
            "source_sequence": self.source_sequence,
            "change_sha256": self.change_sha256,
            "dispatch_status": self.dispatch_status,
            "dispatch_attempts": self.dispatch_attempts,
            "dispatch_max_attempts": self.dispatch_max_attempts,
            "dispatch_error_code": self.dispatch_error_code,
            "plan_id": self.plan_id,
            "job_count": self.job_count,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_poll_attempts": self.lifecycle_poll_attempts,
            "lifecycle_error_code": self.lifecycle_error_code,
            "remote_status": self.remote_status,
            "lifecycle_sha256": self.lifecycle_sha256,
            "required_locales": list(self.required_locales),
            "approved_locales": list(self.approved_locales),
            "blocked_locales": [list(item) for item in self.blocked_locales],
            "queue_counts": dict(self.queue_counts),
        }


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise CMSSourceServiceBlocked(code)
    return value


def _positive_duration(value: Any, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        or float(value) > 86_400
    ):
        raise CMSSourceServiceBlocked(code)
    return float(value)


def _positive_integer(value: Any, code: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise CMSSourceServiceBlocked(code)
    return value


def _safe_code(error: Exception, fallback: str) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else fallback


def _health_payload(value: Any) -> dict[str, Any]:
    try:
        payload = asdict(value)
    except (TypeError, ValueError):
        raise CMSSourceServiceBlocked("source_service.health_invalid") from None
    if (
        not isinstance(payload, dict)
        or payload.get("status") not in {"ok", "blocked"}
    ):
        raise CMSSourceServiceBlocked("source_service.health_invalid")
    return payload


class CMSLocalizationSourceService:
    """Compose durable source outboxes and lifecycle monitoring into one loop."""

    def __init__(
        self,
        change_connection: sqlite3.Connection,
        removal_connection: sqlite3.Connection,
        lifecycle_connection: sqlite3.Connection,
        client: Any,
        *,
        change_worker_id: str,
        removal_worker_id: str,
        lifecycle_worker_id: str,
        clock: Callable[[], float | int] = time.time,
        change_lease_seconds: float | int = 600,
        removal_lease_seconds: float | int = 600,
        lifecycle_lease_seconds: float | int = 600,
        max_lifecycle_failures: int = 5,
        dispatch_base_delay_seconds: float | int = 5,
        dispatch_max_delay_seconds: float | int = 300,
        lifecycle_poll_interval_seconds: float | int = 30,
        lifecycle_base_delay_seconds: float | int = 5,
        lifecycle_max_delay_seconds: float | int = 300,
    ):
        connections = (change_connection, removal_connection, lifecycle_connection)
        if not all(isinstance(item, sqlite3.Connection) for item in connections):
            raise CMSSourceServiceBlocked("source_service.connection_invalid")
        if len({id(item) for item in connections}) != len(connections):
            raise CMSSourceServiceBlocked("source_service.connection_reused")
        methods = (
            "submit_change", "cancel", "request_tombstone", "lifecycle",
        )
        if any(not callable(getattr(client, name, None)) for name in methods):
            raise CMSSourceServiceBlocked("source_service.client_invalid")
        if not callable(clock):
            raise CMSSourceServiceBlocked("source_service.clock_invalid")
        timeout = getattr(client, "timeout", None)
        if (
            timeout is not None
            and (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or float(timeout) <= 0
            )
        ):
            raise CMSSourceServiceBlocked("source_service.client_invalid")

        self.change_worker_id = _identifier(
            change_worker_id, "source_service.worker_invalid",
        )
        self.removal_worker_id = _identifier(
            removal_worker_id, "source_service.worker_invalid",
        )
        self.lifecycle_worker_id = _identifier(
            lifecycle_worker_id, "source_service.worker_invalid",
        )
        self.change_lease_seconds = _positive_duration(
            change_lease_seconds, "source_service.lease_invalid",
        )
        self.removal_lease_seconds = _positive_duration(
            removal_lease_seconds, "source_service.lease_invalid",
        )
        self.lifecycle_lease_seconds = _positive_duration(
            lifecycle_lease_seconds, "source_service.lease_invalid",
        )
        if timeout is not None and any(
            lease <= float(timeout) for lease in (
                self.change_lease_seconds,
                self.removal_lease_seconds,
                self.lifecycle_lease_seconds,
            )
        ):
            raise CMSSourceServiceBlocked("source_service.lease_too_short")
        self.max_lifecycle_failures = _positive_integer(
            max_lifecycle_failures,
            "source_service.max_lifecycle_failures_invalid",
            _LIFECYCLE.MAX_FAILURES,
        )
        self.clock = clock
        self.client = client

        # All configuration is validated before the first schema write.
        for value, code in (
            (dispatch_base_delay_seconds, "source_service.delay_invalid"),
            (dispatch_max_delay_seconds, "source_service.delay_invalid"),
            (lifecycle_poll_interval_seconds, "source_service.delay_invalid"),
            (lifecycle_base_delay_seconds, "source_service.delay_invalid"),
            (lifecycle_max_delay_seconds, "source_service.delay_invalid"),
        ):
            _positive_duration(value, code)
        if float(dispatch_base_delay_seconds) > float(dispatch_max_delay_seconds):
            raise CMSSourceServiceBlocked("source_service.delay_invalid")
        if float(lifecycle_base_delay_seconds) > float(lifecycle_max_delay_seconds):
            raise CMSSourceServiceBlocked("source_service.delay_invalid")

        self.changes = _DISPATCH.DurableCMSChangeDispatcher(
            change_connection,
            base_delay_seconds=dispatch_base_delay_seconds,
            max_delay_seconds=dispatch_max_delay_seconds,
        )
        self.removals = _REMOVAL.DurableCMSRemovalDispatcher(
            removal_connection,
            base_delay_seconds=dispatch_base_delay_seconds,
            max_delay_seconds=dispatch_max_delay_seconds,
        )
        self.lifecycle_monitor = _LIFECYCLE.DurableCMSLifecycleMonitor(
            lifecycle_connection,
            poll_interval_seconds=lifecycle_poll_interval_seconds,
            base_delay_seconds=lifecycle_base_delay_seconds,
            max_delay_seconds=lifecycle_max_delay_seconds,
        )

    def __repr__(self) -> str:
        return "CMSLocalizationSourceService(configured=True)"

    def _now(self) -> float:
        try:
            value = self.clock()
        except Exception:
            raise CMSSourceServiceBlocked("source_service.clock_invalid") from None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise CMSSourceServiceBlocked("source_service.clock_invalid")
        return float(value)

    def enqueue_change(
        self,
        change: Mapping[str, Any],
        *,
        max_attempts: int = 5,
    ) -> Any:
        return self.changes.enqueue(
            change, max_attempts=max_attempts, now=self._now(),
        )

    def enqueue_removal(
        self,
        request: Mapping[str, Any],
        *,
        max_attempts: int = 5,
    ) -> Any:
        return self.removals.enqueue(
            request, max_attempts=max_attempts, now=self._now(),
        )

    def status(self, event_id: str, site_id: str) -> CMSSourceServiceStatus:
        """Return a content-free snapshot without repairing or leasing work."""
        event_id = _identifier(event_id, "source_service.status_invalid")
        site_id = _identifier(site_id, "source_service.status_invalid")
        now = self._now()
        row = self.changes.connection.execute(
            "SELECT * FROM cms_source_change_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise CMSSourceServiceBlocked("source_service.status_not_found")
        try:
            self.changes._validated_row(row)
            change = json.loads(row["payload_json"])
        except Exception as error:
            raise CMSSourceServiceBlocked(
                "source_service.change_state_invalid"
            ) from error
        if (
            not isinstance(change, Mapping)
            or change.get("event_id") != event_id
            or change.get("site_id") != site_id
        ):
            # Deliberately hide whether the event exists for another site.
            raise CMSSourceServiceBlocked("source_service.status_not_found")

        try:
            dispatch = self.changes.status(event_id, now=now)
            lifecycle = self.lifecycle_monitor.status(event_id, now=now)
        except Exception as error:
            if getattr(error, "code", None) == "lifecycle_monitor.event_missing":
                lifecycle = None
            else:
                raise CMSSourceServiceBlocked(
                    "source_service.status_blocked"
                ) from error
        if lifecycle is not None and not self._matching_registration(
            change,
            dispatch,
            lifecycle,
            self.max_lifecycle_failures,
        ):
            raise CMSSourceServiceBlocked(
                "source_service.lifecycle_binding_mismatch"
            )

        return CMSSourceServiceStatus(
            schema=STATUS_SCHEMA,
            event_id=event_id,
            site_id=site_id,
            website_version=change["website_version"],
            source_sequence=change["source_sequence"],
            change_sha256=dispatch.payload_sha256,
            dispatch_status=dispatch.status,
            dispatch_attempts=dispatch.attempts,
            dispatch_max_attempts=dispatch.max_attempts,
            dispatch_error_code=dispatch.last_error_code,
            plan_id=dispatch.remote_plan_id,
            job_count=dispatch.remote_job_count,
            lifecycle_state=None if lifecycle is None else lifecycle.state,
            lifecycle_poll_attempts=(
                0 if lifecycle is None else lifecycle.poll_attempts
            ),
            lifecycle_error_code=(
                None if lifecycle is None else lifecycle.last_error_code
            ),
            remote_status=None if lifecycle is None else lifecycle.remote_status,
            lifecycle_sha256=(
                None if lifecycle is None else lifecycle.lifecycle_sha256
            ),
            required_locales=(
                () if lifecycle is None else lifecycle.required_locales
            ),
            approved_locales=(
                () if lifecycle is None else lifecycle.approved_locales
            ),
            blocked_locales=(
                () if lifecycle is None else lifecycle.blocked_locales
            ),
            queue_counts={} if lifecycle is None else lifecycle.queue_counts,
        )

    @staticmethod
    def _matching_registration(
        change: Mapping[str, Any], dispatch: Any, lifecycle: Any,
        max_lifecycle_failures: int,
    ) -> bool:
        return (
            lifecycle.event_id == dispatch.event_id
            and lifecycle.site_id == change.get("site_id")
            and lifecycle.plan_id == dispatch.remote_plan_id
            and lifecycle.website_version == change.get("website_version")
            and lifecycle.source_sequence == change.get("source_sequence")
            and lifecycle.job_count == dispatch.remote_job_count
            and lifecycle.change_sha256 == dispatch.payload_sha256
            and lifecycle.max_consecutive_failures == max_lifecycle_failures
        )

    def _reconcile_lifecycle(
        self, now: float, *, register: bool,
    ) -> tuple[int, Any | None]:
        pending = 0
        registered = None
        rows = self.changes.connection.execute(
            "SELECT * FROM cms_source_change_outbox "
            "WHERE status = 'succeeded' ORDER BY created_at, event_id"
        ).fetchall()
        for row in rows:
            self.changes._validated_row(row)
            dispatch = self.changes.status(row["event_id"], now=now)
            try:
                change = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError, RecursionError):
                raise CMSSourceServiceBlocked(
                    "source_service.change_state_invalid"
                ) from None
            lifecycle = None
            try:
                lifecycle = self.lifecycle_monitor.status(
                    dispatch.event_id, now=now,
                )
            except Exception as error:
                if getattr(error, "code", None) != "lifecycle_monitor.event_missing":
                    raise
                pending += 1
                if register and registered is None:
                    lifecycle = self.lifecycle_monitor.register(
                        change,
                        dispatch,
                        max_consecutive_failures=self.max_lifecycle_failures,
                        now=now,
                    )
                    registered = lifecycle
                    pending -= 1
            if lifecycle is not None and not self._matching_registration(
                change,
                dispatch,
                lifecycle,
                self.max_lifecycle_failures,
            ):
                raise CMSSourceServiceBlocked(
                    "source_service.lifecycle_binding_mismatch"
                )
        return pending, registered

    @staticmethod
    def _outcome(phase: str, status: str, **values: Any) -> CMSSourceTickOutcome:
        return CMSSourceTickOutcome(SCHEMA, phase, status, **values)

    def run_once(self) -> CMSSourceTickOutcome:
        """Advance at most one external operation and one local handoff."""
        try:
            now = self._now()
        except Exception as error:
            return self._outcome(
                "source", "blocked",
                error_code=_safe_code(error, "source_service.clock_invalid"),
            )

        try:
            removal = self.removals.run_once(
                self.client,
                self.removal_worker_id,
                now=now,
                lease_seconds=self.removal_lease_seconds,
            )
        except Exception as error:
            return self._outcome(
                "removal", "blocked",
                error_code=_safe_code(error, "source_service.removal_blocked"),
            )
        if removal is not None:
            return self._outcome(
                "removal", removal.status,
                operation=removal.operation,
                event_id=removal.event_id,
                request_id=removal.request_id,
                attempt=removal.attempt,
                error_code=removal.error_code,
            )

        try:
            _pending, registered = self._reconcile_lifecycle(now, register=True)
        except Exception as error:
            return self._outcome(
                "lifecycle_registration", "blocked",
                error_code=_safe_code(
                    error, "source_service.lifecycle_registration_blocked",
                ),
            )
        if registered is not None:
            return self._outcome(
                "lifecycle_registration", "registered",
                event_id=registered.event_id,
            )

        try:
            change = self.changes.run_once(
                self.client,
                self.change_worker_id,
                now=now,
                lease_seconds=self.change_lease_seconds,
            )
        except Exception as error:
            return self._outcome(
                "change", "blocked",
                error_code=_safe_code(error, "source_service.change_blocked"),
            )
        if change is not None:
            if change.status == "succeeded":
                try:
                    _pending, registered = self._reconcile_lifecycle(
                        now, register=True,
                    )
                except Exception as error:
                    return self._outcome(
                        "lifecycle_registration", "blocked",
                        event_id=change.event_id,
                        error_code=_safe_code(
                            error,
                            "source_service.lifecycle_registration_blocked",
                        ),
                    )
                if registered is None:
                    return self._outcome(
                        "lifecycle_registration", "blocked",
                        event_id=change.event_id,
                        error_code="source_service.lifecycle_registration_missing",
                    )
            return self._outcome(
                "change", change.status,
                event_id=change.event_id,
                attempt=change.attempt,
                error_code=change.error_code,
            )

        try:
            lifecycle = self.lifecycle_monitor.run_once(
                self.client,
                self.lifecycle_worker_id,
                now=now,
                lease_seconds=self.lifecycle_lease_seconds,
            )
        except Exception as error:
            return self._outcome(
                "lifecycle", "blocked",
                error_code=_safe_code(error, "source_service.lifecycle_blocked"),
            )
        if lifecycle is not None:
            return self._outcome(
                "lifecycle", lifecycle.state,
                event_id=lifecycle.event_id,
                attempt=lifecycle.attempt,
                error_code=lifecycle.error_code,
            )
        return self._outcome("idle", "idle")

    def run_forever(
        self,
        stop: Callable[[], bool],
        *,
        sleeper: Callable[[float], Any] = time.sleep,
        active_delay_seconds: float | int = 0.05,
        idle_delay_seconds: float | int = 1,
        blocked_delay_seconds: float | int = 5,
    ) -> None:
        if not callable(stop) or not callable(sleeper):
            raise CMSSourceServiceBlocked("source_service.loop_invalid")
        active = _positive_duration(
            active_delay_seconds, "source_service.delay_invalid",
        )
        idle = _positive_duration(
            idle_delay_seconds, "source_service.delay_invalid",
        )
        blocked = _positive_duration(
            blocked_delay_seconds, "source_service.delay_invalid",
        )
        while not stop():
            outcome = self.run_once()
            delay = (
                blocked if outcome.status in {"blocked", "failed", "retry_wait"}
                else idle if outcome.status == "idle"
                else active
            )
            sleeper(delay)

    def health(self) -> CMSSourceServiceHealth:
        try:
            now = self._now()
            changes = _health_payload(self.changes.health(now=now))
            removals = _health_payload(self.removals.health(now=now))
            lifecycle = _health_payload(self.lifecycle_monitor.health(now=now))
            pending, _registered = self._reconcile_lifecycle(
                now, register=False,
            )
        except Exception as error:
            return CMSSourceServiceHealth(
                HEALTH_SCHEMA,
                "blocked",
                0,
                {},
                {},
                {},
                _safe_code(error, "source_service.health_blocked"),
            )
        blocked = any(
            part["status"] == "blocked"
            for part in (changes, removals, lifecycle)
        )
        status = "blocked" if blocked else "degraded" if pending else "ok"
        error_code = (
            "source_service.component_blocked" if blocked
            else "source_service.lifecycle_registration_pending"
            if pending
            else None
        )
        return CMSSourceServiceHealth(
            HEALTH_SCHEMA,
            status,
            pending,
            changes,
            removals,
            lifecycle,
            error_code,
        )
