#!/usr/bin/env python3
"""Protected deployment runtime for the durable subagent executor."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Mapping
from wsgiref.simple_server import WSGIServer, make_server


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_PATH = ROOT / "integrations" / "website_localization_subagent_executor.py"
PROTECTED_PATH = ROOT / "integrations" / "response_subagent_https_runtime.py"
CONFIG_SCHEMA = "translate-native.subagent-review-executor-runtime.v1"
BACKEND_SCHEMA = "translate-native.subagent-review-backend-config.v1"
DEPLOYMENT_SCHEMA = "translate-native.subagent-review-executor-deployment.v1"
MAX_CONFIG_BYTES = 512 * 1024
MAX_FACTORY_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
CALLABLE_NAME = re.compile(r"^[A-Za-z_]\w*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FACTORY_EXECUTION_LOCK = threading.Lock()


def _load(name: str, path: Path):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


EXECUTOR = _load("blun_website_localization_subagent_executor", EXECUTOR_PATH)
PROTECTED = _load("blun_response_subagent_executor_protected", PROTECTED_PATH)


class SubagentExecutorRuntimeError(RuntimeError):
    """Content-free startup failure; no executor is exposed."""


class _MissingExecutorLedger(SubagentExecutorRuntimeError):
    """Internal signal allowing first initialization after local inspection."""


class _RuntimeLock:
    """Exclusive executor deployment lock shared with local bootstrap."""

    def __init__(self, ledger_path: Path):
        self.path = ledger_path.with_name(ledger_path.name + ".runtime.lock")
        self._handle = None
        try:
            with PROTECTED._open_directory(self.path) as directory:
                flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
                descriptor = (os.open(self.path, flags, 0o600)
                              if directory is None else os.open(
                                  self.path.name, flags, 0o600, dir_fd=directory,
                              ))
                handle = os.fdopen(descriptor, "r+b", buffering=0)
                linked = PROTECTED._lstat(self.path, directory)
                details = os.fstat(handle.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                    or _ledger_identity(details) != _ledger_identity(linked)
                    or (os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077)
                    or (hasattr(os, "getuid") and details.st_uid != os.getuid())):
                raise SubagentExecutorRuntimeError("executor runtime lock is unsafe")
            if os.name == "nt":
                import msvcrt
                if details.st_size == 0:
                    handle.write(b"0")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle = handle
        except PROTECTED.ResponseReviewRuntimeError as error:
            if "handle" in locals():
                handle.close()
            raise SubagentExecutorRuntimeError(
                "executor runtime lock directory is unsafe"
            ) from error
        except (BlockingIOError, OSError) as error:
            if "handle" in locals():
                handle.close()
            raise SubagentExecutorRuntimeError(
                "executor runtime is already active"
            ) from error
        except Exception:
            if "handle" in locals():
                handle.close()
            raise

    def close(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise SubagentExecutorRuntimeError(f"{name} is invalid")
    return value


def _protected_json(path_value: Any, *, name: str, maximum: int) -> tuple[dict, bytes]:
    if not isinstance(path_value, (str, Path)) or len(str(path_value)) > 4096:
        raise SubagentExecutorRuntimeError(f"{name} path is invalid")
    try:
        raw = PROTECTED._read_protected(Path(path_value), maximum)
        text = raw.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("BOM is not allowed")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentExecutorRuntimeError(f"{name} is unavailable") from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise SubagentExecutorRuntimeError(f"{name} is invalid JSON") from error
    if not isinstance(value, dict):
        raise SubagentExecutorRuntimeError(f"{name} must be an object")
    return value, raw


def _protected_text(path_value: Any, pattern: re.Pattern[str], name: str) -> str:
    try:
        return PROTECTED._protected_text(path_value, pattern, name)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentExecutorRuntimeError(f"{name} is unavailable or invalid") from error


def _route(value: Any):
    fields = {
        "route_id", "phase", "reviewer_role", "reviewer_agent_id",
        "model_id", "model_version", "host_policy_version", "target_locale",
        "content_type", "task_policy_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SubagentExecutorRuntimeError("executor route fields are invalid")
    try:
        return EXECUTOR.PinnedExecutorRoute(**value)
    except (TypeError, ValueError) as error:
        raise SubagentExecutorRuntimeError("executor route is invalid") from error


def load_executor_runtime_config(path: Path) -> tuple[dict, bytes]:
    value, raw = _protected_json(
        path, name="subagent executor configuration", maximum=MAX_CONFIG_BYTES,
    )
    expected = {
        "schema", "executor_id", "launcher_id", "launcher_version",
        "allow_loopback_http", "authentication", "ledger", "backend", "routes",
    }
    if set(value) != expected or value.get("schema") != CONFIG_SCHEMA:
        raise SubagentExecutorRuntimeError("executor configuration fields are invalid")
    for name in ("executor_id", "launcher_id", "launcher_version"):
        _identifier(value.get(name), name)
    if type(value.get("allow_loopback_http")) is not bool:
        raise SubagentExecutorRuntimeError("allow_loopback_http must be boolean")
    authentication = value.get("authentication")
    if (not isinstance(authentication, dict)
            or set(authentication) != {"scheme", "token_file"}
            or authentication.get("scheme") != "bearer"):
        raise SubagentExecutorRuntimeError("authentication configuration is invalid")
    ledger = value.get("ledger")
    if (not isinstance(ledger, dict)
            or set(ledger) != {"path", "max_concurrent_executions"}
            or not isinstance(ledger.get("path"), str)
            or len(ledger["path"]) > 4096 or not Path(ledger["path"]).is_absolute()
            or type(ledger.get("max_concurrent_executions")) is not int
            or not 1 <= ledger["max_concurrent_executions"] <= 256):
        raise SubagentExecutorRuntimeError("executor ledger configuration is invalid")
    backend = value.get("backend")
    backend_fields = {
        "factory_file", "factory_callable", "factory_sha256", "backend_id",
        "backend_version", "config_file",
    }
    if (not isinstance(backend, dict) or set(backend) != backend_fields
            or not isinstance(backend.get("factory_file"), str)
            or len(backend["factory_file"]) > 4096
            or not Path(backend["factory_file"]).is_absolute()
            or not isinstance(backend.get("factory_callable"), str)
            or CALLABLE_NAME.fullmatch(backend["factory_callable"]) is None
            or not isinstance(backend.get("factory_sha256"), str)
            or SHA256.fullmatch(backend["factory_sha256"]) is None
            or not isinstance(backend.get("config_file"), str)
            or len(backend["config_file"]) > 4096
            or not Path(backend["config_file"]).is_absolute()):
        raise SubagentExecutorRuntimeError("backend configuration is invalid")
    _identifier(backend.get("backend_id"), "backend_id")
    _identifier(backend.get("backend_version"), "backend_version")
    routes_value = value.get("routes")
    if not isinstance(routes_value, list) or not routes_value:
        raise SubagentExecutorRuntimeError("at least one executor route is required")
    routes = [_route(item) for item in routes_value]
    if len({item.route_id for item in routes}) != len(routes):
        raise SubagentExecutorRuntimeError("executor route IDs must be unique")
    try:
        EXECUTOR.PinnedExecutorPolicy(routes)
    except (TypeError, ValueError) as error:
        raise SubagentExecutorRuntimeError("executor route policy is invalid") from error
    return value, raw


def _backend_configuration(value: Mapping[str, Any]) -> tuple[dict, bytes]:
    document, raw = _protected_json(
        value["config_file"], name="subagent backend configuration",
        maximum=MAX_CONFIG_BYTES,
    )
    if (set(document) != {"schema", "backend_id", "backend_version", "settings"}
            or document.get("schema") != BACKEND_SCHEMA
            or document.get("backend_id") != value["backend_id"]
            or document.get("backend_version") != value["backend_version"]
            or not isinstance(document.get("settings"), dict)):
        raise SubagentExecutorRuntimeError("backend configuration fields are invalid")
    return document, raw


def _factory_file(config: Mapping[str, Any]):
    path = Path(config["factory_file"])
    try:
        raw = PROTECTED._read_protected(path, MAX_FACTORY_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(digest, config["factory_sha256"]):
            raise SubagentExecutorRuntimeError("backend factory digest does not match")
        code = compile(raw, str(path), "exec", dont_inherit=True)
        module_name = "_blun_subagent_backend_" + hashlib.sha256(
            str(path).encode("utf-8") + b"\0" + raw
        ).hexdigest()
        with _FACTORY_EXECUTION_LOCK:
            module = sys.modules.get(module_name)
            if module is None:
                module = types.ModuleType(module_name)
                module.__file__, module.__package__ = str(path), ""
                sys.modules[module_name] = module
                try:
                    exec(code, module.__dict__)
                except Exception:
                    if sys.modules.get(module_name) is module:
                        sys.modules.pop(module_name, None)
                    raise
        factory = getattr(module, config["factory_callable"], None)
    except SubagentExecutorRuntimeError:
        raise
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentExecutorRuntimeError("backend factory source is unavailable") from error
    except Exception as error:
        raise SubagentExecutorRuntimeError("backend factory is unavailable") from error
    if not callable(factory):
        raise SubagentExecutorRuntimeError("backend factory is invalid")
    return factory


class _ProcessBoundBackend:
    def __init__(self, backend: Any, backend_id: str, backend_version: str):
        self._backend, self._pid = backend, os.getpid()
        self.backend_id, self.backend_version = backend_id, backend_version

    def _check(self):
        if os.getpid() != self._pid:
            raise SubagentExecutorRuntimeError("executor runtime cannot be used after fork")

    def readiness(self):
        self._check()
        return self._backend.readiness()

    def execute_idempotent(self, assignment, model_input, **controls):
        self._check()
        return self._backend.execute_idempotent(assignment, model_input, **controls)

    def reconcile(self, assignment, **controls):
        self._check()
        return self._backend.reconcile(assignment, **controls)

    def close(self):
        closer = getattr(self._backend, "close", None)
        if callable(closer):
            closer()


def _build_backend(config: Mapping[str, Any], document: Mapping[str, Any]):
    factory = _factory_file(config)
    try:
        backend = factory(_copy(document["settings"]))
    except Exception as error:
        raise SubagentExecutorRuntimeError("backend factory failed") from error
    if (getattr(backend, "backend_id", None) != config["backend_id"]
            or getattr(backend, "backend_version", None) != config["backend_version"]
            or any(not callable(getattr(backend, name, None))
                   for name in ("readiness", "execute_idempotent", "reconcile"))):
        closer = getattr(backend, "close", None)
        if callable(closer):
            closer()
        raise SubagentExecutorRuntimeError("backend factory returned an invalid backend")
    return _ProcessBoundBackend(
        backend, config["backend_id"], config["backend_version"],
    )


def _ledger_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev, details.st_ino, details.st_nlink,
        stat.S_IMODE(details.st_mode), details.st_uid, details.st_gid,
    )


def _prepare_ledger(path: Path, *, initialize: bool):
    if not path.is_absolute():
        raise SubagentExecutorRuntimeError("executor ledger path must be absolute")
    try:
        with PROTECTED._open_directory(path) as directory:
            try:
                details = PROTECTED._lstat(path, directory)
            except FileNotFoundError:
                if not initialize:
                    raise _MissingExecutorLedger("executor ledger is missing") from None
                flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
                descriptor = (os.open(path, flags, 0o600) if directory is None
                              else os.open(path.name, flags, 0o600, dir_fd=directory))
                os.close(descriptor)
                details = PROTECTED._lstat(path, directory)
            if (not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode)
                    or details.st_nlink != 1
                    or (os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077)
                    or (hasattr(os, "getuid") and details.st_uid != os.getuid())):
                raise SubagentExecutorRuntimeError("executor ledger is unsafe")
            return _ledger_identity(details)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentExecutorRuntimeError("executor ledger directory is unsafe") from error
    except OSError as error:
        raise SubagentExecutorRuntimeError("executor ledger is unavailable") from error


def _bind_deployment(path: Path, binding_sha256: str, *, initialize: bool):
    try:
        target = str(path) if initialize else path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            target, timeout=10, isolation_level=None, uri=not initialize,
        )
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            if initialize:
                connection.execute("BEGIN IMMEDIATE")
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )}
                if tables and "subagent_executor_deployment" not in tables:
                    connection.rollback()
                    raise SubagentExecutorRuntimeError(
                        "executor ledger is not an initialized deployment"
                    )
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS subagent_executor_deployment (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        schema_name TEXT NOT NULL,
                        binding_sha256 TEXT NOT NULL
                    )
                """)
            row = connection.execute(
                "SELECT schema_name,binding_sha256 FROM subagent_executor_deployment "
                "WHERE singleton=1"
            ).fetchone()
            expected = (DEPLOYMENT_SCHEMA, binding_sha256)
            if row is None and initialize:
                connection.execute(
                    "INSERT INTO subagent_executor_deployment VALUES (1,?,?)", expected,
                )
            elif row is None:
                raise SubagentExecutorRuntimeError("executor ledger has no deployment binding")
            elif tuple(row) != expected:
                if initialize:
                    connection.rollback()
                raise SubagentExecutorRuntimeError("executor deployment binding changed")
            if initialize:
                connection.commit()
        finally:
            connection.close()
    except SubagentExecutorRuntimeError:
        raise
    except sqlite3.Error as error:
        raise SubagentExecutorRuntimeError("executor deployment ledger is invalid") from error


def _deployment_binding(config: Mapping[str, Any], backend_raw: bytes,
                        bearer: str) -> str:
    projection = {
        "schema": DEPLOYMENT_SCHEMA,
        "executor_id": config["executor_id"],
        "launcher_id": config["launcher_id"],
        "launcher_version": config["launcher_version"],
        "allow_loopback_http": config["allow_loopback_http"],
        "authentication_sha256": hashlib.sha256(bearer.encode("ascii")).hexdigest(),
        "max_concurrent_executions": config["ledger"]["max_concurrent_executions"],
        "backend": {
            key: config["backend"][key] for key in (
                "factory_file", "factory_callable", "factory_sha256",
                "backend_id", "backend_version",
            )
        } | {"config_sha256": hashlib.sha256(backend_raw).hexdigest()},
        "routes": config["routes"],
    }
    return hashlib.sha256(_canonical(projection)).hexdigest()


@dataclass
class SubagentExecutorRuntime:
    executor: Any
    backend: _ProcessBoundBackend
    ledger_path: Path
    ledger_identity: tuple[int, int, int, int, int, int]
    runtime_lock: _RuntimeLock

    def __post_init__(self):
        self._pid, self._closed, self._closing, self._active = (
            os.getpid(), False, False, 0,
        )
        self._condition = threading.Condition()
        self.application = self

    def __call__(self, environ, start_response):
        valid = False
        if not self._closed and os.getpid() == self._pid:
            try:
                details = self.ledger_path.lstat()
                valid = (stat.S_ISREG(details.st_mode)
                         and not stat.S_ISLNK(details.st_mode)
                         and _ledger_identity(details) == self.ledger_identity)
            except OSError:
                valid = False
        with self._condition:
            if not valid or self._closing or self._closed:
                admitted = False
            else:
                self._active += 1
                admitted = True
        if not admitted:
            body = b'{"error":{"code":"subagent_executor.runtime_unavailable","retryable":true}}'
            start_response("503 Service Unavailable", [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
            ])
            return [body]
        try:
            return self.executor(environ, start_response)
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        with self._condition:
            if self._closed:
                return
            if self._closing:
                while not self._closed:
                    self._condition.wait()
                return
            self._closing = True
            while self._active:
                self._condition.wait()
        try:
            self.backend.close()
        finally:
            try:
                self.runtime_lock.close()
            finally:
                with self._condition:
                    self._closed = True
                    self._condition.notify_all()


def open_subagent_executor_runtime(path: Path, *, initialize_ledger: bool = False):
    config, _config_raw = load_executor_runtime_config(path)
    ledger_path = Path(config["ledger"]["path"])
    runtime_lock = _RuntimeLock(ledger_path)
    backend = None
    try:
        backend_document, backend_raw = _backend_configuration(config["backend"])
        backend = _build_backend(config["backend"], backend_document)
        bearer = _protected_text(
            config["authentication"]["token_file"], PROTECTED.BEARER,
            "executor authentication token",
        )
        binding = _deployment_binding(config, backend_raw, bearer)
        try:
            initial_identity = _prepare_ledger(ledger_path, initialize=False)
        except SubagentExecutorRuntimeError as error:
            if not (initialize_ledger and isinstance(error, _MissingExecutorLedger)):
                raise
            initial_identity = None
        if initial_identity is not None:
            _bind_deployment(ledger_path, binding, initialize=False)
            if _prepare_ledger(ledger_path, initialize=False) != initial_identity:
                raise SubagentExecutorRuntimeError(
                    "executor ledger changed during startup"
                )
        try:
            readiness = backend.readiness()
            if (not isinstance(readiness, Mapping)
                    or readiness.get("ready") is not True):
                raise ValueError("backend readiness result is invalid")
        except Exception as error:
            raise SubagentExecutorRuntimeError(
                "host-subagent facility readiness failed"
            ) from error
        if initial_identity is None:
            initial_identity = _prepare_ledger(ledger_path, initialize=True)
            _bind_deployment(ledger_path, binding, initialize=True)
        _bind_deployment(ledger_path, binding, initialize=False)
        if _prepare_ledger(ledger_path, initialize=False) != initial_identity:
            raise SubagentExecutorRuntimeError("executor ledger changed during startup")
        routes = [_route(item) for item in config["routes"]]
        ledger = EXECUTOR.SQLiteExecutionLedger(
            ledger_path,
            max_concurrent_executions=config["ledger"]["max_concurrent_executions"],
            initialize_schema=initialize_ledger,
        )
        identity = _prepare_ledger(ledger_path, initialize=False)
        if identity != initial_identity:
            raise SubagentExecutorRuntimeError("executor ledger changed during startup")
        application = EXECUTOR.SubagentExecutorApplication(
            executor_id=config["executor_id"], launcher_id=config["launcher_id"],
            launcher_version=config["launcher_version"], bearer_token=bearer,
            policy=EXECUTOR.PinnedExecutorPolicy(routes), ledger=ledger,
            backend=backend, allow_loopback_http=config["allow_loopback_http"],
        )
        return SubagentExecutorRuntime(
            application, backend, ledger_path, identity, runtime_lock,
        )
    except Exception:
        if backend is not None:
            backend.close()
        runtime_lock.close()
        raise


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = False


def _loopback(value: str) -> bool:
    if value == "::1":
        return True
    try:
        return socket.gethostbyname(value).startswith("127.")
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the protected provider-neutral review-subagent executor",
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--initialize-ledger", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=47642)
    args = parser.parse_args()
    if not _loopback(args.listen_host) or not 1 <= args.listen_port <= 65535:
        print("BLOCK: executor must listen on loopback behind trusted TLS", file=sys.stderr)
        return 1
    runtime = None
    try:
        runtime = open_subagent_executor_runtime(
            args.config, initialize_ledger=args.initialize_ledger,
        )
        if args.check:
            print('{"ready":true,"content_free":true}')
            runtime.close()
            return 0
        server = make_server(
            args.listen_host, args.listen_port, runtime.application,
            server_class=_ThreadingWSGIServer,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(f"BLOCK: {error}", file=sys.stderr)
        if runtime is not None:
            runtime.close()
        return 1

    def stop(_signum=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
