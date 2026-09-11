#!/usr/bin/env python3
"""Safe composition root for the durable terminal-notification receiver."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load terminal receiver dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_RECEIVER = _load_module(
    "blun_website_localization_terminal_receiver_runtime_dependency",
    _ROOT
    / "integrations"
    / "website_localization_cms_terminal_notification_receiver.py",
)


class DurableTerminalReceiverRuntimeBlocked(RuntimeError):
    """Private composition failure that must not cross the HTTP boundary."""


@dataclass(frozen=True)
class DurableTerminalReceiverRuntimeHealth:
    status: str
    state: str
    received: int


def _database_path(value: Any) -> str:
    try:
        result = os.fspath(value)
    except TypeError as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "database path is invalid"
        ) from error
    if isinstance(result, bytes):
        raise DurableTerminalReceiverRuntimeBlocked(
            "database path must be Unicode"
        )
    if (
        not isinstance(result, str)
        or not result
        or "\x00" in result
        or result.startswith("file:")
    ):
        raise DurableTerminalReceiverRuntimeBlocked("database path is invalid")
    if result == ":memory:":
        return result
    if (
        os.name != "posix"
        or not os.path.isabs(result)
        or os.path.normpath(result) != result
    ):
        raise DurableTerminalReceiverRuntimeBlocked(
            "database path must be a canonical absolute POSIX path"
        )
    return result


def _validate_database_parent(database_path: str) -> None:
    current = os.path.dirname(database_path)
    first = True
    while True:
        try:
            current_stat = os.lstat(current)
        except OSError as error:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database directory is unavailable"
            ) from error
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
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database directory is not private"
            )
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
    except OSError as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver database file is unavailable"
        ) from error
    identity = (database_stat.st_dev, database_stat.st_ino)
    if (
        not stat.S_ISREG(database_stat.st_mode)
        or database_stat.st_uid != os.geteuid()
        or database_stat.st_nlink != 1
        or stat.S_IMODE(database_stat.st_mode) != 0o600
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver database file is not private"
        )
    return identity


def _prepare_database_file(database_path: str) -> Callable[[], None]:
    if database_path == ":memory:":
        return lambda: None
    _validate_database_parent(database_path)
    try:
        identity = _validate_database_file(database_path)
    except DurableTerminalReceiverRuntimeBlocked as error:
        if error.__cause__ is None or not isinstance(
            error.__cause__, FileNotFoundError
        ):
            raise
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(database_path, flags, 0o600)
        except FileExistsError:
            identity = _validate_database_file(database_path)
        except OSError as create_error:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database file could not be created"
            ) from create_error
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
                    raise DurableTerminalReceiverRuntimeBlocked(
                        "terminal receiver database file was not created privately"
                    )
            finally:
                os.close(descriptor)

    def guard() -> None:
        _validate_database_parent(database_path)
        _validate_database_file(database_path, identity)

    guard()
    return guard


def _preflight_application(
    authenticate: Callable[[Mapping[str, Any], Mapping[str, str]], Any],
    *,
    origin: str,
    clock: Callable[[], float | int],
    path: str,
    require_https: bool,
) -> None:
    dummy_inbox = object.__new__(_RECEIVER.DurableCMSTerminalNotificationInbox)
    try:
        _RECEIVER.CMSTerminalNotificationReceiverApplication(
            dummy_inbox,
            authenticate,
            origin=origin,
            clock=clock,
            path=path,
            require_https=require_https,
        )
    except (TypeError, ValueError) as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver configuration is invalid"
        ) from error


class DurableTerminalNotificationReceiverRuntime:
    """Own one protected SQLite connection and its complete WSGI boundary."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        inbox: Any,
        application: Any,
        database_guard: Callable[[], None],
    ):
        self._connection = connection
        self.inbox = inbox
        self.application = application
        self._database_guard = database_guard
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._closed = False

    def _assert_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver runtime belongs to another process"
            )

    def _require_open(self) -> None:
        self._assert_owner()
        if self._closed:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver runtime is closed"
            )
        try:
            self._database_guard()
        except Exception as error:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database is unsafe"
            ) from error

    @staticmethod
    def _unavailable(start_response: Callable[..., Any]):
        value = {
            "schema": _RECEIVER.ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": "notification_receiver.runtime_unavailable",
        }
        try:
            body = json.dumps(
                value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            start_response("503 Service Unavailable", (
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
            ))
            return [body]
        except Exception:
            return []

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            self._assert_owner()
            with self._lock:
                self._require_open()
                return self.application(environ, start_response)
        except Exception:
            return self._unavailable(start_response)

    def status(self, event_id: str):
        self._assert_owner()
        with self._lock:
            self._require_open()
            try:
                return self.inbox.status(event_id)
            except Exception as error:
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver status is unavailable"
                ) from error

    def health(self) -> DurableTerminalReceiverRuntimeHealth:
        self._assert_owner()
        with self._lock:
            self._require_open()
            try:
                health = self.inbox.health()
            except Exception as error:
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver health is unavailable"
                ) from error
            if health.status != "ok" or not isinstance(health.received, int):
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver health is invalid"
                )
            return DurableTerminalReceiverRuntimeHealth(
                "ok", "open", health.received,
            )

    def close(self) -> None:
        self._assert_owner()
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    @property
    def state(self) -> str:
        if os.getpid() != self._owner_pid:
            return "foreign-process"
        with self._lock:
            return "closed" if self._closed else "open"

    def __enter__(self) -> "DurableTerminalNotificationReceiverRuntime":
        self._require_open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def open_durable_terminal_notification_receiver(
    database: str | os.PathLike[str],
    authenticate: Callable[[Mapping[str, Any], Mapping[str, str]], Any],
    *,
    origin: str,
    clock: Callable[[], float | int] = time.time,
    path: str = _RECEIVER.DEFAULT_PATH,
    require_https: bool = True,
) -> DurableTerminalNotificationReceiverRuntime:
    """Validate all configuration before opening one protected SQLite worker."""

    database_path = _database_path(database)
    _preflight_application(
        authenticate,
        origin=origin,
        clock=clock,
        path=path,
        require_https=require_https,
    )
    database_guard = _prepare_database_file(database_path)
    try:
        connection = sqlite3.connect(
            database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
    except (OSError, sqlite3.Error) as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver database could not be opened"
        ) from error
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise sqlite3.DatabaseError("foreign keys unavailable")
        if connection.execute("PRAGMA secure_delete").fetchone()[0] != 1:
            raise sqlite3.DatabaseError("secure deletion unavailable")
        database_guard()
        inbox = _RECEIVER.DurableCMSTerminalNotificationInbox(
            connection, database_guard=database_guard,
        )
        application = _RECEIVER.CMSTerminalNotificationReceiverApplication(
            inbox,
            authenticate,
            origin=origin,
            clock=clock,
            path=path,
            require_https=require_https,
        )
    except Exception as error:
        connection.close()
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver runtime initialization failed"
        ) from error
    return DurableTerminalNotificationReceiverRuntime(
        connection, inbox, application, database_guard,
    )
