#!/usr/bin/env python3
"""Protected production runtime for durable website-source delivery.

The composition root validates the complete worker in memory before creating
one private SQLite database. A runtime belongs to one process, serializes all
connection access, guards the database identity around every operation, and
can own one supervised non-daemon delivery worker.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"cannot load source delivery runtime dependency: {path.name}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_DELIVERY = _load_module(
    "blun_website_localization_composed_cms_source_delivery",
    _ROOT / "integrations" / "website_localization_cms_source_delivery.py",
)
_HTTP = _load_module(
    "blun_website_localization_composed_cms_source_delivery_http",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_http.py",
)


class DurableCMSSourceDeliveryRuntimeBlocked(RuntimeError):
    """Stable runtime failure without paths, content, or private exceptions."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _blocked(code: str) -> DurableCMSSourceDeliveryRuntimeBlocked:
    return DurableCMSSourceDeliveryRuntimeBlocked(code)


def _delivery_failure(error: Exception) -> DurableCMSSourceDeliveryRuntimeBlocked:
    code = getattr(error, "code", None)
    if code in {
        "source_delivery.request_invalid",
        "source_delivery.source_attempts_invalid",
        "source_delivery.attempts_invalid",
        "source_delivery.status_invalid",
        "source_delivery.worker_invalid",
        "source_delivery.lease_invalid",
        "source_delivery.lease_too_short",
    }:
        return _blocked("source_delivery_runtime.request_invalid")
    if code == "source_delivery.idempotency_collision":
        return _blocked("source_delivery_runtime.idempotency_collision")
    if code == "source_delivery.status_not_found":
        return _blocked("source_delivery_runtime.status_not_found")
    if code == "source_delivery.source_status_unavailable":
        return _blocked("source_delivery_runtime.source_status_unavailable")
    return _blocked("source_delivery_runtime.outbox_blocked")


def _database_path(value: Any) -> str:
    try:
        result = os.fspath(value)
    except TypeError:
        raise _blocked("source_delivery_runtime.database_path_invalid") from None
    if isinstance(result, bytes):
        raise _blocked("source_delivery_runtime.database_path_invalid")
    if (
        not isinstance(result, str)
        or not result
        or "\x00" in result
        or result.startswith("file:")
    ):
        raise _blocked("source_delivery_runtime.database_path_invalid")
    if result == ":memory:":
        return result
    if (
        os.name != "posix"
        or not os.path.isabs(result)
        or os.path.normpath(result) != result
    ):
        raise _blocked("source_delivery_runtime.database_path_invalid")
    return result


def _validate_database_parent(database_path: str) -> None:
    current = os.path.dirname(database_path)
    first = True
    while True:
        try:
            current_stat = os.lstat(current)
        except OSError:
            raise _blocked(
                "source_delivery_runtime.database_parent_unavailable"
            ) from None
        mode = stat.S_IMODE(current_stat.st_mode)
        sticky_root_directory = (
            current_stat.st_uid == 0
            and bool(mode & stat.S_ISVTX)
            and bool(mode & 0o002)
        )
        if (
            not stat.S_ISDIR(current_stat.st_mode)
            or current_stat.st_uid not in {0, os.geteuid()}
            or (first and current_stat.st_uid != os.geteuid())
            or (mode & 0o022 and not sticky_root_directory)
        ):
            raise _blocked("source_delivery_runtime.database_parent_unsafe")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        first = False


def _validate_database_file(
    database_path: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    try:
        database_stat = os.lstat(database_path)
    except FileNotFoundError:
        raise
    except OSError:
        raise _blocked("source_delivery_runtime.database_unavailable") from None
    identity = (database_stat.st_dev, database_stat.st_ino)
    if (
        not stat.S_ISREG(database_stat.st_mode)
        or database_stat.st_uid != os.geteuid()
        or database_stat.st_nlink != 1
        or stat.S_IMODE(database_stat.st_mode) != 0o600
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise _blocked("source_delivery_runtime.database_unsafe")
    return identity


def _sqlite_timeout(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= 60
    ):
        raise _blocked("source_delivery_runtime.sqlite_timeout_invalid")
    return float(value)


def _prepare_database_file(database_path: str) -> Callable[[], None]:
    if database_path == ":memory:":
        return lambda: None
    _validate_database_parent(database_path)
    try:
        identity = _validate_database_file(database_path)
    except FileNotFoundError:
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(database_path, flags, 0o600)
        except FileExistsError:
            identity = _validate_database_file(database_path)
        except OSError:
            raise _blocked(
                "source_delivery_runtime.database_create_failed"
            ) from None
        else:
            try:
                os.fchmod(descriptor, 0o600)
                created_stat = os.fstat(descriptor)
                identity = (created_stat.st_dev, created_stat.st_ino)
                if (
                    not stat.S_ISREG(created_stat.st_mode)
                    or created_stat.st_uid != os.geteuid()
                    or created_stat.st_nlink != 1
                    or stat.S_IMODE(created_stat.st_mode) != 0o600
                ):
                    raise _blocked(
                        "source_delivery_runtime.database_create_failed"
                    )
            finally:
                os.close(descriptor)

    def guard() -> None:
        _validate_database_parent(database_path)
        try:
            _validate_database_file(database_path, identity)
        except FileNotFoundError:
            raise _blocked(
                "source_delivery_runtime.database_unavailable"
            ) from None

    guard()
    return guard


def _duration(value: Any, code: str, *, maximum: float = 86_400) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= maximum
    ):
        raise _blocked(code)
    return float(value)


def _preflight(
    client: Any,
    worker_id: str,
    lease_seconds: float | int,
    outbox_options: Mapping[str, Any],
) -> float:
    connection = sqlite3.connect(":memory:")
    try:
        outbox = _DELIVERY.DurableCMSSourceDeliveryOutbox(
            connection, client, **dict(outbox_options),
        )
        lease = _duration(
            lease_seconds, "source_delivery_runtime.lease_invalid",
        )
        outbox.claim(worker_id, lease_seconds=lease)
        return lease
    except DurableCMSSourceDeliveryRuntimeBlocked:
        raise
    except Exception as error:
        raise _blocked("source_delivery_runtime.configuration_invalid") from error
    finally:
        connection.close()


class DurableCMSSourceDeliveryRuntime:
    """One process-owned, guarded, thread-safe source-delivery runtime."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        guard: Callable[[], None],
        outbox: Any,
        *,
        worker_id: str,
        lease_seconds: float,
        http_authenticator: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self._connection = connection
        self._guard = guard
        self._outbox = outbox
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
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
            else _HTTP.CMSSourceDeliveryHTTPApplication(
                self, http_authenticator,
            )
        )

    def __repr__(self) -> str:
        return f"DurableCMSSourceDeliveryRuntime(state={self.state!r})"

    def __enter__(self) -> "DurableCMSSourceDeliveryRuntime":
        self._assert_owner()
        if self._closed:
            raise _blocked("source_delivery_runtime.closed")
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _assert_owner(self) -> None:
        # Check before the lock: a child must never wait on a lock inherited
        # from a vanished parent thread.
        if os.getpid() != self._owner_pid:
            raise _blocked("source_delivery_runtime.foreign_process")

    def _guard_database(self) -> None:
        self._guard()

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("source_delivery_runtime.closed")
            self._guard_database()
            try:
                result = getattr(self._outbox, name)(*args, **kwargs)
            except Exception as error:
                raise _delivery_failure(error) from error
            finally:
                self._guard_database()
            return result

    def _require_worker_locked(self) -> None:
        if self._worker_state == "unmanaged":
            return
        if (
            self._worker_state != "running"
            or self._worker_error_code is not None
            or self._worker_thread is None
            or not self._worker_thread.is_alive()
        ):
            raise _blocked("source_delivery_runtime.worker_not_ready")

    def enqueue_change(
        self,
        change: Mapping[str, Any],
        *,
        source_max_attempts: int = 5,
        delivery_max_attempts: int = 5,
    ) -> Any:
        self._assert_owner()
        with self._lock:
            self._require_worker_locked()
            return self._call(
                "enqueue_change",
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
        self._assert_owner()
        with self._lock:
            self._require_worker_locked()
            return self._call(
                "enqueue_removal",
                removal,
                source_max_attempts=source_max_attempts,
                delivery_max_attempts=delivery_max_attempts,
            )

    def status(self, operation: str, request_id: str) -> Any:
        return self._call("status", operation, request_id)

    def source_status(
        self, event_id: str, site_id: str, payload_sha256: str,
    ) -> Mapping[str, Any]:
        """Read the source lifecycle through the exact accepted outbox row."""

        return self._call(
            "source_status", event_id, site_id, payload_sha256,
        )

    def source_readiness(self) -> Mapping[str, Any]:
        """Read the immutable downstream source-service readiness contract."""

        return self._call("source_readiness")

    def source_health(self) -> Mapping[str, Any]:
        """Read the immutable downstream source-service health contract."""

        return self._call("source_health")

    def health(self) -> Any:
        return self._call("health")

    @property
    def expected_capabilities_sha256(self) -> str:
        """Return the immutable source-service contract pinned by the outbox."""

        self._assert_owner()
        return self._outbox.capabilities_sha256

    def run_once(self) -> Any:
        return self._call(
            "run_once",
            self._worker_id,
            lease_seconds=self._lease_seconds,
        )

    @staticmethod
    def _loop_delays(
        active_delay_seconds: float | int,
        idle_delay_seconds: float | int,
        blocked_delay_seconds: float | int,
    ) -> tuple[float, float, float]:
        return (
            _duration(
                active_delay_seconds,
                "source_delivery_runtime.loop_invalid",
                maximum=300,
            ),
            _duration(
                idle_delay_seconds,
                "source_delivery_runtime.loop_invalid",
                maximum=300,
            ),
            _duration(
                blocked_delay_seconds,
                "source_delivery_runtime.loop_invalid",
                maximum=300,
            ),
        )

    @staticmethod
    def _join_timeout(value: float | int) -> float:
        return _duration(
            value, "source_delivery_runtime.worker_timeout_invalid",
            maximum=300,
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
                    "source_delivery_runtime.worker_blocked"
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
        """Start one interruptible process-owned background worker."""

        delays = self._loop_delays(
            active_delay_seconds,
            idle_delay_seconds,
            blocked_delay_seconds,
        )
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("source_delivery_runtime.closed")
            if self._worker_state == "running":
                return
            if self._worker_state in {"stopping", "failed"}:
                raise _blocked("source_delivery_runtime.worker_blocked")
            self._guard_database()
            stop = threading.Event()
            thread = threading.Thread(
                target=self._managed_worker,
                args=(stop, delays),
                name="cms-source-delivery-worker",
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
                    "source_delivery_runtime.worker_blocked"
                )
                raise _blocked(
                    "source_delivery_runtime.worker_blocked"
                ) from error

    def stop_worker(self, *, timeout_seconds: float | int = 30) -> None:
        """Signal and join the worker without closing durable state."""

        timeout = self._join_timeout(timeout_seconds)
        self._assert_owner()
        thread = self._worker_thread
        state = self._worker_state
        if thread is None or state in {"unmanaged", "stopped"}:
            return
        if thread is threading.current_thread():
            raise _blocked("source_delivery_runtime.worker_stop_invalid")
        if state != "failed":
            self._worker_state = "stopping"
        self._worker_stop.set()
        thread.join(timeout)
        if thread.is_alive():
            raise _blocked("source_delivery_runtime.worker_stop_timeout")
        with self._lock:
            if self._worker_state == "stopping":
                self._worker_state = "stopped"

    def require_worker_ready(self) -> None:
        """Block managed intake unless its worker is alive and healthy."""

        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("source_delivery_runtime.closed")
            self._require_worker_locked()

    def worker_readiness(self) -> dict[str, Any]:
        """Return a content-free worker and durable-outbox snapshot."""

        self._assert_owner()
        with self._lock:
            state = "closed" if self._closed else self._worker_state
            error_code = self._worker_error_code
            thread = self._worker_thread
            alive = thread is not None and thread.is_alive()
        if state != "running" or not alive:
            return {
                "schema": "blun.cms-source-delivery-worker-readiness.v1",
                "status": "not_ready",
                "worker_state": state,
                "outbox_status": None,
                "error_code": error_code
                or "source_delivery_runtime.worker_not_ready",
            }
        health = self.health()
        outbox_status = getattr(health, "status", None)
        if outbox_status not in {"ok", "blocked"}:
            raise _blocked("source_delivery_runtime.outbox_blocked")
        ready = outbox_status == "ok"
        return {
            "schema": "blun.cms-source-delivery-worker-readiness.v1",
            "status": "ready" if ready else "not_ready",
            "worker_state": "running",
            "outbox_status": outbox_status,
            "error_code": (
                None
                if ready
                else getattr(health, "error_code", None)
                or "source_delivery_runtime.outbox_blocked"
            ),
        }

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
            raise _blocked("source_delivery_runtime.loop_invalid")
        active, idle, blocked = self._loop_delays(
            active_delay_seconds,
            idle_delay_seconds,
            blocked_delay_seconds,
        )
        while not stop():
            outcome = self.run_once()
            if outcome is None:
                delay = idle
            elif outcome.status in {"retry_wait", "failed"}:
                delay = blocked
            else:
                delay = active
            sleeper(delay)

    def close(self, *, worker_timeout_seconds: float | int = 30) -> None:
        self._assert_owner()
        self.stop_worker(timeout_seconds=worker_timeout_seconds)
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.close()
            except sqlite3.Error as error:
                raise _blocked("source_delivery_runtime.close_failed") from error
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


def open_durable_cms_source_delivery(
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
) -> DurableCMSSourceDeliveryRuntime:
    """Validate configuration, then open one guarded durable outbox."""

    if http_authenticator is not None and not callable(http_authenticator):
        raise _blocked("source_delivery_runtime.configuration_invalid")
    path = _database_path(database)
    timeout = _sqlite_timeout(sqlite_timeout_seconds)
    outbox_options = {
        "clock": clock,
        "base_delay_seconds": base_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
    }
    lease = _preflight(client, worker_id, lease_seconds, outbox_options)
    guard = _prepare_database_file(path)
    connection = None
    try:
        connection = sqlite3.connect(
            path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        guard()
        outbox = _DELIVERY.DurableCMSSourceDeliveryOutbox(
            connection, client, **outbox_options,
        )
        guard()
    except Exception as error:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if isinstance(error, DurableCMSSourceDeliveryRuntimeBlocked):
            raise
        if isinstance(error, _DELIVERY.CMSSourceDeliveryBlocked):
            raise _delivery_failure(error) from error
        raise _blocked("source_delivery_runtime.initialization_failed") from error
    return DurableCMSSourceDeliveryRuntime(
        connection,
        guard,
        outbox,
        worker_id=worker_id,
        lease_seconds=lease,
        http_authenticator=http_authenticator,
    )


def open_hosted_cms_source_delivery(
    *args: Any,
    active_delay_seconds: float | int = 0.05,
    idle_delay_seconds: float | int = 1,
    blocked_delay_seconds: float | int = 5,
    **kwargs: Any,
) -> DurableCMSSourceDeliveryRuntime:
    """Open the guarded outbox and start its supervised worker."""

    delays = DurableCMSSourceDeliveryRuntime._loop_delays(
        active_delay_seconds,
        idle_delay_seconds,
        blocked_delay_seconds,
    )
    runtime = open_durable_cms_source_delivery(*args, **kwargs)
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
