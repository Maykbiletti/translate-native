#!/usr/bin/env python3
"""One-transition service loop for the website-localization pipeline.

The host supplies every provider, verifier, signer, publisher, clock, and
database-backed store. One tick performs at most one external operation and
returns only content-free operational metadata.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


SCHEMA = "blun.website-localization-service-tick.v1"
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required service dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _load_module(
    "blun_website_localization_service_runner",
    _ROOT / "integrations" / "website_localization_runner.py",
)
_COORDINATOR = _load_module(
    "blun_website_localization_service_coordinator",
    _ROOT / "integrations" / "website_localization_release_coordinator.py",
)
_CMS = _COORDINATOR._CMS


class LocalizationServiceBlocked(RuntimeError):
    """Stable service-state failure that contains no stored value."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ServiceTickOutcome:
    """Content-free state transition produced by one service tick."""

    schema: str
    phase: str
    status: str
    event_id: str | None = None
    plan_id: str | None = None
    job_id: str | None = None
    target_locale: str | None = None
    delivery_id: str | None = None
    attempt: int | None = None
    error_code: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def _outcome(phase: str, status: str, **values: Any) -> ServiceTickOutcome:
    return ServiceTickOutcome(SCHEMA, phase, status, **values)


def _runtime_error(phase: str, error: Exception, **values: Any) -> ServiceTickOutcome:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
        code = f"service.{phase}.blocked"
    return _outcome(phase, "blocked", error_code=code, **values)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise LocalizationServiceBlocked("service.state.identifier_invalid")
    return value


def _event_rows(bridge: Any) -> tuple[Any, ...]:
    try:
        rows = tuple(bridge.connection.execute("""
            SELECT event_id, plan_id
            FROM cms_change_events
            WHERE status = 'enqueued'
              AND event_id NOT IN (
                  SELECT event_id FROM cms_publication_deliveries
                  WHERE status = 'succeeded'
              )
              AND event_id NOT IN (SELECT event_id FROM cms_event_supersessions)
              AND event_id NOT IN (SELECT event_id FROM cms_event_cancellations)
            ORDER BY created_at, event_id
        """).fetchall())
        return tuple(
            (_identifier(row["event_id"]), _identifier(row["plan_id"]))
            for row in rows
        )
    except LocalizationServiceBlocked:
        raise
    except Exception:
        raise LocalizationServiceBlocked("service.state.unavailable") from None


def _accepted_event_rows(bridge: Any) -> tuple[tuple[str, str], ...]:
    try:
        rows = tuple(bridge.connection.execute("""
            SELECT event.event_id, event.plan_id
            FROM cms_change_events AS event
            LEFT JOIN cms_event_cancellations AS cancellation
              ON cancellation.event_id = event.event_id
            WHERE event.status = 'accepted' AND cancellation.event_id IS NULL
            ORDER BY event.created_at, event.event_id
        """).fetchall())
        return tuple(
            (_identifier(row["event_id"]), _identifier(row["plan_id"]))
            for row in rows
        )
    except LocalizationServiceBlocked:
        raise
    except Exception:
        raise LocalizationServiceBlocked("service.state.unavailable") from None


def run_service_tick(
    bridge: Any,
    evidence_state: Any,
    *,
    provider_resolver: Any,
    assets_resolver: Any,
    evidence_provider: Any,
    quality_verifier: Any,
    event_verifier: Any,
    approval_authority: Any,
    publication_authority: Any,
    publisher: Any,
    translation_worker_id: str,
    evidence_worker_id: str,
    delivery_worker_id: str,
    evidence_revision: str,
    clock: Callable[[], float] = time.time,
    translation_lease_seconds: float | int = 300,
    translation_retry_base_seconds: float | int = 5,
    translation_retry_max_seconds: float | int = 3600,
    evidence_lease_seconds: float | int = 300,
    evidence_max_attempts: int = 5,
    approval_ttl_seconds: float | int = 2_592_000,
    delivery_lease_seconds: float | int = 300,
    delivery_max_attempts: int = 5,
    ingress_max_attempts: int = 3,
    human_review_verifier: Any | None = None,
    independent_model_review_verifier: Any | None = None,
    result_cache: Any | None = None,
    operation_guard: Callable[[float], Any] | None = None,
) -> ServiceTickOutcome:
    """Advance the durable pipeline by at most one externally active step.

    Priority is due CMS tombstone, due publication delivery, one persisted
    ingress recovery, one release/evidence transition, then one locale
    translation. Backoff or an active lease in one event does not prevent
    another event from becoming releasable in the same read-only scan.
    """
    if not isinstance(bridge, _CMS.WebsiteLocalizationCMSBridge):
        raise TypeError("bridge must be WebsiteLocalizationCMSBridge")
    if not isinstance(evidence_state, _COORDINATOR.QualityEvidenceStateStore):
        raise TypeError("evidence_state must be QualityEvidenceStateStore")
    if not callable(clock):
        raise TypeError("clock must be callable")
    if operation_guard is not None and not callable(operation_guard):
        raise TypeError("operation_guard must be callable")

    try:
        tombstone = bridge.run_tombstone(
            publisher,
            event_verifier,
            publication_authority,
            worker_id=delivery_worker_id,
            clock=clock,
            lease_seconds=delivery_lease_seconds,
            operation_guard=operation_guard,
        )
    except Exception as error:
        return _runtime_error("tombstone", error)
    if tombstone.status != "idle":
        event_id = plan_id = None
        if tombstone.delivery_id is not None:
            try:
                status = bridge.tombstone_status(tombstone.delivery_id)
                event_id = _identifier(status.event_id)
                plan_id = _identifier(status.plan_id)
            except Exception as error:
                return _runtime_error(
                    "tombstone", error, delivery_id=tombstone.delivery_id,
                )
        return _outcome(
            "tombstone",
            tombstone.status,
            event_id=event_id,
            plan_id=plan_id,
            delivery_id=tombstone.delivery_id,
            attempt=tombstone.attempt,
            error_code=tombstone.error_code,
        )

    try:
        delivery = bridge.run_delivery(
            publisher,
            publication_authority,
            worker_id=delivery_worker_id,
            clock=clock,
            lease_seconds=delivery_lease_seconds,
            operation_guard=operation_guard,
        )
    except Exception as error:
        return _runtime_error("delivery", error)
    if delivery.status != "idle":
        event_id = plan_id = None
        if delivery.delivery_id is not None:
            try:
                status = bridge.delivery_status(delivery.delivery_id)
                event_id = _identifier(status.event_id)
                plan_id = _identifier(status.plan_id)
            except Exception as error:
                return _runtime_error(
                    "delivery", error, delivery_id=delivery.delivery_id,
                )
        return _outcome(
            "delivery",
            delivery.status,
            event_id=event_id,
            plan_id=plan_id,
            delivery_id=delivery.delivery_id,
            attempt=delivery.attempt,
            error_code=delivery.error_code,
        )

    try:
        accepted_events = _accepted_event_rows(bridge)
    except Exception as error:
        return _runtime_error("ingress", error)
    if accepted_events:
        event_id, plan_id = accepted_events[0]
        try:
            resumed = bridge.resume_accepted_change(
                event_id,
                event_verifier,
                max_attempts=ingress_max_attempts,
                now=clock(),
            )
        except Exception as error:
            return _runtime_error(
                "ingress", error, event_id=event_id, plan_id=plan_id,
            )
        if resumed is not None:
            return _outcome(
                "ingress",
                resumed.status,
                event_id=resumed.event_id,
                plan_id=resumed.plan_id,
            )

    try:
        events = _event_rows(bridge)
    except Exception as error:
        return _runtime_error("release", error)
    for event_id, plan_id in events:
        try:
            had_delivery = bridge.connection.execute(
                "SELECT 1 FROM cms_publication_deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone() is not None
        except Exception as error:
            return _runtime_error(
                "release", error, event_id=event_id, plan_id=plan_id,
            )
        try:
            release = _COORDINATOR.run_next_release(
                bridge,
                event_id,
                event_verifier,
                evidence_provider,
                quality_verifier,
                approval_authority,
                publication_authority,
                evidence_state=evidence_state,
                evidence_revision=evidence_revision,
                evidence_worker_id=evidence_worker_id,
                evidence_lease_seconds=evidence_lease_seconds,
                evidence_max_attempts=evidence_max_attempts,
                approval_ttl_seconds=approval_ttl_seconds,
                delivery_max_attempts=delivery_max_attempts,
                human_review_verifier=human_review_verifier,
                independent_model_review_verifier=independent_model_review_verifier,
                operation_guard=operation_guard,
                clock=clock,
            )
        except Exception as error:
            return _runtime_error(
                "release", error, event_id=event_id, plan_id=plan_id,
            )
        if release.status == "waiting":
            continue
        if release.status == "delivery_ready" and had_delivery:
            continue
        return _outcome(
            "release",
            release.status,
            event_id=release.event_id,
            plan_id=release.plan_id,
            job_id=release.job_id,
            target_locale=release.target_locale,
            delivery_id=release.delivery_id,
        )

    try:
        translation = _RUNNER.run_next_localization_job(
            bridge.queue,
            translation_worker_id,
            provider_resolver,
            assets_resolver,
            clock=clock,
            lease_seconds=translation_lease_seconds,
            retry_base_seconds=translation_retry_base_seconds,
            retry_max_seconds=translation_retry_max_seconds,
            result_cache=result_cache,
            eligible_plan_ids=tuple(plan_id for _, plan_id in events),
            operation_guard=operation_guard,
        )
    except Exception as error:
        return _runtime_error("translation", error)
    if translation is None:
        return _outcome("idle", "idle")
    return _outcome(
        "translation",
        translation.status,
        job_id=translation.job_id,
        target_locale=translation.target_locale,
        attempt=translation.attempt,
        error_code=translation.error_code,
    )
