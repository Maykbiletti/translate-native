#!/usr/bin/env python3
"""Protected runtime and hosted composition for source-delivery HMAC auth."""

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
from typing import Any, Callable, Iterable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"cannot load source-delivery HMAC runtime dependency: {path.name}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_AUTH = _load_module(
    "blun_website_localization_cms_source_delivery_auth_runtime_auth",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_auth.py",
)
_DELIVERY_RUNTIME = _load_module(
    "blun_website_localization_cms_source_delivery_auth_runtime_delivery",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_runtime.py",
)
HMACCredential = _AUTH.HMACCredential
RotatingSourceDeliveryHMACSigner = _AUTH.RotatingSourceDeliveryHMACSigner


class SourceDeliveryHMACRuntimeBlocked(RuntimeError):
    """Stable runtime failure without paths, secrets, or private exceptions."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _blocked(code: str) -> SourceDeliveryHMACRuntimeBlocked:
    return SourceDeliveryHMACRuntimeBlocked("source_delivery_hmac_runtime." + code)


def _database_path(value: Any) -> str:
    try:
        result = os.fspath(value)
    except TypeError:
        raise _blocked("database_path_invalid") from None
    if isinstance(result, bytes):
        raise _blocked("database_path_invalid")
    if (
        not isinstance(result, str)
        or not result
        or "\x00" in result
        or result.startswith("file:")
    ):
        raise _blocked("database_path_invalid")
    if result == ":memory:":
        return result
    if (
        os.name != "posix"
        or not os.path.isabs(result)
        or os.path.normpath(result) != result
    ):
        raise _blocked("database_path_invalid")
    return result


def _sqlite_timeout(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= 60
    ):
        raise _blocked("sqlite_timeout_invalid")
    return float(value)


def _validate_parent(database_path: str) -> None:
    current = os.path.dirname(database_path)
    first = True
    while True:
        try:
            current_stat = os.lstat(current)
        except OSError:
            raise _blocked("database_parent_unavailable") from None
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
            raise _blocked("database_parent_unsafe")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        first = False


def _validate_file(
    database_path: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    try:
        database_stat = os.lstat(database_path)
    except FileNotFoundError:
        raise
    except OSError:
        raise _blocked("database_unavailable") from None
    identity = (database_stat.st_dev, database_stat.st_ino)
    if (
        not stat.S_ISREG(database_stat.st_mode)
        or database_stat.st_uid != os.geteuid()
        or database_stat.st_nlink != 1
        or stat.S_IMODE(database_stat.st_mode) != 0o600
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise _blocked("database_unsafe")
    return identity


def _prepare_database_file(database_path: str) -> Callable[[], None]:
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
            raise _blocked("database_create_failed") from None
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
                    raise _blocked("database_create_failed")
            finally:
                os.close(descriptor)

    def guard() -> None:
        _validate_parent(database_path)
        try:
            _validate_file(database_path, identity)
        except FileNotFoundError:
            raise _blocked("database_unavailable") from None

    guard()
    return guard


def _same_database_identity(first_path: str, second_path: str) -> bool:
    if ":memory:" in {first_path, second_path}:
        return False
    try:
        first = os.lstat(first_path)
        second = os.lstat(second_path)
    except OSError:
        raise _blocked("database_identity_unavailable") from None
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _credential_values(
    credentials: Iterable[Any],
) -> tuple[Any, ...]:
    try:
        values = tuple(credentials)
    except Exception:
        raise _blocked("configuration_invalid") from None
    if not values:
        raise _blocked("configuration_invalid")
    return values


def _preflight_authentication(
    credentials: tuple[Any, ...],
    *,
    origin: str,
    sidecar_capabilities_sha256: str,
    remote_capabilities_sha256: str,
    clock: Callable[[], float | int],
    max_age_seconds: float | int,
    future_skew_seconds: float | int,
    allow_loopback_http: bool,
) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        store = _AUTH.DurableHMACReplayStore(connection)
        _AUTH.SourceDeliveryHMACVerifier(
            credentials,
            store,
            origin=origin,
            sidecar_capabilities_sha256=sidecar_capabilities_sha256,
            remote_capabilities_sha256=remote_capabilities_sha256,
            clock=clock,
            max_age_seconds=max_age_seconds,
            future_skew_seconds=future_skew_seconds,
            allow_loopback_http=allow_loopback_http,
        )
    except Exception as error:
        raise _blocked("configuration_invalid") from error
    finally:
        connection.close()


class DurableSourceDeliveryHMACRuntime:
    """Own one guarded replay ledger and its request verifier."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        guard: Callable[[], None],
        store: Any,
        verifier: Any,
        verifier_configuration: Mapping[str, Any],
    ):
        self._connection = connection
        self._guard = guard
        self._store = store
        self._verifier = verifier
        self._verifier_configuration = dict(verifier_configuration)
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._closed = False

    def __repr__(self) -> str:
        return f"DurableSourceDeliveryHMACRuntime(state={self.state!r})"

    def __enter__(self) -> "DurableSourceDeliveryHMACRuntime":
        self._assert_owner()
        if self._closed:
            raise _blocked("closed")
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _assert_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise _blocked("foreign_process")

    def _call(self, callback: Callable[[], Any]) -> Any:
        self._assert_owner()
        with self._lock:
            if self._closed:
                raise _blocked("closed")
            self._guard()
            try:
                return callback()
            finally:
                self._guard()

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, str] | None:
        return self._call(lambda: self._verifier(request))

    def replace_credentials(self, credentials: Iterable[Any]) -> None:
        """Atomically replace accepted generations without resetting replay state."""

        self._assert_owner()
        values = _credential_values(credentials)
        with self._lock:
            if self._closed:
                raise _blocked("closed")
            self._guard()
            try:
                replacement = _AUTH.SourceDeliveryHMACVerifier(
                    values,
                    self._store,
                    **self._verifier_configuration,
                )
            except Exception as error:
                raise _blocked("configuration_invalid") from error
            self._guard()
            self._verifier = replacement

    def health(self) -> Mapping[str, Any]:
        try:
            store_health = self._call(self._store.health)
            if (
                not isinstance(store_health, Mapping)
                or store_health.get("status") != "ok"
                or not isinstance(store_health.get("consumed_nonces"), int)
                or isinstance(store_health.get("consumed_nonces"), bool)
                or store_health["consumed_nonces"] < 0
            ):
                raise _blocked("replay_store_blocked")
            return {
                "schema": "blun.cms-source-delivery-hmac-runtime-health.v1",
                "status": "ok",
                "runtime_state": "open",
                "consumed_nonces": store_health["consumed_nonces"],
                "error_code": None,
            }
        except Exception as error:
            code = getattr(error, "code", None)
            if not (
                isinstance(code, str)
                and code.startswith((
                    "source_delivery_hmac_runtime.",
                    "source_delivery_hmac.",
                ))
            ):
                code = "source_delivery_hmac_runtime.health_unavailable"
            return {
                "schema": "blun.cms-source-delivery-hmac-runtime-health.v1",
                "status": "blocked",
                "runtime_state": self.state,
                "consumed_nonces": None,
                "error_code": code,
            }

    def close(self) -> None:
        self._assert_owner()
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


def open_durable_source_delivery_hmac_runtime(
    replay_database: str | os.PathLike[str],
    credentials: Iterable[Any],
    *,
    origin: str,
    sidecar_capabilities_sha256: str,
    remote_capabilities_sha256: str,
    clock: Callable[[], float | int] = time.time,
    max_age_seconds: float | int = 60,
    future_skew_seconds: float | int = 5,
    sqlite_timeout_seconds: float | int = 5,
    allow_loopback_http: bool = False,
) -> DurableSourceDeliveryHMACRuntime:
    """Preflight configuration, then open one private replay ledger."""

    if not isinstance(allow_loopback_http, bool):
        raise _blocked("configuration_invalid")
    path = _database_path(replay_database)
    timeout = _sqlite_timeout(sqlite_timeout_seconds)
    values = _credential_values(credentials)
    _preflight_authentication(
        values,
        origin=origin,
        sidecar_capabilities_sha256=sidecar_capabilities_sha256,
        remote_capabilities_sha256=remote_capabilities_sha256,
        clock=clock,
        max_age_seconds=max_age_seconds,
        future_skew_seconds=future_skew_seconds,
        allow_loopback_http=allow_loopback_http,
    )
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
        store = _AUTH.DurableHMACReplayStore(connection)
        verifier_configuration = {
            "origin": origin,
            "sidecar_capabilities_sha256": sidecar_capabilities_sha256,
            "remote_capabilities_sha256": remote_capabilities_sha256,
            "clock": clock,
            "max_age_seconds": max_age_seconds,
            "future_skew_seconds": future_skew_seconds,
            "allow_loopback_http": allow_loopback_http,
        }
        verifier = _AUTH.SourceDeliveryHMACVerifier(
            values, store, **verifier_configuration,
        )
        guard()
    except Exception as error:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if isinstance(error, SourceDeliveryHMACRuntimeBlocked):
            raise
        raise _blocked("initialization_failed") from error
    return DurableSourceDeliveryHMACRuntime(
        connection, guard, store, verifier, verifier_configuration,
    )


class HostedHMACAuthenticatedCMSSourceDelivery:
    """Own the hosted delivery runtime and its HMAC replay runtime."""

    def __init__(self, delivery: Any, authentication: Any):
        self.delivery = delivery
        self.authentication = authentication
        self.http = delivery.http

    def __repr__(self) -> str:
        return (
            "HostedHMACAuthenticatedCMSSourceDelivery("
            f"state={self.state!r}, worker_state={self.worker_state!r})"
        )

    def __enter__(self) -> "HostedHMACAuthenticatedCMSSourceDelivery":
        if self.state != "open":
            raise _blocked("closed")
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def enqueue_change(self, *args: Any, **kwargs: Any) -> Any:
        return self.delivery.enqueue_change(*args, **kwargs)

    def enqueue_removal(self, *args: Any, **kwargs: Any) -> Any:
        return self.delivery.enqueue_removal(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Any:
        return self.delivery.status(*args, **kwargs)

    def health(self) -> Any:
        return self.delivery.health()

    def worker_readiness(self) -> Mapping[str, Any]:
        return self.delivery.worker_readiness()

    def authentication_health(self) -> Mapping[str, Any]:
        return self.authentication.health()

    def replace_credentials(self, credentials: Iterable[Any]) -> None:
        self.authentication.replace_credentials(credentials)

    def close(self, *, worker_timeout_seconds: float | int = 30) -> None:
        self.delivery.close(worker_timeout_seconds=worker_timeout_seconds)
        self.authentication.close()

    @property
    def state(self) -> str:
        states = {self.delivery.state, self.authentication.state}
        if "foreign-process" in states:
            return "foreign-process"
        if states == {"closed"}:
            return "closed"
        if states == {"open"}:
            return "open"
        return "blocked"

    @property
    def worker_state(self) -> str:
        return self.delivery.worker_state


def open_hosted_hmac_authenticated_cms_source_delivery(
    outbox_database: str | os.PathLike[str],
    replay_database: str | os.PathLike[str],
    client: Any,
    credentials: Iterable[Any],
    *,
    worker_id: str,
    origin: str,
    sidecar_capabilities_sha256: str,
    remote_capabilities_sha256: str,
    clock: Callable[[], float | int] = time.time,
    sqlite_timeout_seconds: float | int = 5,
    replay_sqlite_timeout_seconds: float | int = 5,
    lease_seconds: float | int = 600,
    base_delay_seconds: float | int = 5,
    max_delay_seconds: float | int = 300,
    max_age_seconds: float | int = 60,
    future_skew_seconds: float | int = 5,
    active_delay_seconds: float | int = 0.05,
    idle_delay_seconds: float | int = 1,
    blocked_delay_seconds: float | int = 5,
    allow_loopback_http: bool = False,
) -> HostedHMACAuthenticatedCMSSourceDelivery:
    """Open one hosted sidecar with a separately guarded replay ledger."""

    if not isinstance(allow_loopback_http, bool):
        raise _blocked("configuration_invalid")
    try:
        outbox_path = _DELIVERY_RUNTIME._database_path(outbox_database)
    except Exception as error:
        raise _blocked("configuration_invalid") from error
    replay_path = _database_path(replay_database)
    if outbox_path == replay_path:
        raise _blocked("database_paths_conflict")
    values = _credential_values(credentials)
    _sqlite_timeout(replay_sqlite_timeout_seconds)
    _preflight_authentication(
        values,
        origin=origin,
        sidecar_capabilities_sha256=sidecar_capabilities_sha256,
        remote_capabilities_sha256=remote_capabilities_sha256,
        clock=clock,
        max_age_seconds=max_age_seconds,
        future_skew_seconds=future_skew_seconds,
        allow_loopback_http=allow_loopback_http,
    )
    try:
        _DELIVERY_RUNTIME._sqlite_timeout(sqlite_timeout_seconds)
        _DELIVERY_RUNTIME.DurableCMSSourceDeliveryRuntime._loop_delays(
            active_delay_seconds,
            idle_delay_seconds,
            blocked_delay_seconds,
        )
        _DELIVERY_RUNTIME._preflight(
            client,
            worker_id,
            lease_seconds,
            {
                "clock": clock,
                "base_delay_seconds": base_delay_seconds,
                "max_delay_seconds": max_delay_seconds,
            },
        )
    except Exception as error:
        raise _blocked("configuration_invalid") from error

    authentication = open_durable_source_delivery_hmac_runtime(
        replay_path,
        values,
        origin=origin,
        sidecar_capabilities_sha256=sidecar_capabilities_sha256,
        remote_capabilities_sha256=remote_capabilities_sha256,
        clock=clock,
        max_age_seconds=max_age_seconds,
        future_skew_seconds=future_skew_seconds,
        sqlite_timeout_seconds=replay_sqlite_timeout_seconds,
        allow_loopback_http=allow_loopback_http,
    )
    delivery = None
    try:
        delivery = _DELIVERY_RUNTIME.open_durable_cms_source_delivery(
            outbox_path,
            client,
            worker_id=worker_id,
            clock=clock,
            sqlite_timeout_seconds=sqlite_timeout_seconds,
            lease_seconds=lease_seconds,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            http_authenticator=authentication,
        )
        if _same_database_identity(outbox_path, replay_path):
            delivery.close()
            authentication.close()
            raise _blocked("database_paths_conflict")
        delivery.start_worker(
            active_delay_seconds=active_delay_seconds,
            idle_delay_seconds=idle_delay_seconds,
            blocked_delay_seconds=blocked_delay_seconds,
        )
    except Exception as error:
        if delivery is not None and delivery.state == "open":
            try:
                delivery.close()
            except Exception:
                pass
        if authentication.state == "open":
            authentication.close()
        if isinstance(error, SourceDeliveryHMACRuntimeBlocked):
            raise
        raise _blocked("delivery_initialization_failed") from error
    return HostedHMACAuthenticatedCMSSourceDelivery(
        delivery, authentication,
    )
