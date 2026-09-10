#!/usr/bin/env python3
"""Read-only, content-free health and readiness monitor for localization.

The monitor validates durable queue and quality-evidence state, authenticated
CMS metadata, signed approvals, publication outbox entries, and provider
availability without ever returning source text, target text, reviewer prose,
or transport exceptions.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


SCHEMA = "blun.website-localization-health.v1"
PROVIDER_HEALTH_SCHEMA = "blun.localization-provider-health.v1"
SUPERVISOR_SCHEMA = "blun.website-localization-supervisor.v1"
SUPERVISOR_PHASES = {
    "tombstone", "delivery", "release", "translation", "idle", "supervisor",
}
SUPERVISOR_STATUSES = {
    "idle", "succeeded", "retry_wait", "failed", "blocked", "approved",
    "delivery_ready", "delivered",
}
QUEUE_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
DELIVERY_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
EVIDENCE_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")


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
_COORDINATOR = _load_module(
    "blun_website_localization_health_coordinator",
    _ROOT / "integrations" / "website_localization_release_coordinator.py",
)
_CAMPAIGN = _load_module(
    "blun_website_localization_health_benchmark_campaign",
    _ROOT / "integrations" / "website_localization_benchmark_campaign.py",
)
_BENCHMARK_REVIEW = _load_module(
    "blun_website_localization_health_benchmark_review",
    _ROOT / "integrations" / "website_localization_benchmark_review_store.py",
)
_REFERENCE_QUEUE = _load_module(
    "blun_website_localization_health_native_reference_queue",
    _ROOT / "integrations" / "website_localization_native_reference_queue.py",
)


class LocalizationHealthBlocked(RuntimeError):
    """Raised only when the monitor itself receives an invalid dependency."""


class ProviderHealthProbe(Protocol):
    def check(self, *, provider_id: str, model_id: str, model_version: str) -> Mapping[str, Any]: ...


class PublisherHealthProbe(Protocol):
    def check(self, *, contract_sha256: str) -> Mapping[str, Any]: ...


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

    def __init__(
        self,
        bridge: Any,
        evidence_state: Any | None = None,
        supervisor: Any | None = None,
        supervisor_stale_after_seconds: float | int = 30,
        benchmark_store: Any | None = None,
        benchmark_policy: Any | None = None,
        benchmark_campaign_id: str | None = None,
        benchmark_evidence_authority: Any | None = None,
        benchmark_stale_after_seconds: float | int = 3600,
        benchmark_review_store: Any | None = None,
        benchmark_reviewer_route_id: str | None = None,
        benchmark_reference_queue: Any | None = None,
    ):
        if not self._supports_bridge(bridge):
            raise LocalizationHealthBlocked("bridge must be WebsiteLocalizationCMSBridge")
        if evidence_state is not None and not isinstance(
            getattr(evidence_state, "connection", None), sqlite3.Connection,
        ):
            raise LocalizationHealthBlocked("evidence state must use SQLite")
        if supervisor is not None and not callable(getattr(supervisor, "status", None)):
            raise LocalizationHealthBlocked("supervisor must expose read-only status")
        benchmark_values = (
            benchmark_store, benchmark_policy, benchmark_campaign_id,
            benchmark_evidence_authority,
        )
        if any(value is not None for value in benchmark_values) and any(
            value is None for value in benchmark_values
        ):
            raise LocalizationHealthBlocked("benchmark configuration is incomplete")
        if benchmark_store is not None and (
            not isinstance(getattr(benchmark_store, "connection", None), sqlite3.Connection)
            or not callable(getattr(benchmark_store, "health", None))
            or not callable(getattr(benchmark_store, "_verify_schema", None))
        ):
            raise LocalizationHealthBlocked("benchmark store is invalid")
        if benchmark_store is not None and not callable(
            getattr(benchmark_evidence_authority, "verify", None),
        ):
            raise LocalizationHealthBlocked("benchmark authority is invalid")
        review_values = (benchmark_review_store, benchmark_reviewer_route_id)
        if any(value is not None for value in review_values) and any(
            value is None for value in review_values
        ):
            raise LocalizationHealthBlocked("benchmark review configuration is incomplete")
        if benchmark_review_store is not None and benchmark_store is None:
            raise LocalizationHealthBlocked("benchmark review requires campaign configuration")
        if benchmark_reference_queue is not None and benchmark_store is None:
            raise LocalizationHealthBlocked(
                "native-reference queue requires campaign configuration",
            )
        if benchmark_review_store is not None and (
            not isinstance(
                getattr(benchmark_review_store, "connection", None),
                sqlite3.Connection,
            )
            or not callable(getattr(benchmark_review_store, "health", None))
            or not callable(getattr(benchmark_review_store, "_verify_schema", None))
            or not isinstance(benchmark_reviewer_route_id, str)
            or _CAMPAIGN._BENCHMARK.IDENTIFIER.fullmatch(
                benchmark_reviewer_route_id,
            ) is None
        ):
            raise LocalizationHealthBlocked("benchmark review store is invalid")
        if benchmark_reference_queue is not None and (
            getattr(
                getattr(benchmark_reference_queue, "native_reference_queue", None),
                "connection",
                None,
            ) is not benchmark_store.connection
            or not callable(getattr(
                benchmark_reference_queue, "native_reference_queue_health", None,
            ))
        ):
            raise LocalizationHealthBlocked("native-reference queue is invalid")
        if (
            isinstance(supervisor_stale_after_seconds, bool)
            or not isinstance(supervisor_stale_after_seconds, (int, float))
            or not math.isfinite(float(supervisor_stale_after_seconds))
            or float(supervisor_stale_after_seconds) <= 0
        ):
            raise LocalizationHealthBlocked("supervisor stale threshold is invalid")
        if (
            isinstance(benchmark_stale_after_seconds, bool)
            or not isinstance(benchmark_stale_after_seconds, (int, float))
            or not math.isfinite(float(benchmark_stale_after_seconds))
            or not 0 < float(benchmark_stale_after_seconds) <= _CAMPAIGN.MAX_STALE_SECONDS
        ):
            raise LocalizationHealthBlocked("benchmark stale threshold is invalid")
        self.bridge = bridge
        self.queue = bridge.queue
        self.release_store = bridge.release_store
        self.evidence_state = evidence_state
        self.supervisor = supervisor
        self.supervisor_stale_after_seconds = float(supervisor_stale_after_seconds)
        self.benchmark_store = benchmark_store
        self.benchmark_policy = benchmark_policy
        self.benchmark_campaign_id = benchmark_campaign_id
        self.benchmark_evidence_authority = benchmark_evidence_authority
        self.benchmark_stale_after_seconds = float(benchmark_stale_after_seconds)
        self.benchmark_review_store = benchmark_review_store
        self.benchmark_reviewer_route_id = benchmark_reviewer_route_id
        self.benchmark_reference_queue = benchmark_reference_queue

    @staticmethod
    def _supports_bridge(bridge: Any) -> bool:
        queue = getattr(bridge, "queue", None)
        release_store = getattr(bridge, "release_store", None)
        return (
            isinstance(getattr(bridge, "connection", None), sqlite3.Connection)
            and isinstance(getattr(queue, "connection", None), sqlite3.Connection)
            and isinstance(getattr(release_store, "connection", None), sqlite3.Connection)
            and all(callable(getattr(bridge, name, None)) for name in (
                "_verify_schema", "_load_event", "delivery_status",
            ))
            and all(callable(getattr(queue, name, None)) for name in (
                "_verify_schema", "plan_counts", "result", "status",
            ))
            and all(callable(getattr(release_store, name, None)) for name in (
                "lookup", "readiness", "validated_result",
            ))
        )

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
        if self.evidence_state is not None:
            try:
                columns = tuple(
                    row["name"]
                    for row in self.evidence_state.connection.execute(
                        "PRAGMA table_info(localization_quality_evidence_state)"
                    )
                )
                if columns != _COORDINATOR._EVIDENCE_COLUMNS:
                    reasons.add("evidence.schema_invalid")
            except Exception:
                reasons.add("evidence.schema_invalid")
        if self.benchmark_store is not None:
            try:
                self.benchmark_store._verify_schema()
            except Exception:
                reasons.add("benchmark.campaign.schema_invalid")
        if self.benchmark_review_store is not None:
            try:
                self.benchmark_review_store._verify_schema()
            except Exception:
                reasons.add("review.store.schema_unsupported")
        connections = [
            ("queue", self.queue.connection),
            ("release", self.release_store.connection),
            ("cms", self.bridge.connection),
        ]
        if self.evidence_state is not None:
            connections.append(("evidence", self.evidence_state.connection))
        if self.benchmark_store is not None:
            connections.append(("benchmark", self.benchmark_store.connection))
        if self.benchmark_review_store is not None:
            connections.append(("benchmark_review", self.benchmark_review_store.connection))
        for name, connection in connections:
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

    def _check_evidence(
        self,
        event_verifier: Any,
        now: float,
    ) -> tuple[dict[str, int], set[str]]:
        counts = {status: 0 for status in EVIDENCE_STATUSES}
        reasons: set[str] = set()
        if self.evidence_state is None:
            return counts, reasons
        connection = self.evidence_state.connection
        counts = _counts(
            connection,
            "localization_quality_evidence_state",
            EVIDENCE_STATUSES,
        )
        event_cache: dict[str, tuple[dict[str, Any], Any]] = {}
        rows = connection.execute(
            "SELECT * FROM localization_quality_evidence_state"
        ).fetchall()
        for row in rows:
            try:
                state = _COORDINATOR.QualityEvidenceStateStore._status_from_row(row)
                superseded = self.bridge.connection.execute(
                    "SELECT 1 FROM cms_event_supersessions WHERE event_id = ?",
                    (state.event_id,),
                ).fetchone() is not None
                cancelled = self.bridge.connection.execute(
                    "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
                    (state.event_id,),
                ).fetchone() is not None
                if state.event_id not in event_cache:
                    event_cache[state.event_id] = self.bridge._load_event(
                        state.event_id,
                        event_verifier,
                        allow_superseded=superseded,
                        allow_cancelled=cancelled,
                    )
                event, plan = event_cache[state.event_id]
                if plan.plan_id != state.plan_id:
                    raise ValueError
                job = next(
                    item for item in plan.jobs if item.job_id == state.job_id
                )
                result, result_sha256 = _COORDINATOR._validated_result(
                    self.release_store, plan, job,
                )
                if result_sha256 != state.result_sha256:
                    raise ValueError
                expected = _COORDINATOR._request(
                    event,
                    plan,
                    job,
                    result,
                    result_sha256,
                    state.evidence_revision,
                )
                if expected.request_id != state.request_id:
                    raise ValueError
                approval = self.release_store.connection.execute("""
                    SELECT 1 FROM localization_approvals
                    WHERE job_id = ? AND result_sha256 = ?
                """, (state.job_id, state.result_sha256)).fetchone()
                if state.status == "succeeded" and approval is None:
                    raise ValueError
                if state.status != "succeeded" and approval is not None:
                    if not superseded:
                        reasons.add("evidence.approval_unreconciled")
                if not superseded:
                    if state.status == "leased" and state.lease_expires_at <= now:
                        reasons.add("evidence.lease_expired")
                    if state.status in {"retry_wait", "failed"}:
                        reasons.add("evidence.error." + state.last_error_code)
                    if state.status == "failed":
                        reasons.add("evidence.review_failed")
            except Exception:
                reasons.add("evidence.state_invalid")
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
        counts["superseded"] = 0
        counts["cancelled"] = 0
        reasons: set[str] = set()
        verify = getattr(authority, "verify", None)
        rows = self.bridge.connection.execute(
            "SELECT * FROM cms_publication_deliveries"
        ).fetchall()
        for row in rows:
            try:
                superseded = self.bridge.connection.execute(
                    "SELECT 1 FROM cms_event_supersessions WHERE event_id = ?",
                    (row["event_id"],),
                ).fetchone() is not None
                cancelled = self.bridge.connection.execute(
                    "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
                    (row["event_id"],),
                ).fetchone() is not None
                if superseded and not cancelled and (
                    row["status"] != "failed" or row["last_error_code"] != "event_superseded"
                ):
                    raise ValueError
                if cancelled and (
                    row["status"] != "failed" or row["last_error_code"] != "event_cancelled"
                ):
                    raise ValueError
                if _hash(row["payload_json"]) != row["payload_sha256"]:
                    raise ValueError
                payload = json.loads(row["payload_json"])
                if _canonical_json(payload) != row["payload_json"]:
                    raise ValueError
                if not isinstance(payload, dict) or set(payload) != {
                    "schema", "delivery_id", "event_id", "site_id", "website_version",
                    "plan_id", "source_id", "source_revision", "source_sequence",
                    "source_sha256",
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
                    if superseded and not cancelled and code == "event_superseded":
                        counts["failed"] -= 1
                        counts["superseded"] += 1
                    elif cancelled and code == "event_cancelled":
                        counts["failed"] -= 1
                        counts["cancelled"] += 1
                    else:
                        reasons.add("cms.delivery.error." + code)
            except Exception:
                reasons.add("cms.delivery.invalid")
        if counts["failed"]:
            reasons.add("cms.delivery.failed")
        return counts, reasons

    def _check_tombstones(
        self,
        event_verifier: Any,
        publication_authority: Any,
        now: float,
    ) -> tuple[dict[str, int], set[str]]:
        counts = _counts(
            self.bridge.connection,
            "cms_tombstone_deliveries",
            DELIVERY_STATUSES,
        )
        reasons: set[str] = set()
        rows = self.bridge.connection.execute(
            "SELECT * FROM cms_tombstone_deliveries"
        ).fetchall()
        for row in rows:
            try:
                request = self.bridge._tombstone_request_from_row(
                    row, event_verifier, publication_authority,
                )
                status = self.bridge.tombstone_status(request.delivery_id)
                if (
                    status.event_id != row["event_id"]
                    or status.plan_id != row["plan_id"]
                    or status.payload_sha256 != request.payload_sha256
                    or not 0 <= status.attempts <= status.max_attempts <= _CMS.MAX_ATTEMPTS
                ):
                    raise ValueError
                if status.status == "leased" and (
                    status.lease_expires_at is None
                    or float(status.lease_expires_at) <= now
                ):
                    reasons.add("cms.tombstone.lease_expired")
                if status.status in {"retry_wait", "failed"}:
                    if (
                        not isinstance(status.last_error_code, str)
                        or _CMS.ERROR_CODE.fullmatch(status.last_error_code) is None
                    ):
                        raise ValueError
                    reasons.add("cms.tombstone.error." + status.last_error_code)
            except Exception:
                reasons.add("cms.tombstone.invalid")
        if counts["failed"]:
            reasons.add("cms.tombstone.failed")
        return counts, reasons

    def _check_supersessions(self, event_verifier: Any) -> set[str]:
        reasons: set[str] = set()
        topic_order: dict[tuple[str, str], list[tuple[int, float]]] = {}
        topic_rows = self.bridge.connection.execute("""
            SELECT event.event_id, event.created_at AS event_created_at,
                   topic.site_id, topic.source_id, topic.generation,
                   topic.created_at AS topic_created_at
            FROM cms_change_events AS event
            LEFT JOIN cms_event_topics AS topic ON topic.event_id = event.event_id
            ORDER BY event.event_id
        """).fetchall()
        for row in topic_rows:
            try:
                event, _ = self.bridge._load_event(
                    row["event_id"],
                    event_verifier,
                    allow_superseded=True,
                    allow_cancelled=True,
                    allow_accepted=True,
                )
                if (
                    row["site_id"] != event["site_id"]
                    or row["source_id"] != event["localization"]["source_id"]
                    or isinstance(row["generation"], bool)
                    or not isinstance(row["generation"], int)
                    or row["generation"] <= 0
                    or row["topic_created_at"] != row["event_created_at"]
                    or (
                        "source_sequence" in event
                        and row["generation"] != event["source_sequence"]
                    )
                ):
                    raise ValueError
                if "source_sequence" not in event:
                    topic_order.setdefault(
                        (row["site_id"], row["source_id"]), [],
                    ).append((int(row["generation"]), float(row["topic_created_at"])))
            except Exception:
                reasons.add("cms.supersession.invalid")
        for values in topic_order.values():
            ordered = sorted(values)
            if [generation for generation, _ in ordered] != list(
                range(1, len(ordered) + 1)
            ) or [created_at for _, created_at in ordered] != sorted(
                created_at for _, created_at in ordered
            ):
                reasons.add("cms.supersession.invalid")
        orphan_topics = self.bridge.connection.execute("""
            SELECT COUNT(*)
            FROM cms_event_topics AS topic
            LEFT JOIN cms_change_events AS event ON event.event_id = topic.event_id
            WHERE event.event_id IS NULL
        """).fetchone()[0]
        missing_supersessions = self.bridge.connection.execute("""
            SELECT COUNT(*)
            FROM cms_event_topics AS older
            JOIN cms_change_events AS older_event ON older_event.event_id = older.event_id
            LEFT JOIN cms_publication_deliveries AS delivery
                ON delivery.event_id = older.event_id AND delivery.status = 'succeeded'
            WHERE older_event.status = 'enqueued' AND delivery.event_id IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM cms_event_topics AS newer
                  JOIN cms_change_events AS newer_event
                    ON newer_event.event_id = newer.event_id
                  WHERE newer.site_id = older.site_id
                    AND newer.source_id = older.source_id
                    AND newer.generation > older.generation
                    AND newer_event.status = 'enqueued'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM cms_event_supersessions AS supersession
                  WHERE supersession.event_id = older.event_id
              )
        """).fetchone()[0]
        if orphan_topics or missing_supersessions:
            reasons.add("cms.supersession.invalid")
        rows = self.bridge.connection.execute("""
            SELECT supersession.event_id, supersession.superseded_by_event_id,
                   older.site_id AS older_site_id, older.source_id AS older_source_id,
                   older.generation AS older_generation,
                   newer.site_id AS newer_site_id, newer.source_id AS newer_source_id,
                   newer.generation AS newer_generation,
                   delivery.status AS delivery_status
            FROM cms_event_supersessions AS supersession
            LEFT JOIN cms_event_topics AS older ON older.event_id = supersession.event_id
            LEFT JOIN cms_event_topics AS newer
                ON newer.event_id = supersession.superseded_by_event_id
            LEFT JOIN cms_publication_deliveries AS delivery
                ON delivery.event_id = supersession.event_id
            ORDER BY supersession.event_id
        """).fetchall()
        for row in rows:
            try:
                if (
                    row["older_site_id"] is None
                    or row["newer_site_id"] is None
                    or row["older_site_id"] != row["newer_site_id"]
                    or row["older_source_id"] != row["newer_source_id"]
                    or int(row["older_generation"]) >= int(row["newer_generation"])
                    or row["delivery_status"] == "succeeded"
                ):
                    raise ValueError
                self.bridge._load_event(
                    row["event_id"], event_verifier, allow_superseded=True,
                    allow_cancelled=True,
                )
                self.bridge._load_event(
                    row["superseded_by_event_id"],
                    event_verifier,
                    allow_superseded=True,
                    allow_cancelled=True,
                )
            except Exception:
                reasons.add("cms.supersession.invalid")
        cancellation_rows = self.bridge.connection.execute("""
            SELECT cancellation.*, delivery.status AS delivery_status
            FROM cms_event_cancellations AS cancellation
            LEFT JOIN cms_publication_deliveries AS delivery
                ON delivery.event_id = cancellation.event_id
            ORDER BY cancellation.cancellation_id
        """).fetchall()
        for row in cancellation_rows:
            try:
                if row["delivery_status"] == "succeeded":
                    raise ValueError
                self.bridge._load_event(
                    row["event_id"],
                    event_verifier,
                    allow_superseded=True,
                    allow_cancelled=True,
                    allow_accepted=True,
                )
            except Exception:
                reasons.add("cms.cancellation.invalid")
        return reasons

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
            cancelled = self.bridge.connection.execute(
                "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone() is not None
            tombstone = self.bridge.connection.execute(
                "SELECT status FROM cms_tombstone_deliveries WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone()
            if row["status"] != "enqueued" and not cancelled:
                reasons.add("cms.event.awaiting_queue_resume")
                continue
            try:
                superseded = self.bridge.connection.execute(
                    "SELECT 1 FROM cms_event_supersessions WHERE event_id = ?",
                    (row["event_id"],),
                ).fetchone() is not None
                event, plan = self.bridge._load_event(
                    row["event_id"], event_verifier,
                    allow_superseded=superseded,
                    allow_cancelled=cancelled,
                    allow_accepted=cancelled,
                )
                localization = event["localization"]
                if not superseded and not cancelled and tombstone is None:
                    providers.add((
                        localization["provider_id"],
                        localization["model_id"],
                        localization["model_version"],
                    ))
                if cancelled and row["status"] == "accepted":
                    queue_counts = {status: 0 for status in QUEUE_STATUSES}
                    queue_counts["cancelled"] = len(plan.jobs)
                else:
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
            if cancelled:
                status = "cancelled"
            elif tombstone is not None and tombstone["status"] == "succeeded":
                status = "deleted"
            elif tombstone is not None and tombstone["status"] == "failed":
                status = "deletion_failed"
            elif tombstone is not None:
                status = "deleting"
            elif superseded:
                status = "superseded"
            elif delivery_status == "succeeded":
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

    def _publisher(self, probe: PublisherHealthProbe | None) -> ComponentHealth | None:
        if probe is None:
            return None
        reasons: set[str] = set()
        try:
            check = getattr(probe, "check", None)
            if not callable(check):
                raise ValueError
            capabilities = self.bridge.localization_capabilities()
            contract = capabilities["publication_http"]
            contract_sha256 = contract["sha256"]
            response = check(contract_sha256=contract_sha256)
            if (
                not isinstance(response, Mapping)
                or set(response) != {
                    "schema", "probe_id", "contract_sha256", "status",
                }
                or response["schema"] != _CMS.PUBLICATION_HEALTH_ACK_SCHEMA
                or not isinstance(response["probe_id"], str)
                or not 16 <= len(response["probe_id"]) <= 256
                or _CMS.TOKEN.fullmatch(response["probe_id"]) is None
                or response["contract_sha256"] != contract_sha256
                or response["status"] != "healthy"
            ):
                raise ValueError
        except Exception:
            reasons.add("cms.publisher_unavailable")
        return _component(
            "cms_publisher",
            "blocked" if reasons else "healthy",
            reasons,
            {
                "configured": 1,
                "healthy": int(not reasons),
                "blocked": int(bool(reasons)),
            },
        )

    def _check_supervisor(self, now: float) -> ComponentHealth | None:
        if self.supervisor is None:
            return None
        counts = {"revision": 0, "consecutive_blocked": 0, "lease_active": 0}
        reasons: set[str] = set()
        status = "blocked"
        try:
            value = self.supervisor.status(now=now)
            payload_method = getattr(value, "as_payload", None)
            payload = payload_method() if callable(payload_method) else value
            expected = {
                "schema", "status", "revision", "lease_active",
                "lease_expires_at", "next_tick_at", "consecutive_blocked",
                "last_started_at", "last_finished_at", "last_phase",
                "last_status", "last_error_code",
            }
            if not isinstance(payload, Mapping) or set(payload) != expected:
                raise ValueError
            if payload["schema"] != SUPERVISOR_SCHEMA or payload["status"] not in {
                "ready", "waiting", "leased", "recoverable",
            }:
                raise ValueError
            if not isinstance(payload["lease_active"], bool):
                raise ValueError
            revision = payload["revision"]
            blocked = payload["consecutive_blocked"]
            if any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (revision, blocked)
            ):
                raise ValueError
            _timestamp(payload["next_tick_at"])
            for name in ("lease_expires_at", "last_started_at", "last_finished_at"):
                if payload[name] is not None:
                    _timestamp(payload[name])
            error_code = payload["last_error_code"]
            if error_code is not None and (
                not isinstance(error_code, str) or _QUEUE.ERROR_CODE.fullmatch(error_code) is None
            ):
                raise ValueError
            last_phase = payload["last_phase"]
            last_status = payload["last_status"]
            if (last_phase is None) != (last_status is None):
                raise ValueError
            if last_phase is not None and (
                last_phase not in SUPERVISOR_PHASES
                or last_status not in SUPERVISOR_STATUSES
            ):
                raise ValueError
            blocked_status = last_status in {"blocked", "failed", "retry_wait"}
            if blocked_status != (error_code is not None):
                raise ValueError
            if (blocked > 0) != blocked_status:
                raise ValueError
            counts = {
                "revision": revision,
                "consecutive_blocked": blocked,
                "lease_active": int(payload["lease_active"]),
            }
            if payload["status"] == "recoverable":
                reasons.add("supervisor.lease_expired")
            if (
                payload["status"] == "ready"
                and now - float(payload["next_tick_at"])
                > self.supervisor_stale_after_seconds
            ):
                reasons.add("supervisor.heartbeat_stale")
            if blocked_status:
                reasons.add("supervisor.last_error." + error_code)
            status = "degraded" if reasons else "healthy"
        except Exception:
            reasons = {"supervisor.state_invalid"}
            status = "blocked"
        return _component("supervisor", status, reasons, counts)

    def _check_benchmark(self, now: float) -> ComponentHealth | None:
        if self.benchmark_store is None:
            return None
        counts = {status: 0 for status in _CAMPAIGN.STATUSES}
        counts.update({"work_count": 0, "report_ready": 0})
        try:
            value = self.benchmark_store.health(
                self.benchmark_policy,
                self.benchmark_campaign_id,
                self.benchmark_evidence_authority,
                now=now,
                stale_after_seconds=self.benchmark_stale_after_seconds,
            )
            payload_method = getattr(value, "as_payload", None)
            payload = payload_method() if callable(payload_method) else value
            expected = {
                "schema", "campaign_id", "status", "reasons", "counts",
                "work_count", "report_ready", "last_progress_at",
            }
            if (
                not isinstance(payload, Mapping)
                or set(payload) != expected
                or payload["schema"] != _CAMPAIGN.HEALTH_SCHEMA
                or payload["campaign_id"] != self.benchmark_campaign_id
                or payload["status"] not in {"healthy", "degraded", "blocked"}
                or not isinstance(payload["reasons"], list)
                or payload["reasons"] != sorted(set(payload["reasons"]))
                or not isinstance(payload["counts"], Mapping)
                or set(payload["counts"]) != set(_CAMPAIGN.STATUSES)
                or not isinstance(payload["report_ready"], bool)
            ):
                raise ValueError
            work_count = payload["work_count"]
            if (
                isinstance(work_count, bool)
                or not isinstance(work_count, int)
                or work_count <= 0
            ):
                raise ValueError
            for name, count in payload["counts"].items():
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError
                counts[name] = count
            if sum(counts[name] for name in _CAMPAIGN.STATUSES) != work_count:
                raise ValueError
            reasons = set(payload["reasons"])
            if any(
                not isinstance(reason, str)
                or _CAMPAIGN.HEALTH_REASON.fullmatch(reason) is None
                or not reason.startswith("benchmark.campaign.")
                for reason in reasons
            ):
                raise ValueError
            progress = payload["last_progress_at"]
            if progress is None or _timestamp(progress) > now:
                raise ValueError
            blocking_reasons = {
                "benchmark.campaign.failed",
                "benchmark.campaign.report_invalid",
                "benchmark.campaign.state_invalid",
            }
            expected_status = (
                "blocked" if counts["failed"] or reasons & blocking_reasons
                else "degraded" if reasons
                else "healthy"
            )
            if payload["status"] != expected_status:
                raise ValueError
            if counts["failed"] and "benchmark.campaign.failed" not in reasons:
                raise ValueError
            if payload["report_ready"] != (
                counts["succeeded"] == work_count
                and not reasons & {
                    "benchmark.campaign.report_invalid",
                    "benchmark.campaign.report_missing",
                }
            ):
                raise ValueError
            counts["work_count"] = work_count
            counts["report_ready"] = int(payload["report_ready"])
            return _component(
                "benchmark_campaign", payload["status"], reasons, counts,
            )
        except Exception:
            return _component(
                "benchmark_campaign",
                "blocked",
                {"benchmark.campaign.state_invalid"},
                counts,
            )

    def _check_benchmark_reviews(self, now: float) -> ComponentHealth | None:
        if self.benchmark_review_store is None:
            return None
        counts = {
            "total": 0,
            "scoped": 0,
            "historical": 0,
            "target_native": 0,
            "source_fidelity": 0,
            "required": 0,
            "matched": 0,
        }
        try:
            rows = self.benchmark_store.connection.execute("""
                SELECT result_json, result_sha256
                FROM benchmark_campaign_work
                WHERE campaign_id = ? AND status = 'succeeded'
                ORDER BY target_locale, suite_case_key
            """, (self.benchmark_campaign_id,)).fetchall()
            expected_passes = []
            for row in rows:
                if (
                    not isinstance(row["result_json"], str)
                    or row["result_sha256"] != _hash(row["result_json"])
                ):
                    raise ValueError
                result = json.loads(row["result_json"])
                if _canonical_json(result) != row["result_json"]:
                    raise ValueError
                validated = _CAMPAIGN._BENCHMARK._validated_case_result(
                    result,
                    self.benchmark_policy,
                    self.benchmark_evidence_authority,
                )
                expected_passes.extend({
                    "phase": item["phase"],
                    "request_sha256": item["request_sha256"],
                    "response_sha256": item["response_sha256"],
                } for item in validated["passes"])
            value = self.benchmark_review_store.health(
                self.benchmark_policy,
                self.benchmark_reviewer_route_id,
                evidence_authority=self.benchmark_evidence_authority,
                expected_passes=tuple(expected_passes),
                now=now,
            )
            payload_method = getattr(value, "as_payload", None)
            payload = payload_method() if callable(payload_method) else value
            expected = {"schema", "route_id", "status", "reasons", "counts"}
            if (
                not isinstance(payload, Mapping)
                or set(payload) != expected
                or payload["schema"] != _BENCHMARK_REVIEW.HEALTH_SCHEMA
                or payload["route_id"] != self.benchmark_reviewer_route_id
                or payload["status"] not in {"healthy", "blocked"}
                or not isinstance(payload["reasons"], list)
                or payload["reasons"] != sorted(set(payload["reasons"]))
                or not isinstance(payload["counts"], Mapping)
                or set(payload["counts"]) != set(counts)
            ):
                raise ValueError
            for name, count in payload["counts"].items():
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError
                counts[name] = count
            reasons = set(payload["reasons"])
            if any(
                not isinstance(reason, str)
                or _CAMPAIGN.HEALTH_REASON.fullmatch(reason) is None
                or not reason.startswith("review.store.")
                for reason in reasons
            ):
                raise ValueError
            if counts["matched"] > counts["required"]:
                raise ValueError
            expected_status = "blocked" if reasons else "healthy"
            if payload["status"] != expected_status:
                raise ValueError
            return _component("benchmark_reviews", expected_status, reasons, counts)
        except Exception:
            return _component(
                "benchmark_reviews",
                "blocked",
                {"review.store.state_invalid"},
                counts,
            )

    def _check_native_reference_queue(
        self, now: float,
    ) -> ComponentHealth | None:
        if self.benchmark_reference_queue is None:
            return None
        counts = {status: 0 for status in _REFERENCE_QUEUE.STATUSES}
        counts["work_count"] = 0
        try:
            value = self.benchmark_reference_queue.native_reference_queue_health(
                now=now,
                stale_after_seconds=self.benchmark_stale_after_seconds,
            )
            payload_method = getattr(value, "as_payload", None)
            payload = payload_method() if callable(payload_method) else value
            expected = {
                "schema", "campaign_id", "status", "reasons", "counts",
                "work_count", "last_progress_at",
            }
            if (
                not isinstance(payload, Mapping)
                or set(payload) != expected
                or payload["schema"] != _REFERENCE_QUEUE.HEALTH_SCHEMA
                or payload["campaign_id"] != self.benchmark_campaign_id
                or payload["status"] not in {"healthy", "degraded", "blocked"}
                or not isinstance(payload["reasons"], list)
                or payload["reasons"] != sorted(set(payload["reasons"]))
                or not isinstance(payload["counts"], Mapping)
                or set(payload["counts"]) != set(_REFERENCE_QUEUE.STATUSES)
            ):
                raise ValueError
            work_count = payload["work_count"]
            if (
                isinstance(work_count, bool) or not isinstance(work_count, int)
                or work_count <= 0
            ):
                raise ValueError
            for name, count in payload["counts"].items():
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError
                counts[name] = count
            if sum(counts[name] for name in _REFERENCE_QUEUE.STATUSES) != work_count:
                raise ValueError
            reasons = set(payload["reasons"])
            if any(
                not isinstance(reason, str)
                or _CAMPAIGN.HEALTH_REASON.fullmatch(reason) is None
                or not reason.startswith("native_reference.queue.")
                for reason in reasons
            ):
                raise ValueError
            progress = payload["last_progress_at"]
            if progress is None or _timestamp(progress) > now:
                raise ValueError
            expected_status = (
                "blocked" if counts["failed"] or any(
                    reason in {
                        "native_reference.queue.policy_expired",
                        "native_reference.queue.failed",
                        "native_reference.queue.state_invalid",
                    } for reason in reasons
                )
                else "degraded" if reasons else "healthy"
            )
            if payload["status"] != expected_status:
                raise ValueError
            counts["work_count"] = work_count
            return _component(
                "benchmark_native_references",
                expected_status,
                reasons,
                counts,
            )
        except Exception:
            return _component(
                "benchmark_native_references",
                "blocked",
                {"native_reference.queue.state_invalid"},
                counts,
            )

    def check(
        self,
        *,
        event_verifier: Any,
        approval_authority: Any,
        publication_authority: Any,
        provider_probe: ProviderHealthProbe | None,
        publisher_probe: PublisherHealthProbe | None = None,
        now: float | int,
    ) -> LocalizationHealthReport:
        now = _timestamp(now)
        storage_reasons = self._check_schemas()
        queue_counts: dict[str, int] = {status: 0 for status in QUEUE_STATUSES}
        approval_counts = {"total": 0, "current": 0, "expired": 0}
        delivery_counts: dict[str, int] = {status: 0 for status in DELIVERY_STATUSES}
        tombstone_counts: dict[str, int] = {status: 0 for status in DELIVERY_STATUSES}
        evidence_counts: dict[str, int] = {status: 0 for status in EVIDENCE_STATUSES}
        workflow_reasons: set[str] = set()
        versions: tuple[WebsiteVersionHealth, ...] = ()
        provider_bindings: set[tuple[str, str, str]] = set()

        if not storage_reasons:
            try:
                queue_counts, queue_reasons = self._check_queue(now)
                evidence_counts, evidence_reasons = self._check_evidence(
                    event_verifier, now,
                )
                approval_counts, approval_reasons = self._check_approvals(
                    approval_authority, now,
                )
                delivery_counts, delivery_reasons = self._check_deliveries(
                    publication_authority, now,
                )
                tombstone_counts, tombstone_reasons = self._check_tombstones(
                    event_verifier, publication_authority, now,
                )
                supersession_reasons = self._check_supersessions(event_verifier)
                versions, event_reasons, provider_bindings = self._versions(
                    event_verifier, approval_authority, now,
                )
                workflow_reasons.update(queue_reasons)
                workflow_reasons.update(evidence_reasons)
                workflow_reasons.update(approval_reasons)
                workflow_reasons.update(delivery_reasons)
                workflow_reasons.update(tombstone_reasons)
                workflow_reasons.update(supersession_reasons)
                workflow_reasons.update(event_reasons)
            except Exception:
                storage_reasons.add("monitor.state_unreadable")

        providers, provider_reasons = self._providers(provider_bindings, provider_probe)
        publisher = self._publisher(publisher_probe)
        supervisor = self._check_supervisor(now)
        benchmark = self._check_benchmark(now)
        benchmark_reviews = self._check_benchmark_reviews(now)
        benchmark_references = self._check_native_reference_queue(now)
        blocking_workflow = {
            "queue.state_invalid",
            "evidence.state_invalid",
            "release.approval_invalid",
            "cms.delivery.invalid",
            "cms.tombstone.invalid",
            "cms.event.invalid",
            "cms.supersession.invalid",
        }
        queue_reasons = {reason for reason in workflow_reasons if reason.startswith("queue.")}
        evidence_reasons = {
            reason for reason in workflow_reasons if reason.startswith("evidence.")
        }
        release_reasons = {reason for reason in workflow_reasons if reason.startswith("release.")}
        cms_reasons = {reason for reason in workflow_reasons if reason.startswith("cms.")}
        components = (
            _component(
                "storage",
                "blocked" if storage_reasons else "healthy",
                storage_reasons,
                {"connections": 3 + int(self.evidence_state is not None) + int(
                    self.benchmark_store is not None
                ) + int(self.benchmark_review_store is not None)},
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
                "evidence",
                "blocked" if evidence_reasons & blocking_workflow else (
                    "degraded" if evidence_reasons else "healthy"
                ),
                evidence_reasons,
                evidence_counts,
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
                {
                    **delivery_counts,
                    **{f"tombstone_{key}": value for key, value in tombstone_counts.items()},
                },
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
        if supervisor is not None:
            components = components + (supervisor,)
        if publisher is not None:
            components = components + (publisher,)
        if benchmark is not None:
            components = components + (benchmark,)
        if benchmark_reviews is not None:
            components = components + (benchmark_reviews,)
        if benchmark_references is not None:
            components = components + (benchmark_references,)
        if (
            storage_reasons
            or provider_reasons
            or (publisher is not None and publisher.status == "blocked")
            or workflow_reasons & blocking_workflow
            or (supervisor is not None and supervisor.status == "blocked")
            or (benchmark is not None and benchmark.status == "blocked")
            or (
                benchmark_reviews is not None
                and benchmark_reviews.status == "blocked"
            )
            or (
                benchmark_references is not None
                and benchmark_references.status == "blocked"
            )
        ):
            status = "blocked"
        elif workflow_reasons or (
            supervisor is not None and supervisor.status == "degraded"
        ) or (
            benchmark is not None and benchmark.status == "degraded"
        ) or (
            benchmark_references is not None
            and benchmark_references.status == "degraded"
        ):
            status = "degraded"
        else:
            status = "healthy"
        return LocalizationHealthReport(now, status, components, providers, versions)
