#!/usr/bin/env python3
"""Owned, durable runtime for the source side of the CMS localization API.

The composition root validates the complete service configuration before it
opens three private SQLite databases. One runtime belongs to one process and
serializes every local state transition. Separate processes may safely open
the same files because the underlying outboxes and monitor use durable leases.
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
        raise RuntimeError(f"cannot load source runtime dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_SERVICE = _load_module(
    "blun_website_localization_composed_cms_source_service",
    _ROOT / "integrations" / "website_localization_cms_source_service.py",
)
_HTTP = _load_module(
    "blun_website_localization_composed_cms_source_http",
    _ROOT / "integrations" / "website_localization_cms_source_http.py",
)


class DurableCMSSourceRuntimeBlocked(RuntimeError):
    """Stable runtime failure that contains no path, credential, or content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _blocked(code: str) -> DurableCMSSourceRuntimeBlocked:
    return DurableCMSSourceRuntimeBlocked(code)


def _service_failure(error: Exception) -> DurableCMSSourceRuntimeBlocked:
    code = getattr(error, "code", None)
    if code in {
        "dispatch.change_invalid",
        "dispatch.max_attempts_invalid",
        "removal.request_invalid",
        "removal.max_attempts_invalid",
        "source_service.status_invalid",
    }:
        return _blocked("source_runtime.request_invalid")
    if code in {
        "dispatch.idempotency_collision",
        "removal.idempotency_collision",
    }:
        return _blocked("source_runtime.idempotency_collision")
    if code == "source_service.status_not_found":
        return _blocked("source_runtime.status_not_found")
    return _blocked("source_runtime.service_blocked")


def _database_path(value: Any) -> str:
    try:
        result = os.fspath(value)
    except TypeError:
        raise _blocked("source_runtime.database_path_invalid") from None
    if isinstance(result, bytes):
        raise _blocked("source_runtime.database_path_invalid")
    if (
        not isinstance(result, str)
        or not result
        or "\x00" in result
        or result.startswith("file:")
    ):
        raise _blocked("source_runtime.database_path_invalid")
    if result == ":memory:":
        return result
    if (
        os.name != "posix"
        or not os.path.isabs(result)
        or os.path.normpath(result) != result
    ):
        raise _blocked("source_runtime.database_path_invalid")
    return result


def _validate_database_parent(database_path: str) -> None:
    current = os.path.dirname(database_path)
    first = True
    while True:
        try:
            current_stat = os.lstat(current)
        except OSError:
            raise _blocked("source_runtime.database_parent_unavailable") from None
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
            raise _blocked("source_runtime.database_parent_unsafe")
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
        raise _blocked("source_runtime.database_unavailable") from None
    identity = (database_stat.st_dev, database_stat.st_ino)
    if (
        not stat.S_ISREG(database_stat.st_mode)
        or database_stat.st_uid != os.geteuid()
        or database_stat.st_nlink != 1
        or stat.S_IMODE(database_stat.st_mode) != 0o600
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise _blocked("source_runtime.database_unsafe")
    return identity


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
            raise _blocked("source_runtime.database_create_failed") from None
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
                    raise _blocked("source_runtime.database_create_failed")
            finally:
                os.close(descriptor)

    def guard() -> None:
        _validate_database_parent(database_path)
        try:
            _validate_database_file(database_path, identity)
        except FileNotFoundError:
            raise _blocked("source_runtime.database_unavailable") from None

    guard()
    return guard


def _validate_paths(paths: tuple[str, str, str]) -> None:
    filesystem_paths = [path for path in paths if path != ":memory:"]
    if len(set(filesystem_paths)) != len(filesystem_paths):
        raise _blocked("source_runtime.database_path_reused")
    identities: set[tuple[int, int]] = set()
    for path in filesystem_paths:
        _validate_database_parent(path)
        try:
            identity = _validate_database_file(path)
        except FileNotFoundError:
            continue
        if identity in identities:
            raise _blocked("source_runtime.database_identity_reused")
        identities.add(identity)


def _sqlite_timeout(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= 60
    ):
        raise _blocked("source_runtime.sqlite_timeout_invalid")
    return float(value)


def _preflight_service(
    client: Any,
    service_options: Mapping[str, Any],
) -> None:
    connections = [sqlite3.connect(":memory:") for _ in range(3)]
    try:
        _SERVICE.CMSLocalizationSourceService(
            *connections,
            client,
            **dict(service_options),
        )
    except _SERVICE.CMSSourceServiceBlocked as error:
        raise _blocked("source_runtime.configuration_invalid") from error
    except (TypeError, ValueError, sqlite3.Error) as error:
        raise _blocked("source_runtime.configuration_invalid") from error
    except Exception as error:
        raise _blocked("source_runtime.configuration_invalid") from error
    finally:
        for connection in connections:
            connection.close()


class DurableCMSSourceRuntime:
    """One process-owned and thread-safe source-CMS service runtime."""

    def __init__(
        self,
        connections: tuple[sqlite3.Connection, sqlite3.Connection, sqlite3.Connection],
        guards: tuple[Callable[[], None], Callable[[], None], Callable[[], None]],
        service: Any,
        http_authenticator: Callable[[dict[str, Any]], Any] | None,
    ):
        self._connections = connections
        self._guards = guards
        self._service = service
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
            else _HTTP.CMSSourceHTTPApplication(self, http_authenticator)
        )

    def __repr__(self) -> str:
        return f"DurableCMSSourceRuntime(state={self.state!r})"

    def __enter__(self) -> "DurableCMSSourceRuntime":
        self._assert_owner()
        if self._closed:
            raise _blocked("source_runtime.closed")
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _assert_owner(self) -> None:
        # This must precede lock acquisition: a lock held by a vanished parent
        # thread after fork cannot safely be acquired by the child process.
        if os.getpid() != self._owner_pid:
            raise _blocked("source_runtime.foreign_process")

    def _guard_databases(self) -> None:
        for guard in self._guards:
            guard()

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("source_runtime.closed")
            self._guard_databases()
            try:
                result = getattr(self._service, name)(*args, **kwargs)
            except Exception as error:
                raise _service_failure(error) from error
            finally:
                self._guard_databases()
            return result

    def enqueue_change(
        self, change: Mapping[str, Any], *, max_attempts: int = 5,
    ) -> Any:
        return self._call("enqueue_change", change, max_attempts=max_attempts)

    def enqueue_removal(
        self, request: Mapping[str, Any], *, max_attempts: int = 5,
    ) -> Any:
        return self._call("enqueue_removal", request, max_attempts=max_attempts)

    def status(self, event_id: str, site_id: str) -> Any:
        return self._call("status", event_id, site_id)

    def run_once(self) -> Any:
        return self._call("run_once")

    def health(self) -> Any:
        return self._call("health")

    @staticmethod
    def _loop_delays(
        active_delay_seconds: float | int,
        idle_delay_seconds: float | int,
        blocked_delay_seconds: float | int,
    ) -> tuple[float, float, float]:
        try:
            return tuple(
                _SERVICE._positive_duration(
                    value, "source_service.delay_invalid",
                )
                for value in (
                    active_delay_seconds,
                    idle_delay_seconds,
                    blocked_delay_seconds,
                )
            )
        except Exception as error:
            raise _blocked("source_runtime.loop_invalid") from error

    @staticmethod
    def _join_timeout(value: float | int) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 300
        ):
            raise _blocked("source_runtime.worker_timeout_invalid")
        return float(value)

    def _managed_worker(
        self,
        stop: threading.Event,
        delays: tuple[float, float, float],
    ) -> None:
        try:
            with self._lock:
                if self._worker_state != "starting" or self._worker_stop is not stop:
                    return
                self._worker_state = "running"
            active, idle, blocked = delays
            while not stop.is_set():
                outcome = self.run_once()
                delay = (
                    blocked
                    if outcome.status in {"blocked", "failed", "retry_wait"}
                    else idle if outcome.status == "idle" else active
                )
                stop.wait(delay)
        except Exception:
            with self._lock:
                self._worker_error_code = "source_runtime.worker_blocked"
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
        """Start one interruptible, process-owned background worker."""

        delays = self._loop_delays(
            active_delay_seconds,
            idle_delay_seconds,
            blocked_delay_seconds,
        )
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("source_runtime.closed")
            if self._worker_state in {"starting", "running"}:
                return
            if self._worker_state in {"stopping", "failed"}:
                raise _blocked("source_runtime.worker_blocked")
            self._guard_databases()
            stop = threading.Event()
            thread = threading.Thread(
                target=self._managed_worker,
                args=(stop, delays),
                name="cms-source-worker",
                daemon=False,
            )
            self._worker_stop = stop
            self._worker_thread = thread
            self._worker_error_code = None
            self._worker_state = "starting"
            try:
                thread.start()
            except Exception as error:
                self._worker_thread = None
                self._worker_state = "failed"
                self._worker_error_code = "source_runtime.worker_blocked"
                raise _blocked("source_runtime.worker_blocked") from error

    def stop_worker(self, *, timeout_seconds: float | int = 30) -> None:
        """Stop and join the managed worker without closing durable state."""

        timeout = self._join_timeout(timeout_seconds)
        self._assert_owner()
        # Signal before taking the runtime lock. A provider call may currently
        # hold that lock, and shutdown still needs a real upper time bound.
        thread = self._worker_thread
        state = self._worker_state
        if thread is None or state in {"unmanaged", "stopped"}:
            return
        if thread is threading.current_thread():
            raise _blocked("source_runtime.worker_stop_invalid")
        if state != "failed":
            self._worker_state = "stopping"
        self._worker_stop.set()
        thread.join(timeout)
        if thread.is_alive():
            raise _blocked("source_runtime.worker_stop_timeout")
        with self._lock:
            if self._worker_state == "stopping":
                self._worker_state = "stopped"

    def require_worker_ready(self) -> None:
        """Block hosted HTTP writes unless their managed worker is running."""

        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("source_runtime.closed")
            if self._worker_state == "unmanaged":
                return
            if self._worker_state != "running" or self._worker_error_code is not None:
                raise _blocked("source_runtime.worker_not_ready")

    def worker_readiness(self) -> dict[str, Any]:
        """Return a content-free readiness snapshot for supervised hosts."""

        self._assert_owner()
        with self._lock:
            state = "closed" if self._closed else self._worker_state
            error_code = self._worker_error_code
        if state != "running":
            return {
                "schema": "blun.cms-source-worker-readiness.v1",
                "status": "not_ready",
                "worker_state": state,
                "service_status": None,
                "error_code": error_code or "source_runtime.worker_not_ready",
            }
        health = self.health()
        service_status = getattr(health, "status", None)
        if service_status not in {"ok", "degraded", "blocked"}:
            raise _blocked("source_runtime.service_blocked")
        ready = service_status != "blocked"
        return {
            "schema": "blun.cms-source-worker-readiness.v1",
            "status": "ready" if ready else "not_ready",
            "worker_state": "running",
            "service_status": service_status,
            "error_code": (
                None if ready else "source_runtime.service_blocked"
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
            raise _blocked("source_runtime.loop_invalid")
        active, idle, blocked = self._loop_delays(
            active_delay_seconds,
            idle_delay_seconds,
            blocked_delay_seconds,
        )
        while not stop():
            outcome = self.run_once()
            delay = (
                blocked if outcome.status in {"blocked", "failed", "retry_wait"}
                else idle if outcome.status == "idle"
                else active
            )
            sleeper(delay)

    def close(self, *, worker_timeout_seconds: float | int = 30) -> None:
        self._assert_owner()
        self.stop_worker(timeout_seconds=worker_timeout_seconds)
        with self._lock:
            if self._closed:
                return
            failed = False
            for connection in self._connections:
                try:
                    connection.close()
                except sqlite3.Error:
                    failed = True
            self._closed = True
            if failed:
                raise _blocked("source_runtime.close_failed")

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


def open_durable_cms_source(
    change_database: str | os.PathLike[str],
    removal_database: str | os.PathLike[str],
    lifecycle_database: str | os.PathLike[str],
    client: Any,
    *,
    change_worker_id: str,
    removal_worker_id: str,
    lifecycle_worker_id: str,
    terminal_notifier: Callable[[Mapping[str, Any]], Any] | None = None,
    terminal_status_reader: Callable[[str, str], Any] | None = None,
    notification_worker_id: str | None = None,
    http_authenticator: Callable[[dict[str, Any]], Any] | None = None,
    clock: Callable[[], float | int] = time.time,
    sqlite_timeout_seconds: float | int = 5,
    change_lease_seconds: float | int = 600,
    removal_lease_seconds: float | int = 600,
    lifecycle_lease_seconds: float | int = 600,
    notification_lease_seconds: float | int = 600,
    terminal_processing_lease_seconds: float | int = 600,
    max_lifecycle_failures: int = 5,
    max_notification_attempts: int = 5,
    max_terminal_processing_failures: int = 5,
    dispatch_base_delay_seconds: float | int = 5,
    dispatch_max_delay_seconds: float | int = 300,
    lifecycle_poll_interval_seconds: float | int = 30,
    lifecycle_base_delay_seconds: float | int = 5,
    lifecycle_max_delay_seconds: float | int = 300,
    notification_base_delay_seconds: float | int = 5,
    notification_max_delay_seconds: float | int = 300,
    terminal_processing_poll_interval_seconds: float | int = 30,
    terminal_processing_base_delay_seconds: float | int = 5,
    terminal_processing_max_delay_seconds: float | int = 300,
) -> DurableCMSSourceRuntime:
    """Validate and open the complete durable source-side CMS worker."""

    paths = tuple(_database_path(value) for value in (
        change_database, removal_database, lifecycle_database,
    ))
    timeout = _sqlite_timeout(sqlite_timeout_seconds)
    if http_authenticator is not None and not callable(http_authenticator):
        raise _blocked("source_runtime.http_authenticator_invalid")
    service_options = {
        "change_worker_id": change_worker_id,
        "removal_worker_id": removal_worker_id,
        "lifecycle_worker_id": lifecycle_worker_id,
        "terminal_notifier": terminal_notifier,
        "terminal_status_reader": terminal_status_reader,
        "notification_worker_id": notification_worker_id,
        "clock": clock,
        "change_lease_seconds": change_lease_seconds,
        "removal_lease_seconds": removal_lease_seconds,
        "lifecycle_lease_seconds": lifecycle_lease_seconds,
        "notification_lease_seconds": notification_lease_seconds,
        "terminal_processing_lease_seconds": terminal_processing_lease_seconds,
        "max_lifecycle_failures": max_lifecycle_failures,
        "max_notification_attempts": max_notification_attempts,
        "max_terminal_processing_failures": max_terminal_processing_failures,
        "dispatch_base_delay_seconds": dispatch_base_delay_seconds,
        "dispatch_max_delay_seconds": dispatch_max_delay_seconds,
        "lifecycle_poll_interval_seconds": lifecycle_poll_interval_seconds,
        "lifecycle_base_delay_seconds": lifecycle_base_delay_seconds,
        "lifecycle_max_delay_seconds": lifecycle_max_delay_seconds,
        "notification_base_delay_seconds": notification_base_delay_seconds,
        "notification_max_delay_seconds": notification_max_delay_seconds,
        "terminal_processing_poll_interval_seconds": (
            terminal_processing_poll_interval_seconds
        ),
        "terminal_processing_base_delay_seconds": (
            terminal_processing_base_delay_seconds
        ),
        "terminal_processing_max_delay_seconds": (
            terminal_processing_max_delay_seconds
        ),
    }
    _preflight_service(client, service_options)
    _validate_paths(paths)
    guards = tuple(_prepare_database_file(path) for path in paths)
    connections: list[sqlite3.Connection] = []
    try:
        for path in paths:
            connections.append(sqlite3.connect(
                path,
                timeout=timeout,
                isolation_level=None,
                check_same_thread=False,
            ))
        for guard in guards:
            guard()
        service = _SERVICE.CMSLocalizationSourceService(
            *connections,
            client,
            **service_options,
        )
        for guard in guards:
            guard()
    except Exception as error:
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if isinstance(error, DurableCMSSourceRuntimeBlocked):
            raise
        raise _blocked("source_runtime.initialization_failed") from error
    return DurableCMSSourceRuntime(
        tuple(connections), guards, service, http_authenticator,
    )


def open_hosted_cms_source(
    *args: Any,
    active_delay_seconds: float | int = 0.05,
    idle_delay_seconds: float | int = 1,
    blocked_delay_seconds: float | int = 5,
    **kwargs: Any,
) -> DurableCMSSourceRuntime:
    """Open a durable runtime and start its supervised background worker."""

    runtime = open_durable_cms_source(*args, **kwargs)
    try:
        runtime.start_worker(
            active_delay_seconds=active_delay_seconds,
            idle_delay_seconds=idle_delay_seconds,
            blocked_delay_seconds=blocked_delay_seconds,
        )
    except Exception:
        runtime.close()
        raise
    return runtime
