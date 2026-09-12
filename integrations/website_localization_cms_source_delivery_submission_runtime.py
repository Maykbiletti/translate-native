#!/usr/bin/env python3
"""Owned website runtime for durable HMAC-authenticated sidecar submission."""

from __future__ import annotations

import copy
import importlib.util
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"cannot load source-delivery submission dependency: {path.name}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_AUTH = _load_module(
    "blun_website_localization_cms_source_delivery_submission_auth",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_auth.py",
)
_ADAPTER = _load_module(
    "blun_website_localization_cms_source_delivery_submission_adapter",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_sidecar_adapter.py",
)
_RUNTIME = _load_module(
    "blun_website_localization_cms_source_delivery_submission_outbox",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_runtime.py",
)

HMACCredential = _AUTH.HMACCredential


class HMACCMSSourceDeliverySubmissionRuntimeBlocked(RuntimeError):
    """Stable composition failure without credentials or website content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _blocked(code: str) -> HMACCMSSourceDeliverySubmissionRuntimeBlocked:
    return HMACCMSSourceDeliverySubmissionRuntimeBlocked(
        "source_delivery_submission_runtime." + code
    )


@dataclass(frozen=True)
class HMACCMSSourceDeliverySubmissionStatus:
    """Content-free progress across website and sidecar acceptance queues."""

    schema: str
    operation: str
    request_id: str
    event_id: str
    site_id: str
    payload_sha256: str
    status: str
    stage: str
    website_status: str
    website_attempts: int
    website_delivery_max_attempts: int
    sidecar_status: str | None
    sidecar_attempts: int | None
    sidecar_delivery_max_attempts: int
    source_max_attempts: int
    next_attempt_at: float
    lease_expired: bool
    error_code: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "operation": self.operation,
            "request_id": self.request_id,
            "event_id": self.event_id,
            "site_id": self.site_id,
            "payload_sha256": self.payload_sha256,
            "status": self.status,
            "stage": self.stage,
            "website_status": self.website_status,
            "website_attempts": self.website_attempts,
            "website_delivery_max_attempts": (
                self.website_delivery_max_attempts
            ),
            "sidecar_status": self.sidecar_status,
            "sidecar_attempts": self.sidecar_attempts,
            "sidecar_delivery_max_attempts": (
                self.sidecar_delivery_max_attempts
            ),
            "source_max_attempts": self.source_max_attempts,
            "next_attempt_at": self.next_attempt_at,
            "lease_expired": self.lease_expired,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class HMACCMSSourceDeliverySubmissionReadiness:
    """Content-free readiness across both durable submission workers."""

    schema: str
    status: str
    website_status: str
    website_worker_state: str
    website_outbox_status: str | None
    website_error_code: str | None
    sidecar_status: str | None
    sidecar_worker_state: str | None
    sidecar_outbox_status: str | None
    sidecar_error_code: str | None
    sidecar_capabilities_sha256: str
    source_capabilities_sha256: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "website_status": self.website_status,
            "website_worker_state": self.website_worker_state,
            "website_outbox_status": self.website_outbox_status,
            "website_error_code": self.website_error_code,
            "sidecar_status": self.sidecar_status,
            "sidecar_worker_state": self.sidecar_worker_state,
            "sidecar_outbox_status": self.sidecar_outbox_status,
            "sidecar_error_code": self.sidecar_error_code,
            "sidecar_capabilities_sha256": self.sidecar_capabilities_sha256,
            "source_capabilities_sha256": self.source_capabilities_sha256,
        }


@dataclass(frozen=True)
class HMACCMSSourceDeliverySubmissionPipelineReadiness:
    """Content-free intake and source-processing readiness kept separate."""

    schema: str
    status: str
    intake_readiness: Mapping[str, Any]
    source_readiness: Mapping[str, Any] | None
    source_capability_binding: Mapping[str, Any] | None
    sidecar_capabilities_sha256: str
    source_capabilities_sha256: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "intake_readiness": copy.deepcopy(dict(self.intake_readiness)),
            "source_readiness": (
                None
                if self.source_readiness is None
                else copy.deepcopy(dict(self.source_readiness))
            ),
            "source_capability_binding": (
                None
                if self.source_capability_binding is None
                else copy.deepcopy(dict(self.source_capability_binding))
            ),
            "sidecar_capabilities_sha256": (
                self.sidecar_capabilities_sha256
            ),
            "source_capabilities_sha256": (
                self.source_capabilities_sha256
            ),
        }


@dataclass(frozen=True)
class HMACCMSSourceDeliverySubmissionHealth:
    """Content-free health across both durable submission outboxes."""

    schema: str
    status: str
    website_health: Mapping[str, Any]
    sidecar_health: Mapping[str, Any]
    sidecar_capabilities_sha256: str
    source_capabilities_sha256: str

    @staticmethod
    def _copy_health(value: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(value)
        result["counts"] = dict(value["counts"])
        result["operations"] = dict(value["operations"])
        return result

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "website_health": self._copy_health(self.website_health),
            "sidecar_health": self._copy_health(self.sidecar_health),
            "sidecar_capabilities_sha256": self.sidecar_capabilities_sha256,
            "source_capabilities_sha256": self.source_capabilities_sha256,
        }


@dataclass(frozen=True)
class HMACCMSSourceDeliverySubmissionPipelineHealth:
    """Content-free intake and source queue health kept separate."""

    schema: str
    status: str
    intake_health: Mapping[str, Any]
    source_health: Mapping[str, Any] | None
    source_capability_binding: Mapping[str, Any] | None
    sidecar_capabilities_sha256: str
    source_capabilities_sha256: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "intake_health": copy.deepcopy(dict(self.intake_health)),
            "source_health": (
                None
                if self.source_health is None
                else copy.deepcopy(dict(self.source_health))
            ),
            "source_capability_binding": (
                None
                if self.source_capability_binding is None
                else copy.deepcopy(dict(self.source_capability_binding))
            ),
            "sidecar_capabilities_sha256": (
                self.sidecar_capabilities_sha256
            ),
            "source_capabilities_sha256": (
                self.source_capabilities_sha256
            ),
        }


@dataclass(frozen=True)
class HMACCMSSourceDeliverySubmissionLifecycle:
    """Content-free acceptance and source localization state kept separate."""

    schema: str
    status: str
    stage: str
    submission: Mapping[str, Any]
    source_status: Mapping[str, Any] | None
    source_capability_binding: Mapping[str, Any] | None
    sidecar_capabilities_sha256: str
    source_capabilities_sha256: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "stage": self.stage,
            "submission": copy.deepcopy(dict(self.submission)),
            "source_status": (
                None
                if self.source_status is None
                else copy.deepcopy(dict(self.source_status))
            ),
            "source_capability_binding": (
                None
                if self.source_capability_binding is None
                else copy.deepcopy(dict(self.source_capability_binding))
            ),
            "sidecar_capabilities_sha256": (
                self.sidecar_capabilities_sha256
            ),
            "source_capabilities_sha256": (
                self.source_capabilities_sha256
            ),
        }


class HMACCMSSourceDeliverySubmissionRuntime:
    """Own one signer, sidecar client, adapter, outbox, and optional worker."""

    def __init__(self, client: Any, adapter: Any, delivery: Any):
        self._owner_pid = os.getpid()
        self._client = client
        self._adapter = adapter
        self._delivery = delivery

    def __repr__(self) -> str:
        return (
            "HMACCMSSourceDeliverySubmissionRuntime"
            f"(state={self.state!r}, worker_state={self.worker_state!r})"
        )

    def __enter__(self) -> "HMACCMSSourceDeliverySubmissionRuntime":
        self._assert_open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _assert_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise _blocked("foreign_process")

    def _assert_open(self) -> None:
        self._assert_owner()
        if self._delivery.state != "open":
            raise _blocked("closed")

    def replace_credential(self, credential: HMACCredential) -> None:
        """Atomically replace the signer used by every sidecar operation."""

        self._assert_open()
        self._client.replace_credential(credential)

    def enqueue_change(
        self,
        change: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
    ) -> Any:
        self._assert_open()
        return self._delivery.enqueue_change(
            change,
            source_max_attempts=source_max_attempts,
            delivery_max_attempts=delivery_max_attempts,
        )

    def enqueue_removal(
        self,
        removal: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
    ) -> Any:
        self._assert_open()
        return self._delivery.enqueue_removal(
            removal,
            source_max_attempts=source_max_attempts,
            delivery_max_attempts=delivery_max_attempts,
        )

    def status(self, operation: str, request_id: str) -> Any:
        """Return only the local website-to-sidecar acceptance state."""

        self._assert_open()
        return self._delivery.status(operation, request_id)

    def submission_status(
        self, operation: str, request_id: str,
    ) -> HMACCMSSourceDeliverySubmissionStatus:
        """Project exact progress through both durable acceptance queues."""

        self._assert_open()
        local = self._delivery.status(operation, request_id)
        if local.capabilities_sha256 != self.expected_capabilities_sha256:
            raise _blocked("status_invalid")
        if local.status != "succeeded":
            failed = local.status == "failed"
            return HMACCMSSourceDeliverySubmissionStatus(
                schema="blun.cms-source-delivery-submission-status.v1",
                operation=local.operation,
                request_id=local.request_id,
                event_id=local.event_id,
                site_id=local.site_id,
                payload_sha256=local.payload_sha256,
                status="failed" if failed else "pending",
                stage="website_acceptance",
                website_status=local.status,
                website_attempts=local.attempts,
                website_delivery_max_attempts=local.delivery_max_attempts,
                sidecar_status=None,
                sidecar_attempts=None,
                sidecar_delivery_max_attempts=(
                    self._adapter.sidecar_delivery_max_attempts
                ),
                source_max_attempts=local.source_max_attempts,
                next_attempt_at=local.next_attempt_at,
                lease_expired=local.lease_expired,
                error_code=local.last_error_code,
            )

        response = self._client.status(
            local.operation,
            local.request_id,
            local.event_id,
            local.site_id,
            local.payload_sha256,
        )
        try:
            if (
                not isinstance(response, Mapping)
                or set(response)
                != {"schema", "status", "capabilities_sha256"}
                or response.get("schema")
                != _AUTH._HTTP.STATUS_RESPONSE_SCHEMA
                or response.get("capabilities_sha256")
                != self._client.expected_capabilities_sha256
            ):
                raise ValueError
            sidecar = _AUTH._CLIENT._status_payload(
                response["status"],
                expected_operation=local.operation,
                expected_request_id=local.request_id,
                expected_event_id=local.event_id,
                expected_site_id=local.site_id,
                expected_payload_sha256=local.payload_sha256,
                expected_remote_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )
            if (
                sidecar["delivery_max_attempts"]
                != self._adapter.sidecar_delivery_max_attempts
                or sidecar["source_max_attempts"]
                != local.source_max_attempts
            ):
                raise ValueError
        except Exception:
            raise _blocked("status_invalid") from None

        accepted = sidecar["status"] == "succeeded"
        failed = sidecar["status"] == "failed"
        return HMACCMSSourceDeliverySubmissionStatus(
            schema="blun.cms-source-delivery-submission-status.v1",
            operation=local.operation,
            request_id=local.request_id,
            event_id=local.event_id,
            site_id=local.site_id,
            payload_sha256=local.payload_sha256,
            status="accepted" if accepted else "failed" if failed else "pending",
            stage="source_acceptance" if accepted else "sidecar_delivery",
            website_status=local.status,
            website_attempts=local.attempts,
            website_delivery_max_attempts=local.delivery_max_attempts,
            sidecar_status=sidecar["status"],
            sidecar_attempts=sidecar["attempts"],
            sidecar_delivery_max_attempts=sidecar["delivery_max_attempts"],
            source_max_attempts=sidecar["source_max_attempts"],
            next_attempt_at=float(sidecar["next_attempt_at"]),
            lease_expired=sidecar["lease_expired"],
            error_code=sidecar["last_error_code"],
        )

    def submission_lifecycle(
        self, operation: str, request_id: str,
    ) -> HMACCMSSourceDeliverySubmissionLifecycle:
        """Read acceptance first, then the complete source localization state."""

        self._assert_open()
        submission = self.submission_status(operation, request_id)
        submission_payload = submission.as_payload()
        if submission.status != "accepted":
            return HMACCMSSourceDeliverySubmissionLifecycle(
                schema="blun.cms-source-delivery-submission-lifecycle.v2",
                status=submission.status,
                stage=submission.stage,
                submission=submission_payload,
                source_status=None,
                source_capability_binding=None,
                sidecar_capabilities_sha256=(
                    self._client.expected_capabilities_sha256
                ),
                source_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )

        try:
            response = self._client.source_status(
                submission.event_id,
                submission.site_id,
                submission.payload_sha256,
            )
        except Exception:
            raise _blocked("lifecycle_unavailable") from None
        try:
            if (
                not isinstance(response, Mapping)
                or set(response) != {
                    "schema", "source_status",
                    "source_capabilities_sha256", "capabilities_sha256",
                    "source_capability_binding",
                }
                or response.get("schema")
                != _AUTH._HTTP.SOURCE_STATUS_RESPONSE_SCHEMA
                or response.get("capabilities_sha256")
                != self._client.expected_capabilities_sha256
                or response.get("source_capabilities_sha256")
                != self._client.expected_remote_capabilities_sha256
            ):
                raise ValueError
            source_status = _AUTH._HTTP._source_status_response(
                {
                    "schema": _AUTH._HTTP._SOURCE_HTTP.STATUS_RESPONSE_SCHEMA,
                    "status": response["source_status"],
                    "capability_binding": response[
                        "source_capability_binding"
                    ],
                    "capabilities_sha256": (
                        response["source_capabilities_sha256"]
                    ),
                },
                expected_event_id=submission.event_id,
                expected_site_id=submission.site_id,
                expected_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )
            if (
                source_status["status"] != response["source_status"]
                or source_status["capability_binding"]
                != response["source_capability_binding"]
            ):
                raise ValueError
        except Exception:
            raise _blocked("lifecycle_invalid") from None

        source_capability_binding = source_status["capability_binding"]
        source_status = source_status["status"]
        remote_status = source_status["remote_status"]
        if remote_status is not None:
            status = remote_status
            stage = "localization_lifecycle"
        elif source_status["dispatch_status"] == "failed":
            status = "failed"
            stage = "source_processing"
        else:
            status = "accepted"
            stage = "source_processing"
        return HMACCMSSourceDeliverySubmissionLifecycle(
            schema="blun.cms-source-delivery-submission-lifecycle.v2",
            status=status,
            stage=stage,
            submission=submission_payload,
            source_status=source_status,
            source_capability_binding=source_capability_binding,
            sidecar_capabilities_sha256=(
                self._client.expected_capabilities_sha256
            ),
            source_capabilities_sha256=(
                self._client.expected_remote_capabilities_sha256
            ),
        )

    def health(self) -> Any:
        self._assert_open()
        return self._delivery.health()

    def submission_health(self) -> HMACCMSSourceDeliverySubmissionHealth:
        """Project exact health across both durable acceptance outboxes."""

        self._assert_open()
        try:
            website = _AUTH._HTTP._health_payload(self._delivery.health())
        except Exception:
            raise _blocked("health_invalid") from None

        response = self._client.health()
        try:
            if (
                not isinstance(response, Mapping)
                or set(response)
                != {"schema", "health", "capabilities_sha256"}
                or response.get("schema") != _AUTH._HTTP.HEALTH_RESPONSE_SCHEMA
                or response.get("capabilities_sha256")
                != self._client.expected_capabilities_sha256
            ):
                raise ValueError
            sidecar = _AUTH._HTTP._health_payload(
                _AUTH._CLIENT._PayloadView(response["health"]),
            )
            if sidecar != response["health"]:
                raise ValueError
        except Exception:
            raise _blocked("health_invalid") from None

        return HMACCMSSourceDeliverySubmissionHealth(
            schema="blun.cms-source-delivery-submission-health.v1",
            status=(
                "ok"
                if website["status"] == sidecar["status"] == "ok"
                else "blocked"
            ),
            website_health=website,
            sidecar_health=sidecar,
            sidecar_capabilities_sha256=(
                self._client.expected_capabilities_sha256
            ),
            source_capabilities_sha256=(
                self._client.expected_remote_capabilities_sha256
            ),
        )

    def submission_pipeline_health(
        self,
    ) -> HMACCMSSourceDeliverySubmissionPipelineHealth:
        """Project exact health through intake and source processing."""

        self._assert_open()
        intake = self.submission_health()
        intake_payload = intake.as_payload()
        if intake.status != "ok":
            return HMACCMSSourceDeliverySubmissionPipelineHealth(
                schema="blun.cms-source-delivery-submission-pipeline-health.v2",
                status="blocked",
                intake_health=intake_payload,
                source_health=None,
                source_capability_binding=None,
                sidecar_capabilities_sha256=(
                    self._client.expected_capabilities_sha256
                ),
                source_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )

        try:
            response = self._client.source_health()
        except Exception:
            raise _blocked("pipeline_health_unavailable") from None
        try:
            if (
                not isinstance(response, Mapping)
                or set(response) != {
                    "schema", "source_health",
                    "source_capabilities_sha256", "capabilities_sha256",
                    "source_capability_binding",
                }
                or response.get("schema")
                != _AUTH._HTTP.SOURCE_HEALTH_RESPONSE_SCHEMA
                or response.get("capabilities_sha256")
                != self._client.expected_capabilities_sha256
                or response.get("source_capabilities_sha256")
                != self._client.expected_remote_capabilities_sha256
            ):
                raise ValueError
            source_health = _AUTH._HTTP._source_health_response(
                {
                    "schema": _AUTH._HTTP._SOURCE_HTTP.HEALTH_RESPONSE_SCHEMA,
                    "health": response["source_health"],
                    "capability_binding": response[
                        "source_capability_binding"
                    ],
                    "capabilities_sha256": (
                        response["source_capabilities_sha256"]
                    ),
                },
                expected_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )
            if (
                source_health["health"] != response["source_health"]
                or source_health["capability_binding"]
                != response["source_capability_binding"]
            ):
                raise ValueError
        except Exception:
            raise _blocked("pipeline_health_invalid") from None

        return HMACCMSSourceDeliverySubmissionPipelineHealth(
            schema="blun.cms-source-delivery-submission-pipeline-health.v2",
            status=source_health["health"]["status"],
            intake_health=intake_payload,
            source_health=source_health["health"],
            source_capability_binding=source_health["capability_binding"],
            sidecar_capabilities_sha256=(
                self._client.expected_capabilities_sha256
            ),
            source_capabilities_sha256=(
                self._client.expected_remote_capabilities_sha256
            ),
        )

    def submission_readiness(
        self,
    ) -> HMACCMSSourceDeliverySubmissionReadiness:
        """Project readiness without confusing durable intake with delivery."""

        self._assert_open()
        local = self._delivery.worker_readiness()
        try:
            website = _AUTH._HTTP._readiness_payload(local)
            if website != local:
                raise ValueError
        except Exception:
            raise _blocked("readiness_invalid") from None

        if website["status"] != "ready":
            return HMACCMSSourceDeliverySubmissionReadiness(
                schema="blun.cms-source-delivery-submission-readiness.v1",
                status="not_ready",
                website_status=website["status"],
                website_worker_state=website["worker_state"],
                website_outbox_status=website["outbox_status"],
                website_error_code=website["error_code"],
                sidecar_status=None,
                sidecar_worker_state=None,
                sidecar_outbox_status=None,
                sidecar_error_code=None,
                sidecar_capabilities_sha256=(
                    self._client.expected_capabilities_sha256
                ),
                source_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )

        response = self._client.readiness()
        try:
            if (
                not isinstance(response, Mapping)
                or set(response)
                != {"schema", "readiness", "capabilities_sha256"}
                or response.get("schema")
                != _AUTH._HTTP.READINESS_RESPONSE_SCHEMA
                or response.get("capabilities_sha256")
                != self._client.expected_capabilities_sha256
            ):
                raise ValueError
            sidecar = _AUTH._HTTP._readiness_payload(response["readiness"])
            if sidecar != response["readiness"]:
                raise ValueError
        except Exception:
            raise _blocked("readiness_invalid") from None

        ready = sidecar["status"] == "ready"
        return HMACCMSSourceDeliverySubmissionReadiness(
            schema="blun.cms-source-delivery-submission-readiness.v1",
            status="ready" if ready else "not_ready",
            website_status=website["status"],
            website_worker_state=website["worker_state"],
            website_outbox_status=website["outbox_status"],
            website_error_code=website["error_code"],
            sidecar_status=sidecar["status"],
            sidecar_worker_state=sidecar["worker_state"],
            sidecar_outbox_status=sidecar["outbox_status"],
            sidecar_error_code=sidecar["error_code"],
            sidecar_capabilities_sha256=(
                self._client.expected_capabilities_sha256
            ),
            source_capabilities_sha256=(
                self._client.expected_remote_capabilities_sha256
            ),
        )

    def submission_pipeline_readiness(
        self,
    ) -> HMACCMSSourceDeliverySubmissionPipelineReadiness:
        """Require website, sidecar, and source processing to be ready."""

        self._assert_open()
        intake = self.submission_readiness()
        intake_payload = intake.as_payload()
        if intake.status != "ready":
            return HMACCMSSourceDeliverySubmissionPipelineReadiness(
                schema=(
                    "blun.cms-source-delivery-submission-pipeline-readiness.v2"
                ),
                status="not_ready",
                intake_readiness=intake_payload,
                source_readiness=None,
                source_capability_binding=None,
                sidecar_capabilities_sha256=(
                    self._client.expected_capabilities_sha256
                ),
                source_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )

        try:
            response = self._client.source_readiness()
        except Exception:
            raise _blocked("pipeline_readiness_unavailable") from None
        try:
            if (
                not isinstance(response, Mapping)
                or set(response) != {
                    "schema", "source_readiness",
                    "source_capabilities_sha256", "capabilities_sha256",
                    "source_capability_binding",
                }
                or response.get("schema")
                != _AUTH._HTTP.SOURCE_READINESS_RESPONSE_SCHEMA
                or response.get("capabilities_sha256")
                != self._client.expected_capabilities_sha256
                or response.get("source_capabilities_sha256")
                != self._client.expected_remote_capabilities_sha256
            ):
                raise ValueError
            source_readiness = _AUTH._HTTP._source_readiness_response(
                {
                    "schema": (
                        _AUTH._HTTP._SOURCE_HTTP.READINESS_RESPONSE_SCHEMA
                    ),
                    "readiness": response["source_readiness"],
                    "capability_binding": response[
                        "source_capability_binding"
                    ],
                    "capabilities_sha256": (
                        response["source_capabilities_sha256"]
                    ),
                },
                expected_capabilities_sha256=(
                    self._client.expected_remote_capabilities_sha256
                ),
            )
            if (
                source_readiness["readiness"]
                != response["source_readiness"]
                or source_readiness["capability_binding"]
                != response["source_capability_binding"]
            ):
                raise ValueError
        except Exception:
            raise _blocked("pipeline_readiness_invalid") from None

        return HMACCMSSourceDeliverySubmissionPipelineReadiness(
            schema="blun.cms-source-delivery-submission-pipeline-readiness.v2",
            status=(
                "ready"
                if source_readiness["readiness"]["status"] == "ready"
                else "not_ready"
            ),
            intake_readiness=intake_payload,
            source_readiness=source_readiness["readiness"],
            source_capability_binding=(
                source_readiness["capability_binding"]
            ),
            sidecar_capabilities_sha256=(
                self._client.expected_capabilities_sha256
            ),
            source_capabilities_sha256=(
                self._client.expected_remote_capabilities_sha256
            ),
        )

    def run_once(self) -> Any:
        self._assert_open()
        return self._delivery.run_once()

    def start_worker(self, **kwargs: Any) -> None:
        self._assert_open()
        self._delivery.start_worker(**kwargs)

    def stop_worker(self, **kwargs: Any) -> None:
        self._assert_open()
        self._delivery.stop_worker(**kwargs)

    def require_worker_ready(self) -> None:
        self._assert_open()
        self._delivery.require_worker_ready()

    def worker_readiness(self) -> Mapping[str, Any]:
        self._assert_open()
        return self._delivery.worker_readiness()

    def run_forever(self, stop: Callable[[], bool], **kwargs: Any) -> None:
        self._assert_open()
        self._delivery.run_forever(stop, **kwargs)

    def sidecar_capabilities(self) -> Mapping[str, Any]:
        self._assert_open()
        return self._client.capabilities()

    def sidecar_status(
        self,
        operation: str,
        request_id: str,
        event_id: str,
        site_id: str,
        payload_sha256: str,
    ) -> Mapping[str, Any]:
        """Read the separately durable sidecar-to-source processing state."""

        self._assert_open()
        return self._client.status(
            operation, request_id, event_id, site_id, payload_sha256,
        )

    def sidecar_source_status(
        self, event_id: str, site_id: str, payload_sha256: str,
    ) -> Mapping[str, Any]:
        """Read source lifecycle through the owned authenticated sidecar."""

        self._assert_open()
        return self._client.source_status(
            event_id, site_id, payload_sha256,
        )

    def sidecar_source_readiness(self) -> Mapping[str, Any]:
        """Read source processing readiness through the owned sidecar."""

        self._assert_open()
        return self._client.source_readiness()

    def sidecar_source_health(self) -> Mapping[str, Any]:
        """Read source queue health through the owned sidecar."""

        self._assert_open()
        return self._client.source_health()

    def sidecar_health(self) -> Mapping[str, Any]:
        self._assert_open()
        return self._client.health()

    def sidecar_readiness(self) -> Mapping[str, Any]:
        self._assert_open()
        return self._client.readiness()

    def close(self, *, worker_timeout_seconds: float | int = 30) -> None:
        self._assert_owner()
        self._delivery.close(worker_timeout_seconds=worker_timeout_seconds)

    @property
    def state(self) -> str:
        if os.getpid() != self._owner_pid:
            return "foreign-process"
        return self._delivery.state

    @property
    def worker_state(self) -> str:
        if os.getpid() != self._owner_pid:
            return "foreign-process"
        return self._delivery.worker_state

    @property
    def expected_capabilities_sha256(self) -> str:
        """Return the synthetic contract pinned into every local outbox row."""

        return self._adapter.expected_capabilities_sha256


def _client_and_adapter(
    credential: HMACCredential,
    *,
    origin: str,
    sidecar_capabilities_sha256: str,
    remote_capabilities_sha256: str,
    runtime_capabilities_sha256: str,
    commercial_rendering_registry_sha256: str,
    sidecar_delivery_max_attempts: int,
    clock: Callable[[], float | int],
    nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
    transport: Any,
    timeout: float | int,
    allow_loopback_http: bool,
) -> tuple[Any, Any]:
    client = _AUTH.RotatingHMACCMSSourceDeliveryClient(
        credential,
        origin=origin,
        sidecar_capabilities_sha256=sidecar_capabilities_sha256,
        remote_capabilities_sha256=remote_capabilities_sha256,
        clock=clock,
        nonce_factory=nonce_factory,
        transport=transport,
        timeout=timeout,
        allow_loopback_http=allow_loopback_http,
    )
    adapter = _ADAPTER.CMSSourceDeliverySidecarOutboxAdapter(
        client,
        sidecar_delivery_max_attempts=sidecar_delivery_max_attempts,
        runtime_capabilities_sha256=runtime_capabilities_sha256,
        commercial_rendering_registry_sha256=(
            commercial_rendering_registry_sha256
        ),
    )
    try:
        response = client.source_readiness()
    except Exception:
        raise _blocked("capability_preflight_unavailable") from None
    binding = (
        response.get("source_capability_binding")
        if isinstance(response, Mapping)
        else None
    )
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {
            "schema", "status", "capabilities_sha256",
            "commercial_rendering_registry_sha256", "database_roles",
        }
        or binding.get("schema") != "blun.cms-source-capability-binding.v2"
        or binding.get("status") != "verified"
        or binding.get("capabilities_sha256")
        != runtime_capabilities_sha256
        or binding.get("commercial_rendering_registry_sha256")
        != commercial_rendering_registry_sha256
        or binding.get("database_roles")
        != ["changes", "removals", "lifecycle"]
    ):
        raise _blocked("capability_preflight_mismatch")
    return client, adapter


def open_durable_hmac_cms_source_delivery_submission(
    database: str | os.PathLike[str],
    credential: HMACCredential,
    *,
    worker_id: str,
    origin: str,
    sidecar_capabilities_sha256: str,
    remote_capabilities_sha256: str,
    runtime_capabilities_sha256: str,
    commercial_rendering_registry_sha256: str,
    sidecar_delivery_max_attempts: int = 5,
    clock: Callable[[], float | int] = time.time,
    nonce_factory: Callable[[], str],
    transport: Any = None,
    timeout: float | int = 30,
    allow_loopback_http: bool = False,
    sqlite_timeout_seconds: float | int = 5,
    lease_seconds: float | int = 600,
    base_delay_seconds: float | int = 5,
    max_delay_seconds: float | int = 300,
) -> HMACCMSSourceDeliverySubmissionRuntime:
    """Preflight and open one durable website-to-sidecar submission runtime."""

    client, adapter = _client_and_adapter(
        credential,
        origin=origin,
        sidecar_capabilities_sha256=sidecar_capabilities_sha256,
        remote_capabilities_sha256=remote_capabilities_sha256,
        runtime_capabilities_sha256=runtime_capabilities_sha256,
        commercial_rendering_registry_sha256=(
            commercial_rendering_registry_sha256
        ),
        sidecar_delivery_max_attempts=sidecar_delivery_max_attempts,
        clock=clock,
        nonce_factory=nonce_factory,
        transport=transport,
        timeout=timeout,
        allow_loopback_http=allow_loopback_http,
    )
    delivery = _RUNTIME.open_durable_cms_source_delivery(
        database,
        adapter,
        worker_id=worker_id,
        clock=clock,
        sqlite_timeout_seconds=sqlite_timeout_seconds,
        lease_seconds=lease_seconds,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    return HMACCMSSourceDeliverySubmissionRuntime(client, adapter, delivery)


def open_hosted_hmac_cms_source_delivery_submission(
    *args: Any,
    active_delay_seconds: float | int = 0.05,
    idle_delay_seconds: float | int = 1,
    blocked_delay_seconds: float | int = 5,
    **kwargs: Any,
) -> HMACCMSSourceDeliverySubmissionRuntime:
    """Open the complete composition and start its supervised worker."""

    # Validate worker delays before the durable factory can create its file.
    delays = _RUNTIME.DurableCMSSourceDeliveryRuntime._loop_delays(
        active_delay_seconds,
        idle_delay_seconds,
        blocked_delay_seconds,
    )
    runtime = open_durable_hmac_cms_source_delivery_submission(*args, **kwargs)
    try:
        runtime.start_worker(
            active_delay_seconds=delays[0],
            idle_delay_seconds=delays[1],
            blocked_delay_seconds=delays[2],
        )
    except Exception:
        runtime.close()
        raise
    return runtime
