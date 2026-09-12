#!/usr/bin/env python3
"""Bind the source-delivery sidecar client to the durable website outbox.

The website outbox and the sidecar intentionally own different retry loops.
This adapter keeps their budgets separate: the outbox retries durable sidecar
acceptance, while ``sidecar_delivery_max_attempts`` controls the sidecar's
delivery to the source service and the per-call ``max_attempts`` controls the
source service itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping


ADAPTER_SCHEMA = "blun.cms-source-delivery-sidecar-outbox-adapter.v1"
SHA256 = re.compile(r"^[a-f0-9]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
CHANGE_SCHEMA = "blun.cms-content-change.v2"
CANCELLATION_SCHEMA = "blun.cms-content-cancellation.v1"
TOMBSTONE_SCHEMA = "blun.cms-content-tombstone.v1"
CHANGE_RESPONSE_SCHEMA = "blun.cms-source-change-enqueue-response.v2"
REMOVAL_RESPONSE_SCHEMA = "blun.cms-source-removal-enqueue-response.v2"
SIDECAR_QUEUE_RESPONSE_SCHEMA = "blun.cms-source-delivery-queue-response.v1"
QUEUE_FIELDS = {
    "operation", "request_id", "event_id", "site_id", "payload_sha256",
    "remote_capabilities_sha256", "status", "attempts",
    "delivery_max_attempts", "source_max_attempts", "next_attempt_at",
    "lease_expires_at", "lease_expired", "last_error_code",
    "response_sha256",
}


class CMSSourceDeliverySidecarAdapterBlocked(RuntimeError):
    """Content-free failure compatible with the website delivery outbox."""

    cms_source_client_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            code = "source_delivery_sidecar_adapter.blocked"
            retryable = False
        if not isinstance(retryable, bool):
            retryable = False
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _attempts(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise CMSSourceDeliverySidecarAdapterBlocked(
            "source_delivery_sidecar_adapter.attempts_invalid", retryable=False,
        )
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CMSSourceDeliverySidecarAdapterBlocked(
            "source_delivery_sidecar_adapter.request_invalid", retryable=False,
        ) from None


def _failure(error: Exception) -> CMSSourceDeliverySidecarAdapterBlocked:
    code = getattr(error, "code", None)
    retryable = getattr(error, "retryable", None)
    if (
        getattr(error, "cms_source_delivery_client_failure", False) is True
        and isinstance(code, str)
        and ERROR_CODE.fullmatch(code) is not None
        and isinstance(retryable, bool)
    ):
        return CMSSourceDeliverySidecarAdapterBlocked(code, retryable=retryable)
    return CMSSourceDeliverySidecarAdapterBlocked(
        "source_delivery_sidecar_adapter.client_failure", retryable=False,
    )


class CMSSourceDeliverySidecarOutboxAdapter:
    """Project validated sidecar acceptance into the durable outbox contract."""

    def __init__(self, client: Any, *, sidecar_delivery_max_attempts: int = 5):
        sidecar_digest = getattr(client, "expected_capabilities_sha256", None)
        remote_digest = getattr(client, "expected_remote_capabilities_sha256", None)
        timeout = getattr(client, "timeout", None)
        if (
            not callable(getattr(client, "submit_change", None))
            or not callable(getattr(client, "submit_removal", None))
            or not isinstance(sidecar_digest, str)
            or SHA256.fullmatch(sidecar_digest) is None
            or not isinstance(remote_digest, str)
            or SHA256.fullmatch(remote_digest) is None
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 300
        ):
            raise CMSSourceDeliverySidecarAdapterBlocked(
                "source_delivery_sidecar_adapter.client_invalid", retryable=False,
            )
        delivery_attempts = _attempts(sidecar_delivery_max_attempts)
        binding = {
            "schema": ADAPTER_SCHEMA,
            "sidecar_capabilities_sha256": sidecar_digest,
            "remote_capabilities_sha256": remote_digest,
            "sidecar_delivery_max_attempts": delivery_attempts,
        }
        self.client = client
        self.sidecar_capabilities_sha256 = sidecar_digest
        self.remote_capabilities_sha256 = remote_digest
        self.sidecar_delivery_max_attempts = delivery_attempts
        self.expected_capabilities_sha256 = hashlib.sha256(
            _canonical(binding)
        ).hexdigest()
        self.timeout = float(timeout)

    def __repr__(self) -> str:
        return "CMSSourceDeliverySidecarOutboxAdapter(configured=True)"

    @staticmethod
    def _identity(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
        if not isinstance(value, Mapping):
            raise CMSSourceDeliverySidecarAdapterBlocked(
                "source_delivery_sidecar_adapter.request_invalid", retryable=False,
            )
        schema = value.get("schema")
        site_id = value.get("site_id")
        event_id = value.get("event_id")
        if schema == CHANGE_SCHEMA:
            operation, request_id = "change", event_id
        elif schema == CANCELLATION_SCHEMA:
            operation, request_id = "cancellation", value.get("cancellation_id")
        elif schema == TOMBSTONE_SCHEMA:
            operation, request_id = "tombstone", value.get("tombstone_id")
        else:
            operation, request_id = "", None
        if (
            operation == ""
            or not all(
                isinstance(item, str) and TOKEN.fullmatch(item) is not None
                for item in (site_id, event_id, request_id)
            )
        ):
            raise CMSSourceDeliverySidecarAdapterBlocked(
                "source_delivery_sidecar_adapter.request_invalid", retryable=False,
            )
        return operation, request_id, event_id, site_id

    def _submit(
        self, value: Mapping[str, Any], *, max_attempts: int, change: bool,
    ) -> Mapping[str, Any]:
        source_attempts = _attempts(max_attempts)
        operation, request_id, event_id, site_id = self._identity(value)
        if change != (operation == "change"):
            raise CMSSourceDeliverySidecarAdapterBlocked(
                "source_delivery_sidecar_adapter.request_invalid", retryable=False,
            )
        payload_hash = hashlib.sha256(_canonical(dict(value))).hexdigest()
        try:
            if change:
                response = self.client.submit_change(
                    value, source_max_attempts=source_attempts,
                    delivery_max_attempts=self.sidecar_delivery_max_attempts,
                )
            else:
                response = self.client.submit_removal(
                    value, source_max_attempts=source_attempts,
                    delivery_max_attempts=self.sidecar_delivery_max_attempts,
                )
        except Exception as error:
            raise _failure(error) from error

        queue = response.get("queue") if isinstance(response, Mapping) else None
        status = queue.get("status") if isinstance(queue, Mapping) else None
        attempts = queue.get("attempts") if isinstance(queue, Mapping) else None
        next_attempt = (
            queue.get("next_attempt_at") if isinstance(queue, Mapping) else None
        )
        lease_expires = (
            queue.get("lease_expires_at") if isinstance(queue, Mapping) else None
        )
        last_error = (
            queue.get("last_error_code") if isinstance(queue, Mapping) else None
        )
        response_hash = (
            queue.get("response_sha256") if isinstance(queue, Mapping) else None
        )
        if (
            not isinstance(response, Mapping)
            or set(response) != {"schema", "queue", "capabilities_sha256"}
            or response.get("schema") != SIDECAR_QUEUE_RESPONSE_SCHEMA
            or response.get("capabilities_sha256") != self.sidecar_capabilities_sha256
            or not isinstance(queue, Mapping)
            or set(queue) != QUEUE_FIELDS
            or queue.get("operation") != operation
            or queue.get("request_id") != request_id
            or queue.get("event_id") != event_id
            or queue.get("site_id") != site_id
            or queue.get("payload_sha256") != payload_hash
            or queue.get("remote_capabilities_sha256") != self.remote_capabilities_sha256
            or queue.get("source_max_attempts") != source_attempts
            or queue.get("delivery_max_attempts") != self.sidecar_delivery_max_attempts
            or status not in {"pending", "leased", "retry_wait", "succeeded", "failed"}
            or isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 0 <= attempts <= self.sidecar_delivery_max_attempts
            or isinstance(next_attempt, bool)
            or not isinstance(next_attempt, (int, float))
            or not math.isfinite(float(next_attempt))
            or float(next_attempt) < 0
            or not isinstance(queue.get("lease_expired"), bool)
            or (status == "leased") != (lease_expires is not None)
            or lease_expires is not None and (
                isinstance(lease_expires, bool)
                or not isinstance(lease_expires, (int, float))
                or not math.isfinite(float(lease_expires))
                or float(lease_expires) < 0
            )
            or last_error is not None and (
                not isinstance(last_error, str)
                or ERROR_CODE.fullmatch(last_error) is None
            )
            or response_hash is not None and (
                not isinstance(response_hash, str)
                or SHA256.fullmatch(response_hash) is None
            )
            or (status == "succeeded") != (response_hash is not None)
            or status in {"retry_wait", "failed"} and last_error is None
        ):
            raise CMSSourceDeliverySidecarAdapterBlocked(
                "source_delivery_sidecar_adapter.response_invalid", retryable=False,
            )

        # This private projection records sidecar acceptance only. It is never
        # exposed as source-service status; downstream state remains separate.
        return {
            "schema": CHANGE_RESPONSE_SCHEMA if change else REMOVAL_RESPONSE_SCHEMA,
            "operation": operation,
            "request_id": request_id,
            "event_id": event_id,
            "payload_sha256": payload_hash,
            "status": "pending",
            "attempts": 0,
            "max_attempts": source_attempts,
            "capabilities_sha256": self.expected_capabilities_sha256,
        }

    def submit_change(
        self, value: Mapping[str, Any], *, max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        return self._submit(value, max_attempts=max_attempts, change=True)

    def submit_removal(
        self, value: Mapping[str, Any], *, max_attempts: int = 5,
    ) -> Mapping[str, Any]:
        return self._submit(value, max_attempts=max_attempts, change=False)
