#!/usr/bin/env python3
"""Crash-durable, deployment-bound reconcile-only admission latch."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
from pathlib import Path
from typing import Any


SCHEMA = "translate-native.subagent-review-drain-latch.v1"
MAX_BYTES = 4096
SHA256 = __import__("re").compile(r"^[0-9a-f]{64}$")
LEDGER_TABLE = "subagent_reconcile_only_state"
LEDGER_COLUMNS = (
    "singleton", "schema", "component", "deployment_binding_sha256",
    "facility_ledger_instance_id", "mode", "drain_id",
)


def _trigger_statements(component: str) -> dict[str, str]:
    table = ("subagent_executor_jobs" if component == "executor"
             else "subagent_facility_jobs")
    prefix = f"subagent_{component}_reconcile_only"
    return {
        prefix + "_insert": (
            f"CREATE TRIGGER {prefix}_insert BEFORE INSERT ON {table} "
            "WHEN NEW.status='dispatching' BEGIN "
            "SELECT RAISE(ABORT,'reconcile_only'); END"
        ),
        prefix + "_update": (
            f"CREATE TRIGGER {prefix}_update BEFORE UPDATE OF status ON {table} "
            "WHEN NEW.status='dispatching' BEGIN "
            "SELECT RAISE(ABORT,'reconcile_only'); END"
        ),
    }


class SubagentDrainError(RuntimeError):
    """The protected drain latch is missing, unsafe or inconsistent."""


def _raw(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _document(component: str, deployment_binding_sha256: str,
              facility_ledger_instance_id: str | None,
              drain_id: str) -> dict:
    if component not in {"executor", "facility"}:
        raise SubagentDrainError("drain component is invalid")
    if not isinstance(deployment_binding_sha256, str) \
            or SHA256.fullmatch(deployment_binding_sha256) is None:
        raise SubagentDrainError("drain deployment binding is invalid")
    if component == "facility":
        if not isinstance(facility_ledger_instance_id, str) \
                or SHA256.fullmatch(facility_ledger_instance_id) is None:
            raise SubagentDrainError("facility ledger instance is invalid")
    elif facility_ledger_instance_id is not None:
        raise SubagentDrainError("executor drain cannot bind a facility ledger")
    if not isinstance(drain_id, str) or SHA256.fullmatch(drain_id) is None:
        raise SubagentDrainError("drain identifier is invalid")
    return {
        "schema": SCHEMA,
        "component": component,
        "mode": "reconcile_only",
        "deployment_binding_sha256": deployment_binding_sha256,
        "facility_ledger_instance_id": facility_ledger_instance_id,
        "drain_id": drain_id,
    }


def _ledger_state(
        ledger_path: Path, *, component: str,
        deployment_binding_sha256: str,
        facility_ledger_instance_id: str | None, begin: bool) -> tuple[str, str | None]:
    """Migrate/read the authoritative monotone state inside the locked ledger."""
    try:
        connection = sqlite3.connect(ledger_path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"""
                CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema TEXT NOT NULL,
                    component TEXT NOT NULL,
                    deployment_binding_sha256 TEXT NOT NULL,
                    facility_ledger_instance_id TEXT,
                    mode TEXT NOT NULL,
                    drain_id TEXT
                )
            """)
            columns = tuple(row[1] for row in connection.execute(
                f"PRAGMA table_info({LEDGER_TABLE})"
            ))
            if columns != LEDGER_COLUMNS:
                raise SubagentDrainError("drain ledger schema is invalid")
            rows = connection.execute(
                f"SELECT * FROM {LEDGER_TABLE}"
            ).fetchall()
            binding = (
                SCHEMA, component, deployment_binding_sha256,
                facility_ledger_instance_id,
            )
            if not rows:
                connection.execute(
                    f"INSERT INTO {LEDGER_TABLE} VALUES (1,?,?,?,?,?,NULL)",
                    (*binding, "execute_and_reconcile"),
                )
                mode, drain_id = "execute_and_reconcile", None
            elif len(rows) == 1 and rows[0][0] == 1:
                row = rows[0]
                if tuple(row[1:5]) != binding:
                    raise SubagentDrainError("drain ledger binding changed")
                mode, drain_id = row[5], row[6]
            else:
                raise SubagentDrainError("drain ledger state is invalid")
            if mode == "execute_and_reconcile":
                if drain_id is not None:
                    raise SubagentDrainError("drain ledger state is invalid")
                if begin:
                    drain_id = hashlib.sha256(os.urandom(32)).hexdigest()
                    changed = connection.execute(
                        f"UPDATE {LEDGER_TABLE} SET mode='reconcile_only', "
                        "drain_id=? WHERE singleton=1 "
                        "AND mode='execute_and_reconcile' AND drain_id IS NULL",
                        (drain_id,),
                    ).rowcount
                    if changed != 1:
                        raise SubagentDrainError("drain ledger transition failed")
                    mode = "reconcile_only"
            elif (mode != "reconcile_only"
                    or not isinstance(drain_id, str)
                    or SHA256.fullmatch(drain_id) is None):
                raise SubagentDrainError("drain ledger state is invalid")
            triggers = _trigger_statements(component)
            if mode == "reconcile_only" and begin:
                for statement in triggers.values():
                    connection.execute(statement.replace(
                        "CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1,
                    ))
            stored_triggers = dict(connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                f"AND name IN ({','.join('?' for _ in triggers)})",
                tuple(triggers),
            ))
            if mode == "execute_and_reconcile":
                if stored_triggers:
                    raise SubagentDrainError("unexpected drain trigger exists")
            elif (set(stored_triggers) != set(triggers)
                    or any(stored_triggers[name] != statement
                           for name, statement in triggers.items())):
                raise SubagentDrainError("drain trigger binding changed")
            connection.commit()
            return mode, drain_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except SubagentDrainError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise SubagentDrainError("drain ledger state is unavailable") from error


def marker_path(ledger_path: Path) -> Path:
    if not isinstance(ledger_path, Path) or not ledger_path.is_absolute() \
            or len(str(ledger_path)) > 4096:
        raise SubagentDrainError("drain ledger path is invalid")
    return ledger_path.with_name(ledger_path.name + ".reconcile-only.json")


def _read(path: Path, protected: Any) -> dict | None:
    try:
        with protected._open_directory(path) as directory:
            try:
                protected._lstat(path, directory)
            except FileNotFoundError:
                return None
        raw = protected._read_protected(path, MAX_BYTES)
    except Exception as error:
        raise SubagentDrainError("drain latch is unavailable") from error
    try:
        text = raw.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise SubagentDrainError("drain latch is invalid") from error
    if not isinstance(value, dict):
        raise SubagentDrainError("drain latch is invalid")
    return value


def _rename_noreplace(directory: int, source: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SubagentDrainError("atomic drain latch creation is unsupported")
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(
            directory, source.encode("utf-8"), directory,
            target.encode("utf-8"), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(target)
        raise OSError(code, os.strerror(code), target)


def _create(path: Path, raw: bytes, protected: Any) -> None:
    descriptor = None
    temporary_name = "." + path.name + ".create-" + secrets.token_hex(16)
    temporary_identity = None
    try:
        with protected._open_directory(path) as directory:
            if directory is None:
                raise SubagentDrainError(
                    "atomic drain latch creation requires protected POSIX storage"
                )
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
            opened = os.fstat(descriptor)
            temporary_identity = (opened.st_dev, opened.st_ino)
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            details = os.stat(temporary_name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                    or (details.st_dev, details.st_ino) != temporary_identity
                    or stat.S_IMODE(details.st_mode) != 0o600
                    or (hasattr(os, "getuid") and details.st_uid != os.getuid())):
                raise SubagentDrainError("drain latch changed during creation")
            try:
                _rename_noreplace(directory, temporary_name, path.name)
            except FileExistsError:
                existing = protected._read_protected(path, MAX_BYTES)
                if not hmac.compare_digest(existing, raw):
                    raise SubagentDrainError("conflicting drain latch exists")
            else:
                temporary_name = ""
                os.fsync(directory)
                if not hmac.compare_digest(
                        protected._read_protected(path, MAX_BYTES), raw):
                    raise SubagentDrainError("drain latch verification failed")
    except SubagentDrainError:
        raise
    except Exception as error:
        raise SubagentDrainError("drain latch could not be created") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name and temporary_identity is not None:
            try:
                with protected._open_directory(path) as directory:
                    details = os.stat(
                        temporary_name, dir_fd=directory, follow_symlinks=False,
                    )
                    if (details.st_dev, details.st_ino) == temporary_identity:
                        os.unlink(temporary_name, dir_fd=directory)
                        os.fsync(directory)
            except Exception:
                pass


def load_or_create(
        ledger_path: Path, *, component: str, deployment_binding_sha256: str,
        facility_ledger_instance_id: str | None, begin: bool,
        protected: Any) -> dict | None:
    """Load the monotone latch or atomically create it under the runtime lock."""
    if type(begin) is not bool:
        raise SubagentDrainError("begin drain flag must be boolean")
    path = marker_path(ledger_path)
    existing = _read(path, protected)
    mode, drain_id = _ledger_state(
        ledger_path, component=component,
        deployment_binding_sha256=deployment_binding_sha256,
        facility_ledger_instance_id=facility_ledger_instance_id, begin=begin,
    )
    if mode == "execute_and_reconcile":
        if existing is not None:
            raise SubagentDrainError("unexpected drain latch exists")
        return None
    expected = _document(
        component, deployment_binding_sha256, facility_ledger_instance_id,
        drain_id,
    )
    if existing is None:
        if not begin:
            raise SubagentDrainError("drain latch is missing")
        _create(path, _raw(expected), protected)
        existing = _read(path, protected)
    if (not isinstance(existing, dict)
            or set(existing) != set(expected)
            or existing != expected):
        raise SubagentDrainError("drain latch binding changed")
    return existing
