#!/usr/bin/env python3
"""Process-owned runtime for the durable public submission dispatcher.

The composition root validates the client, worker policy, and existing SQLite
generation before it creates or mutates durable state. One runtime owns one
connection, guards its file identity around every operation, serializes access,
and can supervise one non-daemon delivery worker.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load submission runtime dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_DISPATCH = _load_module(
    "blun_website_localization_submission_dispatch_runtime_store",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_dispatch.py",
)
_FILE_RUNTIME = _load_module(
    "blun_website_localization_submission_dispatch_runtime_file",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_runtime.py",
)
_HTTP = _load_module(
    "blun_website_localization_submission_dispatch_runtime_http",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_dispatch_http.py",
)


class CMSSourceDeliverySubmissionDispatchRuntimeBlocked(RuntimeError):
    """Stable runtime failure without paths, content, or private exceptions."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _blocked(code: str) -> CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
    return CMSSourceDeliverySubmissionDispatchRuntimeBlocked(
        "source_delivery_submission_dispatch_runtime." + code
    )


def _store_failure(error: Exception) -> CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
    code = getattr(error, "code", "")
    if code.endswith("idempotency_collision"):
        return _blocked("idempotency_collision")
    if code.endswith("submission_missing"):
        return _blocked("submission_missing")
    if code.endswith("lifecycle_not_accepted"):
        return _blocked("lifecycle_not_accepted")
    if code.endswith("commercial_profile_invalid"):
        return _blocked("commercial_profile_invalid")
    if code.endswith(("request_invalid", "attempts_invalid", "identity_invalid")):
        return _blocked("request_invalid")
    return _blocked("outbox_blocked")


def _file_failure(error: Exception) -> CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
    code = getattr(error, "code", "")
    suffix = code.rsplit(".", 1)[-1]
    allowed = {
        "database_path_invalid",
        "database_parent_unavailable",
        "database_parent_unsafe",
        "database_unavailable",
        "database_unsafe",
        "database_create_failed",
        "sqlite_timeout_invalid",
    }
    return _blocked(suffix if suffix in allowed else "database_blocked")


def _duration(value: Any, code: str, *, maximum: float = 86_400) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= maximum
    ):
        raise _blocked(code)
    return float(value)


def _now(clock: Callable[[], float | int]) -> float:
    try:
        value = clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        result = float(value)
        if result < 0 or not math.isfinite(result):
            raise ValueError
        return result
    except Exception:
        raise _blocked("clock_invalid") from None


def _client_digest(client: Any) -> str:
    value = getattr(client, "expected_capabilities_sha256", None)
    if (
        not callable(getattr(client, "submit_change", None))
        or not callable(getattr(client, "submit_removal", None))
        or not isinstance(value, str)
        or _DISPATCH.SHA256.fullmatch(value) is None
    ):
        raise _blocked("configuration_invalid")
    return value


def _preflight(
    client: Any,
    worker_id: Any,
    lease_seconds: Any,
    clock: Callable[[], float | int],
    options: Mapping[str, Any],
) -> tuple[str, float, tuple[tuple[Any, ...], ...]]:
    if not callable(clock):
        raise _blocked("configuration_invalid")
    digest = _client_digest(client)
    lease = _duration(lease_seconds, "lease_invalid")
    now = _now(clock)
    connection = sqlite3.connect(":memory:")
    try:
        dispatcher = _DISPATCH.DurableCMSSourceDeliverySubmissionDispatcher(
            connection, digest, **dict(options)
        )
        dispatcher.run_once(client, worker_id, now=now, lease_seconds=lease)
        dispatcher.health(now=now)
        schema = tuple(tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall())
    except CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
        raise
    except Exception as error:
        raise _blocked("configuration_invalid") from error
    finally:
        connection.close()
    return digest, lease, schema


def _preflight_existing(
    path: str,
    digest: str,
    options: Mapping[str, Any],
    expected_schema: tuple[tuple[Any, ...], ...],
    now: float,
) -> None:
    if path == ":memory:" or not os.path.exists(path):
        return
    connection = None
    try:
        connection = sqlite3.connect(
            Path(path).as_uri() + "?mode=ro",
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        actual = tuple(tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall())
        if not actual:
            return
        if actual != expected_schema:
            if _DISPATCH._is_empty_legacy_v1(connection, digest):
                return
            raise _blocked("database_schema_altered")
        dispatcher = object.__new__(
            _DISPATCH.DurableCMSSourceDeliverySubmissionDispatcher
        )
        dispatcher.connection = connection
        dispatcher.expected_capabilities_sha256 = digest
        dispatcher.base_delay_seconds = float(options["base_delay_seconds"])
        dispatcher.max_delay_seconds = float(options["max_delay_seconds"])
        dispatcher.health(now=now)
    except CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
        raise
    except Exception as error:
        if isinstance(error, _DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked):
            raise _blocked("database_generation_invalid") from error
        raise _blocked("database_preflight_failed") from error
    finally:
        if connection is not None:
            connection.close()


class DurableCMSSourceDeliverySubmissionDispatchRuntime:
    """One process-owned, guarded, thread-safe caller submission runtime."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        guard: Callable[[], None],
        dispatcher: Any,
        client: Any,
        *,
        worker_id: str,
        lease_seconds: float,
        clock: Callable[[], float | int],
        expected_capabilities_sha256: str,
        http_authenticator: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self._connection = connection
        self._guard = guard
        self._dispatcher = dispatcher
        self._client = client
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._expected_capabilities_sha256 = expected_capabilities_sha256
        self._lock = threading.RLock()
        self._closed = False
        self._owner_pid = os.getpid()
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._worker_state = "unmanaged"
        self._worker_error_code: str | None = None
        self.http = (
            None
            if http_authenticator is None
            else _HTTP.build_submission_dispatch_http(self, http_authenticator)
        )

    def __repr__(self) -> str:
        return f"DurableCMSSourceDeliverySubmissionDispatchRuntime(state={self.state!r})"

    def __enter__(self) -> "DurableCMSSourceDeliverySubmissionDispatchRuntime":
        self._assert_owner()
        if self._closed:
            raise _blocked("closed")
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _assert_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise _blocked("foreign_process")

    def _guard_client(self) -> None:
        if _client_digest(self._client) != self._expected_capabilities_sha256:
            raise _blocked("capability_drift")

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("closed")
            try:
                self._guard()
                self._guard_client()
                result = getattr(self._dispatcher, name)(*args, **kwargs)
                self._guard()
                return result
            except CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
                raise
            except _FILE_RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked as error:
                raise _file_failure(error) from error
            except _DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked as error:
                raise _store_failure(error) from error
            except Exception as error:
                raise _blocked("outbox_blocked") from error

    def _require_worker_locked(self) -> None:
        if self._worker_state == "unmanaged":
            return
        if (
            self._worker_state != "running"
            or self._worker_error_code is not None
            or self._worker_thread is None
            or not self._worker_thread.is_alive()
        ):
            raise _blocked("worker_not_ready")

    def enqueue(
        self,
        payload: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
        client_max_attempts: int = 5,
    ) -> Any:
        self._assert_owner()
        with self._lock:
            self._require_worker_locked()
            return self._call(
                "enqueue",
                payload,
                source_max_attempts=source_max_attempts,
                delivery_max_attempts=delivery_max_attempts,
                client_max_attempts=client_max_attempts,
                now=_now(self._clock),
            )

    def status(self, operation: str, request_id: str) -> Any:
        return self._call("status", operation, request_id, now=_now(self._clock))

    def lifecycle(self, operation: str, request_id: str) -> Any:
        return self._call(
            "lifecycle", self._client, operation, request_id,
            now=_now(self._clock),
        )

    def commercial_profile(self) -> Any:
        return self._call("commercial_profile", self._client)

    def health(self) -> Any:
        return self._call("health", now=_now(self._clock))

    def run_once(self) -> Any:
        return self._call(
            "run_once",
            self._client,
            self._worker_id,
            now=_now(self._clock),
            lease_seconds=self._lease_seconds,
        )

    @property
    def expected_capabilities_sha256(self) -> str:
        """Return the locally verified public website contract pin."""

        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("closed")
            try:
                self._guard()
                self._guard_client()
            except _FILE_RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked as error:
                raise _file_failure(error) from error
            return self._expected_capabilities_sha256

    @staticmethod
    def _loop_delays(
        active_delay_seconds: Any,
        idle_delay_seconds: Any,
        blocked_delay_seconds: Any,
    ) -> tuple[float, float, float]:
        return tuple(
            _duration(value, "loop_invalid", maximum=300)
            for value in (
                active_delay_seconds,
                idle_delay_seconds,
                blocked_delay_seconds,
            )
        )

    def _managed_worker(
        self,
        stop: threading.Event,
        delays: tuple[float, float, float],
    ) -> None:
        try:
            active, idle, blocked = delays
            while not stop.is_set():
                outcome = self.run_once()
                if outcome is None:
                    delay = idle
                elif outcome.status in {"retry_wait", "failed"}:
                    delay = blocked
                else:
                    delay = active
                stop.wait(delay)
        except Exception:
            with self._lock:
                self._worker_error_code = (
                    "source_delivery_submission_dispatch_runtime.worker_blocked"
                )
                self._worker_state = "failed"
            return
        with self._lock:
            if self._worker_state in {"running", "stopping"}:
                self._worker_state = "stopped"

    def start_worker(
        self,
        *,
        active_delay_seconds: float | int = 0.05,
        idle_delay_seconds: float | int = 1,
        blocked_delay_seconds: float | int = 5,
    ) -> None:
        delays = self._loop_delays(
            active_delay_seconds, idle_delay_seconds, blocked_delay_seconds
        )
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("closed")
            if self._worker_state == "running":
                return
            if self._worker_state in {"stopping", "failed"}:
                raise _blocked("worker_blocked")
            self._guard()
            stop = threading.Event()
            thread = threading.Thread(
                target=self._managed_worker,
                args=(stop, delays),
                name="cms-public-submission-worker",
                daemon=False,
            )
            self._worker_stop = stop
            self._worker_thread = thread
            self._worker_error_code = None
            self._worker_state = "running"
            try:
                thread.start()
            except Exception as error:
                self._worker_thread = None
                self._worker_state = "failed"
                self._worker_error_code = (
                    "source_delivery_submission_dispatch_runtime.worker_blocked"
                )
                raise _blocked("worker_blocked") from error

    def stop_worker(self, *, timeout_seconds: float | int = 30) -> None:
        timeout = _duration(timeout_seconds, "worker_timeout_invalid", maximum=300)
        self._assert_owner()
        thread = self._worker_thread
        state = self._worker_state
        if thread is None or state in {"unmanaged", "stopped"}:
            return
        if thread is threading.current_thread():
            raise _blocked("worker_stop_invalid")
        if state != "failed":
            self._worker_state = "stopping"
        self._worker_stop.set()
        thread.join(timeout)
        if thread.is_alive():
            raise _blocked("worker_stop_timeout")
        with self._lock:
            if self._worker_state == "stopping":
                self._worker_state = "stopped"

    def worker_readiness(self) -> dict[str, Any]:
        self._assert_owner()
        with self._lock:
            state = "closed" if self._closed else self._worker_state
            thread = self._worker_thread
            alive = thread is not None and thread.is_alive()
            error_code = self._worker_error_code
        if state != "running" or not alive:
            return {
                "schema": "blun.cms-public-submission-worker-readiness.v1",
                "status": "not_ready",
                "worker_state": state,
                "outbox_status": None,
                "error_code": error_code or (
                    "source_delivery_submission_dispatch_runtime.worker_not_ready"
                ),
                "capabilities_sha256": self._expected_capabilities_sha256,
            }
        try:
            health = self.health()
        except CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
            return {
                "schema": "blun.cms-public-submission-worker-readiness.v1",
                "status": "not_ready",
                "worker_state": "running",
                "outbox_status": "blocked",
                "error_code": (
                    "source_delivery_submission_dispatch_runtime.outbox_blocked"
                ),
                "capabilities_sha256": self._expected_capabilities_sha256,
            }
        ready = health.status == "ok"
        return {
            "schema": "blun.cms-public-submission-worker-readiness.v1",
            "status": "ready" if ready else "not_ready",
            "worker_state": "running",
            "outbox_status": health.status,
            "error_code": None if ready else (
                "source_delivery_submission_dispatch_runtime.outbox_blocked"
            ),
            "capabilities_sha256": self._expected_capabilities_sha256,
        }

    def close(self, *, worker_timeout_seconds: float | int = 30) -> None:
        self._assert_owner()
        self.stop_worker(timeout_seconds=worker_timeout_seconds)
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.close()
            except sqlite3.Error as error:
                raise _blocked("close_failed") from error
            self._closed = True

    @property
    def state(self) -> str:
        if os.getpid() != self._owner_pid:
            return "foreign-process"
        with self._lock:
            return "closed" if self._closed else "open"

    @property
    def worker_state(self) -> str:
        if os.getpid() != self._owner_pid:
            return "foreign-process"
        with self._lock:
            return "closed" if self._closed else self._worker_state


def open_durable_cms_source_delivery_submission_dispatch(
    database: str | os.PathLike[str],
    client: Any,
    *,
    worker_id: str,
    clock: Callable[[], float | int] = time.time,
    sqlite_timeout_seconds: float | int = 5,
    lease_seconds: float | int = 600,
    base_delay_seconds: float | int = 5,
    max_delay_seconds: float | int = 300,
    http_authenticator: Callable[[dict[str, Any]], Any] | None = None,
) -> DurableCMSSourceDeliverySubmissionDispatchRuntime:
    """Validate everything, then open one guarded caller-owned outbox."""

    if http_authenticator is not None and not callable(http_authenticator):
        raise _blocked("configuration_invalid")
    options = {
        "base_delay_seconds": base_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
    }
    digest, lease, schema = _preflight(
        client, worker_id, lease_seconds, clock, options
    )
    try:
        path = _FILE_RUNTIME._database_path(database)
        timeout = _FILE_RUNTIME._sqlite_timeout(sqlite_timeout_seconds)
        guard = _FILE_RUNTIME._prepare_database_file(path)
        _preflight_existing(path, digest, options, schema, _now(clock))
        guard()
    except CMSSourceDeliverySubmissionDispatchRuntimeBlocked:
        raise
    except _FILE_RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked as error:
        raise _file_failure(error) from error
    connection = None
    try:
        connection = sqlite3.connect(
            path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        guard()
        dispatcher = _DISPATCH.DurableCMSSourceDeliverySubmissionDispatcher(
            connection, digest, **options
        )
        guard()
    except Exception as error:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if isinstance(error, _FILE_RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked):
            raise _file_failure(error) from error
        if isinstance(error, _DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked):
            raise _store_failure(error) from error
        raise _blocked("initialization_failed") from error
    return DurableCMSSourceDeliverySubmissionDispatchRuntime(
        connection,
        guard,
        dispatcher,
        client,
        worker_id=worker_id,
        lease_seconds=lease,
        clock=clock,
        expected_capabilities_sha256=digest,
        http_authenticator=http_authenticator,
    )


def open_hosted_cms_source_delivery_submission_dispatch(
    *args: Any,
    active_delay_seconds: float | int = 0.05,
    idle_delay_seconds: float | int = 1,
    blocked_delay_seconds: float | int = 5,
    **kwargs: Any,
) -> DurableCMSSourceDeliverySubmissionDispatchRuntime:
    """Open the guarded caller outbox and start its supervised worker."""

    delays = DurableCMSSourceDeliverySubmissionDispatchRuntime._loop_delays(
        active_delay_seconds, idle_delay_seconds, blocked_delay_seconds
    )
    runtime = open_durable_cms_source_delivery_submission_dispatch(*args, **kwargs)
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
