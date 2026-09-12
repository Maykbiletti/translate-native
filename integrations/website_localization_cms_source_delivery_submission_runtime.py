#!/usr/bin/env python3
"""Owned website runtime for durable HMAC-authenticated sidecar submission."""

from __future__ import annotations

import importlib.util
import os
import secrets
import sys
import time
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

    def health(self) -> Any:
        self._assert_open()
        return self._delivery.health()

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
    )
    return client, adapter


def open_durable_hmac_cms_source_delivery_submission(
    database: str | os.PathLike[str],
    credential: HMACCredential,
    *,
    worker_id: str,
    origin: str,
    sidecar_capabilities_sha256: str,
    remote_capabilities_sha256: str,
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
