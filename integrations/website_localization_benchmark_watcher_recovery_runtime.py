#!/usr/bin/env python3
"""Safe process-owned runtime for durable benchmark-watcher recovery.

The composition root validates the client and retry policy before creating an
owner-only SQLite file.  It serializes access to the runner, guards the file's
identity around every operation, and can supervise one interruptible worker.
Recovery still requires an explicit operation identity supplied by the host.
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
from typing import Any, Callable


def _load_runner():
    path = Path(__file__).resolve().with_name(
        "website_localization_benchmark_watcher_recovery_runner.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_benchmark_watcher_recovery_runtime_runner",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark watcher recovery runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_RUNNER = _load_runner()


class BenchmarkWatcherRecoveryRuntimeBlocked(RuntimeError):
    """Stable, content-free hosted-recovery failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _blocked(code: str) -> BenchmarkWatcherRecoveryRuntimeBlocked:
    return BenchmarkWatcherRecoveryRuntimeBlocked(code)


def _database_path(value: Any) -> str:
    try:
        result = os.fspath(value)
    except TypeError:
        raise _blocked("benchmark_watcher.recovery_runtime.database_path_invalid") from None
    if (
        isinstance(result, bytes) or not isinstance(result, str) or not result
        or "\x00" in result or result.startswith("file:")
    ):
        raise _blocked("benchmark_watcher.recovery_runtime.database_path_invalid")
    if result == ":memory:":
        return result
    if (
        os.name != "posix" or not os.path.isabs(result)
        or os.path.normpath(result) != result
    ):
        raise _blocked("benchmark_watcher.recovery_runtime.database_path_invalid")
    return result


def _validate_parent(database_path: str) -> None:
    current = os.path.dirname(database_path)
    first = True
    while True:
        try:
            value = os.lstat(current)
        except OSError:
            raise _blocked(
                "benchmark_watcher.recovery_runtime.database_parent_unavailable"
            ) from None
        mode = stat.S_IMODE(value.st_mode)
        sticky_root = (
            value.st_uid == 0 and bool(mode & stat.S_ISVTX)
            and bool(mode & 0o002)
        )
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid not in {0, os.geteuid()}
            or (first and value.st_uid != os.geteuid())
            or (mode & 0o022 and not sticky_root)
        ):
            raise _blocked(
                "benchmark_watcher.recovery_runtime.database_parent_unsafe"
            )
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent
        first = False


def _validate_file(
    database_path: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    try:
        value = os.lstat(database_path)
    except FileNotFoundError:
        raise
    except OSError:
        raise _blocked(
            "benchmark_watcher.recovery_runtime.database_unavailable"
        ) from None
    identity = (value.st_dev, value.st_ino)
    if (
        not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid()
        or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) != 0o600
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise _blocked("benchmark_watcher.recovery_runtime.database_unsafe")
    return identity


def _prepare_file(database_path: str) -> Callable[[], None]:
    if database_path == ":memory:":
        return lambda: None
    _validate_parent(database_path)
    try:
        identity = _validate_file(database_path)
    except FileNotFoundError:
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(database_path, flags, 0o600)
        except FileExistsError:
            identity = _validate_file(database_path)
        except OSError:
            raise _blocked(
                "benchmark_watcher.recovery_runtime.database_create_failed"
            ) from None
        else:
            try:
                os.fchmod(descriptor, 0o600)
                value = os.fstat(descriptor)
                identity = (value.st_dev, value.st_ino)
                if (
                    not stat.S_ISREG(value.st_mode)
                    or value.st_uid != os.geteuid() or value.st_nlink != 1
                    or stat.S_IMODE(value.st_mode) != 0o600
                ):
                    raise _blocked(
                        "benchmark_watcher.recovery_runtime.database_create_failed"
                    )
            finally:
                os.close(descriptor)

    def guard() -> None:
        _validate_parent(database_path)
        try:
            _validate_file(database_path, identity)
        except FileNotFoundError:
            raise _blocked(
                "benchmark_watcher.recovery_runtime.database_unavailable"
            ) from None

    guard()
    return guard


def _timeout(value: Any) -> float:
    if (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(float(value)) or not 0 < float(value) <= 60
    ):
        raise _blocked("benchmark_watcher.recovery_runtime.sqlite_timeout_invalid")
    return float(value)


def _duration(value: Any, code: str, maximum: float = 3600) -> float:
    if (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(float(value)) or not 0 < float(value) <= maximum
    ):
        raise _blocked(code)
    return float(value)


def _runner_failure(error: Exception) -> BenchmarkWatcherRecoveryRuntimeBlocked:
    value = str(error)
    if value.startswith("benchmark_watcher.recovery_runner."):
        suffix = value.removeprefix("benchmark_watcher.recovery_runner.")
        if suffix in {
            "operation_invalid", "operation_conflict", "worker_invalid",
            "time_invalid", "clock_invalid", "delay_invalid",
        }:
            return _blocked(f"benchmark_watcher.recovery_runtime.{suffix}")
    return _blocked("benchmark_watcher.recovery_runtime.runner_blocked")


def _preflight(client: Any, runner_options: dict[str, Any]) -> Any:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        return _RUNNER.DurableBenchmarkWatcherRecoveryRunner(
            connection, client, **runner_options,
        )
    except Exception as error:
        if isinstance(error, BenchmarkWatcherRecoveryRuntimeBlocked):
            raise
        raise _blocked(
            "benchmark_watcher.recovery_runtime.configuration_invalid"
        ) from error
    finally:
        connection.close()


def _preflight_existing(
    database_path: str,
    guard: Callable[[], None],
    prototype: Any,
) -> None:
    """Validate an existing store read-only before its first writable open."""
    if database_path == ":memory:":
        return
    connection = None
    try:
        guard()
        connection = sqlite3.connect(
            Path(database_path).as_uri() + "?mode=ro",
            uri=True, timeout=0, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        objects = connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        if not objects:
            guard()
            return
        expected = [
            (
                "table", "benchmark_watcher_recovery_runner",
                "benchmark_watcher_recovery_runner",
            ),
            (
                "table", "benchmark_watcher_recovery_runner_meta",
                "benchmark_watcher_recovery_runner_meta",
            ),
        ]
        if [tuple(row) for row in objects] != expected:
            raise _blocked(
                "benchmark_watcher.recovery_runtime.database_schema_altered"
            )
        meta_columns = tuple(
            row["name"] for row in connection.execute(
                "PRAGMA table_info(benchmark_watcher_recovery_runner_meta)"
            ).fetchall()
        )
        columns = tuple(
            row["name"] for row in connection.execute(
                "PRAGMA table_info(benchmark_watcher_recovery_runner)"
            ).fetchall()
        )
        meta = connection.execute(
            "SELECT singleton, schema_version "
            "FROM benchmark_watcher_recovery_runner_meta"
        ).fetchall()
        rows = connection.execute(
            "SELECT * FROM benchmark_watcher_recovery_runner"
        ).fetchall()
        if (
            meta_columns != _RUNNER._META_COLUMNS
            or columns != _RUNNER._COLUMNS
            or len(meta) != 1
            or tuple(meta[0]) != (1, _RUNNER.SCHEMA_VERSION)
            or len(rows) > 1
        ):
            raise _blocked(
                "benchmark_watcher.recovery_runtime.database_schema_altered"
            )
        if rows:
            try:
                prototype._validated_row(rows[0])
            except Exception as error:
                raise _blocked(
                    "benchmark_watcher.recovery_runtime.database_generation_invalid"
                ) from error
        guard()
    except BenchmarkWatcherRecoveryRuntimeBlocked:
        raise
    except sqlite3.Error:
        raise _blocked(
            "benchmark_watcher.recovery_runtime.database_preflight_failed"
        ) from None
    finally:
        if connection is not None:
            connection.close()


class DurableBenchmarkWatcherRecoveryRuntime:
    """One guarded process-owned recovery runner and optional worker."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        guard: Callable[[], None],
        runner: Any,
        *,
        worker_id: str,
        clock: Callable[[], float | int],
        maximum_wait_seconds: float,
    ):
        self._connection = connection
        self._guard = guard
        self._runner = runner
        self._worker_id = worker_id
        self._clock = clock
        self._maximum_wait_seconds = maximum_wait_seconds
        self._lock = threading.RLock()
        self._closed = False
        self._owner_pid = os.getpid()
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._worker_state = "unmanaged"
        self._worker_error_code: str | None = None

    def __enter__(self) -> "DurableBenchmarkWatcherRecoveryRuntime":
        self._assert_owner()
        if self._closed:
            raise _blocked("benchmark_watcher.recovery_runtime.closed")
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _assert_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise _blocked("benchmark_watcher.recovery_runtime.foreign_process")

    def _now(self) -> float | int:
        try:
            value = self._clock()
        except Exception:
            raise _blocked("benchmark_watcher.recovery_runtime.clock_invalid") from None
        if (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or not 0 <= float(value) <= 10**12
        ):
            raise _blocked("benchmark_watcher.recovery_runtime.clock_invalid")
        return value

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("benchmark_watcher.recovery_runtime.closed")
            self._guard()
            try:
                return getattr(self._runner, name)(*args, **kwargs)
            except BenchmarkWatcherRecoveryRuntimeBlocked:
                raise
            except Exception as error:
                raise _runner_failure(error) from error
            finally:
                self._guard()

    def start(self, operation_id: str) -> Any:
        """Record explicit operator intent without starting a worker."""
        return self._call("start", operation_id, now=self._now())

    def status(self) -> Any:
        return self._call("status", now=self._now())

    def run_once(self) -> Any:
        return self._call(
            "run_once", self._worker_id, now=self._now(),
        )

    def _managed_worker(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                self.run_once()
                snapshot = self.status()
                if snapshot.state in {"succeeded", "not_required", "failed"}:
                    break
                now = float(self._now())
                action_at = (
                    snapshot.lease_expires_at
                    if snapshot.state == "leased"
                    else snapshot.next_attempt_at
                )
                if action_at is None:
                    raise _blocked(
                        "benchmark_watcher.recovery_runtime.runner_blocked"
                    )
                stop.wait(min(
                    self._maximum_wait_seconds,
                    max(0.1, float(action_at) - now),
                ))
        except Exception:
            with self._lock:
                self._worker_error_code = (
                    "benchmark_watcher.recovery_runtime.worker_blocked"
                )
                self._worker_state = "failed"
            return
        with self._lock:
            if self._worker_state in {"running", "stopping"}:
                self._worker_state = "stopped"

    def start_worker(self) -> None:
        """Start one non-daemon worker after explicit recovery intent exists."""
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("benchmark_watcher.recovery_runtime.closed")
            if self._worker_state == "running":
                return
            if self._worker_state in {"stopping", "failed"}:
                raise _blocked(
                    "benchmark_watcher.recovery_runtime.worker_blocked"
                )
            # Status proves that start() already committed explicit intent.
            self._call("status", now=self._now())
            stop = threading.Event()
            thread = threading.Thread(
                target=self._managed_worker,
                args=(stop,),
                name="benchmark-watcher-recovery-worker",
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
                    "benchmark_watcher.recovery_runtime.worker_blocked"
                )
                raise _blocked(
                    "benchmark_watcher.recovery_runtime.worker_blocked"
                ) from error

    def stop_worker(self, *, timeout_seconds: float | int = 30) -> None:
        """Signal and join without closing storage beneath a live request."""
        timeout = _duration(
            timeout_seconds,
            "benchmark_watcher.recovery_runtime.worker_timeout_invalid",
            maximum=300,
        )
        self._assert_owner()
        thread = self._worker_thread
        state = self._worker_state
        if thread is None or state in {"unmanaged", "stopped"}:
            return
        if thread is threading.current_thread():
            raise _blocked(
                "benchmark_watcher.recovery_runtime.worker_stop_invalid"
            )
        if state != "failed":
            self._worker_state = "stopping"
        self._worker_stop.set()
        thread.join(timeout)
        if thread.is_alive():
            raise _blocked(
                "benchmark_watcher.recovery_runtime.worker_stop_timeout"
            )
        with self._lock:
            if self._worker_state == "stopping":
                self._worker_state = "stopped"

    def readiness(self) -> dict[str, Any]:
        """Return a content-free worker and durable recovery snapshot."""
        self._assert_owner()
        with self._lock:
            state = "closed" if self._closed else self._worker_state
            thread = self._worker_thread
            alive = thread is not None and thread.is_alive()
            error_code = self._worker_error_code
        if thread is None or state == "closed":
            return {
                "schema": "blun.website-localization-benchmark-watcher-recovery-runtime-readiness.v1",
                "status": "not_ready",
                "worker_state": state,
                "recovery_state": None,
                "recovery_phase": None,
                "attempts": None,
                "error_code": error_code or (
                    "benchmark_watcher.recovery_runtime.worker_not_ready"
                ),
            }
        snapshot = self.status()
        final = snapshot.state in {"succeeded", "not_required", "failed"}
        ready = state == "running" and alive and not final
        return {
            "schema": "blun.website-localization-benchmark-watcher-recovery-runtime-readiness.v1",
            "status": "ready" if ready else "not_ready",
            "worker_state": state,
            "recovery_state": snapshot.state,
            "recovery_phase": snapshot.phase,
            "attempts": snapshot.attempts,
            "error_code": (
                None if ready else error_code or snapshot.last_error_code or
                "benchmark_watcher.recovery_runtime.worker_not_ready"
            ),
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
                raise _blocked(
                    "benchmark_watcher.recovery_runtime.close_failed"
                ) from error
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


def open_durable_benchmark_watcher_recovery(
    database: str | os.PathLike[str],
    client: Any,
    *,
    worker_id: str,
    clock: Callable[[], float | int] = time.time,
    sqlite_timeout_seconds: float | int = 5,
    maximum_wait_seconds: float | int = 30,
    lease_seconds: float | int = 30,
    base_delay_seconds: float | int = 5,
    max_delay_seconds: float | int = 300,
    max_attempts: int = 20,
) -> DurableBenchmarkWatcherRecoveryRuntime:
    """Validate configuration, then open one guarded recovery runner."""
    if not callable(clock):
        raise _blocked(
            "benchmark_watcher.recovery_runtime.configuration_invalid"
        )
    path = _database_path(database)
    timeout = _timeout(sqlite_timeout_seconds)
    maximum_wait = _duration(
        maximum_wait_seconds,
        "benchmark_watcher.recovery_runtime.maximum_wait_invalid",
    )
    options = {
        "lease_seconds": lease_seconds,
        "base_delay_seconds": base_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
        "max_attempts": max_attempts,
    }
    prototype = _preflight(client, options)
    try:
        _RUNNER._worker(worker_id)
    except Exception as error:
        raise _blocked(
            "benchmark_watcher.recovery_runtime.configuration_invalid"
        ) from error
    guard = _prepare_file(path)
    _preflight_existing(path, guard, prototype)
    connection = None
    try:
        connection = sqlite3.connect(
            path, timeout=timeout, isolation_level=None,
            check_same_thread=False,
        )
        guard()
        runner = _RUNNER.DurableBenchmarkWatcherRecoveryRunner(
            connection, client, **options,
        )
        guard()
    except Exception as error:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if isinstance(error, BenchmarkWatcherRecoveryRuntimeBlocked):
            raise
        raise _blocked(
            "benchmark_watcher.recovery_runtime.initialization_failed"
        ) from error
    return DurableBenchmarkWatcherRecoveryRuntime(
        connection, guard, runner, worker_id=worker_id, clock=clock,
        maximum_wait_seconds=maximum_wait,
    )


def open_hosted_benchmark_watcher_recovery(
    *args: Any,
    operation_id: str,
    **kwargs: Any,
) -> DurableBenchmarkWatcherRecoveryRuntime:
    """Open, record explicit intent, and start the supervised worker."""
    runtime = open_durable_benchmark_watcher_recovery(*args, **kwargs)
    try:
        runtime.start(operation_id)
        runtime.start_worker()
    except Exception:
        runtime.close()
        raise
    return runtime
