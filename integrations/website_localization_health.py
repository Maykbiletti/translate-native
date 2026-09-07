#!/usr/bin/env python3
"""Read-only, content-free health and readiness monitor for localization.

The monitor validates durable state, authenticated CMS metadata, signed
approvals, publication outbox entries, and provider availability without ever
returning source text, target text, reviewer prose, or transport exceptions.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


SCHEMA = "blun.website-localization-health.v1"
PROVIDER_HEALTH_SCHEMA = "blun.localization-provider-health.v1"
QUEUE_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
DELIVERY_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required health dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CMS = _load_module(
    "blun_website_localization_health_cms",
    _ROOT / "integrations" / "website_localization_cms.py",
)
_QUEUE = _CMS._QUEUE
_RELEASE = _CMS._RELEASE


class LocalizationHealthBlocked(RuntimeError):
    """Raised only when the monitor itself receives an invalid dependency."""


class ProviderHealthProbe(Protocol):
    def check(self, *, provider_id: str, model_id: str, model_version: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ComponentHealth:
    component: str
    status: str
    reasons: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    model_id: str
    model_version: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class WebsiteVersionHealth:
    event_id: str
    site_id: str
    website_version: str
    plan_id: str
    status: str
    required_locales: int
    approved_locales: int
    queue_counts: tuple[tuple[str, int], ...]
    blocked_locales: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class LocalizationHealthReport:
    checked_at: float
    status: str
    components: tuple[ComponentHealth, ...]
    providers: tuple[ProviderHealth, ...]
    website_versions: tuple[WebsiteVersionHealth, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "checked_at": self.checked_at,
            "status": self.status,
            "components": [
                {
                    "component": item.component,
                    "status": item.status,
                    "reasons": list(item.reasons),
                    "counts": dict(item.counts),
                }
                for item in self.components
            ],
            "providers": [asdict(item) for item in self.providers],
            "website_versions": [
                {
                    **asdict(item),
                    "queue_counts": dict(item.queue_counts),
                    "blocked_locales": [list(blocked) for blocked in item.blocked_locales],
                }
                for item in self.website_versions
            ],
        }


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalizationHealthBlocked("checked_at must be a finite timestamp")
    value = float(value)
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise LocalizationHealthBlocked("checked_at must be a finite timestamp")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _counts(connection: sqlite3.Connection, table: str, statuses: tuple[str, ...]) -> dict[str, int]:
    result = {status: 0 for status in statuses}
    rows = connection.execute(
        f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
    ).fetchall()
    for row in rows:
        if row["status"] not in result:
            raise ValueError("unknown status")
        result[row["status"]] = int(row["count"])
    return result


def _component(name: str, status: str, reasons: set[str], counts: dict[str, int]) -> ComponentHealth:
    return ComponentHealth(
        name,
        status,
        tuple(sorted(reasons)),
        tuple(sorted((key, int(value)) for key, value in counts.items())),
    )


class LocalizationHealthMonitor:
    """Inspect a configured localization bridge without mutating its state."""

    def __init__(self, bridge: Any):
        if not isinstance(bridge, _CMS.WebsiteLocalizationCMSBridge):
            raise LocalizationHealthBlocked("bridge must be WebsiteLocalizationCMSBridge")
        self.bridge = bridge
        self.queue = bridge.queue
        self.release_store = bridge.release_store

    @staticmethod
    def _quick_check(connection: sqlite3.Connection) -> bool:
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        return row is not None and row[0] == "ok"

    def _check_schemas(self) -> set[str]:
        reasons: set[str] = set()
        try:
            self.queue._verify_schema()
        except Exception:
            reasons.add("queue.schema_invalid")
        try:
            columns = tuple(
                row["name"]
                for row in self.release_store.connection.execute(
                    "PRAGMA table_info(localization_approvals)"
                )
            )
            if columns != _RELEASE._COLUMNS:
                reasons.add("release.schema_invalid")
        except Exception:
            reasons.add("release.schema_invalid")
        try:
            self.bridge._verify_schema()
        except Exception:
            reasons.add("cms.schema_invalid")
        for name, connection in (
            ("queue", self.queue.connection),
            ("release", self.release_store.connection),
            ("cms", self.bridge.connection),
        ):
            try:
                if not self._quick_check(connection):
                    reasons.add(f"{name}.database_invalid")
            except Exception:
                reasons.add(f"{name}.database_invalid")
        return reasons

    def _check_queue(self, now: float) -> tuple[dict[str, int], set[str]]:
        reasons: set[str] = set()
        counts = _counts(self.queue.connection, "localization_jobs", QUEUE_STATUSES)
        rows = self.queue.connection.execute("SELECT * FROM localization_jobs").fetchall()
        for row in rows:
            try:
                if _hash(row["payload_json"]) != row["payload_sha256"]:
                    raise ValueError
                payload = json.loads(row["payload_json"])
                if _canonical_json(payload) != row["payload_json"]:
                    raise ValueError
                if payload.get("job_id") != row["job_id"]:
                    raise ValueError
                if payload.get("target", {}).get("locale") != row["target_locale"]:
                    raise ValueError
                if row["status"] == "succeeded":
                    self.queue.result(row["job_id"])
                if row["status"] == "leased" and float(row["lease_expires_at"]) <= now:
                    reasons.add("queue.lease_expired")
                if row["status"] in {"retry_wait", "failed"}:
                    code = row["last_error_code"]
                    if not isinstance(code, str) or _QUEUE.ERROR_CODE.fullmatch(code) is None:
                        raise ValueError
                    reasons.add("queue.error." + code)
            except Exception:
                reasons.add("queue.state_invalid")
        if counts["failed"]:
            reasons.add("queue.locale_failed")
        return counts, reasons

    def _check_approvals(self, authority: Any, now: float) -> tuple[dict[str, int], set[str]]:
        counts = {"total": 0, "current": 0, "expired": 0}
        reasons: set[str] = set()
        rows = self.release_store.connection.execute(
            "SELECT * FROM localization_approvals"
        ).fetchall()
        verify = getattr(authority, "verify", None)
        for row in rows:
            counts["total"] += 1
            try:
                if _hash(row["result_json"]) != row["result_sha256"]:
                    raise ValueError
                if _hash(row["approval_json"]) != row["approval_sha256"]:
                    raise ValueError
                result = json.loads(row["result_json"])
                approval = json.loads(row["approval_json"])
                if _canonical_json(result) != row["result_json"]:
                    raise ValueError
                if _canonical_json(approval) != row["approval_json"]:
                    raise ValueError
                if not isinstance(result, dict) or not isinstance(approval, dict):
                    raise ValueError
                candidate = result.get("candidate")
                if not isinstance(candidate, str) or _hash(candidate) != result.get("target_sha256"):
                    raise ValueError
                result_binding = {
                    "job_id": row["job_id"],
                    "target_locale": row["target_locale"],
                    "target_sha256": row["target_sha256"],
                }
                if any(result.get(key) != value for key, value in result_binding.items()):
                    raise ValueError
                approval_binding = {
                    "approval_id": row["approval_id"],
                    "job_id": row["job_id"],
                    "target_locale": row["target_locale"],
                    "target_sha256": row["target_sha256"],
                    "result_sha256": row["result_sha256"],
                    "approved_at": row["approved_at"],
                    "expires_at": row["expires_at"],
                }
                if any(approval.get(key) != value for key, value in approval_binding.items()):
                    raise ValueError
                for key in (
                    "source_sha256", "target_sha256", "source_locale", "target_locale",
                    "content_type", "glossary_version", "policy_version", "provider",
                    "software_version", "worker_schema",
                ):
                    if approval.get(key) != result.get(key):
                        raise ValueError
                immutable = {
                    key: value
                    for key, value in approval.items()
                    if key not in {"approval_id", "approved_at", "expires_at"}
                }
                if approval["approval_id"] != "blun-l10n-approval-" + _hash(
                    _canonical_json(immutable)
                ):
                    raise ValueError
                signature = _RELEASE._signature(_RELEASE.ApprovalSignature(
                    row["signature_algorithm"], row["key_id"], row["signature"],
                ))
                if not callable(verify) or verify(
                    row["approval_json"].encode("utf-8"), signature,
                ) is not True:
                    raise ValueError
            except Exception:
                reasons.add("release.approval_invalid")
                continue
            if float(row["expires_at"]) <= now:
                counts["expired"] += 1
            else:
                counts["current"] += 1
        return counts, reasons

    def _check_deliveries(
        self,
        authority: Any,
        now: float,
    ) -> tuple[dict[str, int], set[str]]:
        counts = _counts(
            self.bridge.connection,
            "cms_publication_deliveries",
            DELIVERY_STATUSES,
        )
        reasons: set[str] = set()
        verify = getattr(authority, "verify", None)
        rows = self.bridge.connection.execute(
            "SELECT * FROM cms_publication_deliveries"
        ).fetchall()
        for row in rows:
            try:
                if _hash(row["payload_json"]) != row["payload_sha256"]:
                    raise ValueError
                payload = json.loads(row["payload_json"])
                if _canonical_json(payload) != row["payload_json"]:
                    raise ValueError
                if not isinstance(payload, dict) or set(payload) != {
                    "schema", "delivery_id", "event_id", "site_id", "website_version",
                    "plan_id", "source_id", "source_revision", "source_sha256",
                    "localizations",
                }:
                    raise ValueError
                if payload.get("schema") != _CMS.PUBLICATION_SCHEMA:
                    raise ValueError
                if payload.get("delivery_id") != row["delivery_id"]:
                    raise ValueError
                if payload.get("event_id") != row["event_id"] or payload.get("plan_id") != row["plan_id"]:
                    raise ValueError
                unsigned = {key: value for key, value in payload.items() if key != "delivery_id"}
                if payload["delivery_id"] != "blun-cms-delivery-" + _hash(
                    _canonical_json(unsigned)
                ):
                    raise ValueError
                localizations = payload.get("localizations")
                if not isinstance(localizations, list) or not localizations:
                    raise ValueError
                locales: list[str] = []
                for item in localizations:
                    if not isinstance(item, dict) or set(item) != {
                        "locale", "target_text", "target_sha256", "approval_id",
                        "approval_expires_at",
                    }:
                        raise ValueError
                    if not isinstance(item["locale"], str) or not isinstance(item["target_text"], str):
                        raise ValueError
                    if _hash(item["target_text"]) != item["target_sha256"]:
                        raise ValueError
                    locales.append(item["locale"])
                if locales != sorted(set(locales)):
                    raise ValueError
                signature = _CMS._signature(_CMS.CMSMessageSignature(
                    row["signature_algorithm"], row["key_id"], row["signature"],
                ))
                if not callable(verify) or verify(
                    row["payload_json"].encode("utf-8"), signature,
                ) is not True:
                    raise ValueError
                if row["status"] != "succeeded":
                    if any(
                        not isinstance(item.get("approval_expires_at"), (int, float))
                        or isinstance(item.get("approval_expires_at"), bool)
                        or float(item["approval_expires_at"]) <= now
                        for item in localizations
                    ):
                        reasons.add("cms.delivery.approval_expired")
                if row["status"] == "leased" and float(row["lease_expires_at"]) <= now:
                    reasons.add("cms.delivery.lease_expired")
                if row["status"] in {"retry_wait", "failed"}:
                    code = row["last_error_code"]
                    if not isinstance(code, str) or _CMS.ERROR_CODE.fullmatch(code) is None:
                        raise ValueError
                    reasons.add("cms.delivery.error." + code)
            except Exception:
                reasons.add("cms.delivery.invalid")
        if counts["failed"]:
            reasons.add("cms.delivery.failed")
        return counts, reasons

    def _versions(
        self,
        event_verifier: Any,
        approval_authority: Any,
        now: float,
    ) -> tuple[tuple[WebsiteVersionHealth, ...], set[str], set[tuple[str, str, str]]]:
        versions: list[WebsiteVersionHealth] = []
        reasons: set[str] = set()
        providers: set[tuple[str, str, str]] = set()
        rows = self.bridge.connection.execute(
            "SELECT event_id, status FROM cms_change_events ORDER BY event_id"
        ).fetchall()
        for row in rows:
            if row["status"] != "enqueued":
                reasons.add("cms.event.awaiting_queue_resume")
                continue
            try:
                event, plan = self.bridge._load_event(row["event_id"], event_verifier)
                localization = event["localization"]
                providers.add((
                    localization["provider_id"],
                    localization["model_id"],
                    localization["model_version"],
                ))
                queue_counts = self.queue.plan_counts(plan.plan_id)
                readiness = self.release_store.readiness(
                    plan, approval_authority, now=now,
                )
            except Exception:
                reasons.add("cms.event.invalid")
                continue
            delivery = self.bridge.connection.execute(
                "SELECT status FROM cms_publication_deliveries WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            delivery_status = delivery["status"] if delivery is not None else None
            if delivery_status == "succeeded":
                status = "published"
            elif delivery_status == "failed":
                status = "publication_failed"
            elif delivery_status in {"pending", "leased", "retry_wait"}:
                status = "publishing"
            elif readiness.ready:
                status = "ready"
            elif queue_counts["failed"]:
                status = "localization_failed"
            elif queue_counts["succeeded"] == len(plan.jobs):
                status = "awaiting_approval"
            else:
                status = "processing"
            versions.append(WebsiteVersionHealth(
                event_id=event["event_id"],
                site_id=event["site_id"],
                website_version=event["website_version"],
                plan_id=plan.plan_id,
                status=status,
                required_locales=len(readiness.required_locales),
                approved_locales=len(readiness.approved_locales),
                queue_counts=tuple(sorted(queue_counts.items())),
                blocked_locales=readiness.blocked,
            ))
            if any(code == "approval.expired" for _, code in readiness.blocked):
                reasons.add("release.approval_expired")
        return tuple(versions), reasons, providers

    @staticmethod
    def _providers(
        provider_bindings: set[tuple[str, str, str]],
        probe: ProviderHealthProbe | None,
    ) -> tuple[tuple[ProviderHealth, ...], set[str]]:
        statuses: list[ProviderHealth] = []
        reasons: set[str] = set()
        check = getattr(probe, "check", None)
        for provider_id, model_id, model_version in sorted(provider_bindings):
            reason = None
            try:
                if not callable(check):
                    raise ValueError("missing")
                response = check(
                    provider_id=provider_id,
                    model_id=model_id,
                    model_version=model_version,
                )
                expected = {
                    "schema": PROVIDER_HEALTH_SCHEMA,
                    "provider": {
                        "id": provider_id,
                        "model_id": model_id,
                        "model_version": model_version,
                    },
                    "status": "healthy",
                }
                if not isinstance(response, Mapping) or dict(response) != expected:
                    raise ValueError("invalid")
                status = "healthy"
            except Exception:
                status = "blocked"
                reason = "provider.probe_missing" if not callable(check) else "provider.unavailable"
                reasons.add(reason)
            statuses.append(ProviderHealth(
                provider_id, model_id, model_version, status, reason,
            ))
        return tuple(statuses), reasons

    def check(
        self,
        *,
        event_verifier: Any,
        approval_authority: Any,
        publication_authority: Any,
        provider_probe: ProviderHealthProbe | None,
        now: float | int,
    ) -> LocalizationHealthReport:
        now = _timestamp(now)
        storage_reasons = self._check_schemas()
        queue_counts: dict[str, int] = {status: 0 for status in QUEUE_STATUSES}
        approval_counts = {"total": 0, "current": 0, "expired": 0}
        delivery_counts: dict[str, int] = {status: 0 for status in DELIVERY_STATUSES}
        workflow_reasons: set[str] = set()
        versions: tuple[WebsiteVersionHealth, ...] = ()
        provider_bindings: set[tuple[str, str, str]] = set()

        if not storage_reasons:
            try:
                queue_counts, queue_reasons = self._check_queue(now)
                approval_counts, approval_reasons = self._check_approvals(
                    approval_authority, now,
                )
                delivery_counts, delivery_reasons = self._check_deliveries(
                    publication_authority, now,
                )
                versions, event_reasons, provider_bindings = self._versions(
                    event_verifier, approval_authority, now,
                )
                workflow_reasons.update(queue_reasons)
                workflow_reasons.update(approval_reasons)
                workflow_reasons.update(delivery_reasons)
                workflow_reasons.update(event_reasons)
            except Exception:
                storage_reasons.add("monitor.state_unreadable")

        providers, provider_reasons = self._providers(provider_bindings, provider_probe)
        blocking_workflow = {
            "queue.state_invalid",
            "release.approval_invalid",
            "cms.delivery.invalid",
            "cms.event.invalid",
        }
        queue_reasons = {reason for reason in workflow_reasons if reason.startswith("queue.")}
        release_reasons = {reason for reason in workflow_reasons if reason.startswith("release.")}
        cms_reasons = {reason for reason in workflow_reasons if reason.startswith("cms.")}
        components = (
            _component(
                "storage",
                "blocked" if storage_reasons else "healthy",
                storage_reasons,
                {"connections": 3},
            ),
            _component(
                "queue",
                "blocked" if queue_reasons & blocking_workflow else (
                    "degraded" if queue_reasons else "healthy"
                ),
                queue_reasons,
                queue_counts,
            ),
            _component(
                "release",
                "blocked" if release_reasons & blocking_workflow else (
                    "degraded" if release_reasons else "healthy"
                ),
                release_reasons,
                approval_counts,
            ),
            _component(
                "cms",
                "blocked" if cms_reasons & blocking_workflow else (
                    "degraded" if cms_reasons else "healthy"
                ),
                cms_reasons,
                delivery_counts,
            ),
            _component(
                "providers",
                "blocked" if provider_reasons else "healthy",
                provider_reasons,
                {
                    "configured": len(providers),
                    "healthy": sum(item.status == "healthy" for item in providers),
                    "blocked": sum(item.status == "blocked" for item in providers),
                },
            ),
        )
        if storage_reasons or provider_reasons or workflow_reasons & blocking_workflow:
            status = "blocked"
        elif workflow_reasons:
            status = "degraded"
        else:
            status = "healthy"
        return LocalizationHealthReport(now, status, components, providers, versions)
