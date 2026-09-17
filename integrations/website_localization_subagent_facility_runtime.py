#!/usr/bin/env python3
"""Protected runtime for the durable host-subagent facility.

The runtime loads one digest-pinned, provider-neutral host driver from
owner-only files.  It exposes only execute/reconcile capabilities to the
facility; Guard signing and publication authority are never passed in.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Mapping
from wsgiref.simple_server import WSGIServer, make_server


ROOT = Path(__file__).resolve().parents[1]
FACILITY_PATH = ROOT / "integrations" / "website_localization_subagent_facility.py"
PROTECTED_PATH = ROOT / "integrations" / "response_subagent_https_runtime.py"
CONFIG_SCHEMA = "translate-native.subagent-review-facility-runtime.v1"
DRIVER_SCHEMA = "translate-native.subagent-review-driver-config.v1"
DEPLOYMENT_SCHEMA = "translate-native.subagent-review-facility-deployment.v1"
MAX_CONFIG_BYTES = 512 * 1024
MAX_FACTORY_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
CALLABLE_NAME = re.compile(r"^[A-Za-z_]\w*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FACTORY_LOCK = threading.Lock()


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


FACILITY = _load("blun_website_localization_subagent_facility", FACILITY_PATH)
PROTECTED = _load("blun_subagent_facility_protected", PROTECTED_PATH)


class SubagentFacilityRuntimeError(RuntimeError):
    """Content-free startup failure; no facility is exposed."""


class _DriverWorkerFailed(RuntimeError):
    def __init__(self, *, retryable: bool):
        self.retryable = retryable
        super().__init__("driver worker failed")


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
        raise SubagentFacilityRuntimeError(f"{name} is invalid")
    return value


def _protected_json(path_value: Any, *, name: str) -> tuple[dict, bytes]:
    if not isinstance(path_value, (str, Path)) or len(str(path_value)) > 4096:
        raise SubagentFacilityRuntimeError(f"{name} path is invalid")
    try:
        raw = PROTECTED._read_protected(Path(path_value), MAX_CONFIG_BYTES)
        text = raw.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentFacilityRuntimeError(f"{name} is unavailable") from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise SubagentFacilityRuntimeError(f"{name} is invalid JSON") from error
    if not isinstance(value, dict):
        raise SubagentFacilityRuntimeError(f"{name} must be an object")
    return value, raw


def _protected_text(path_value: Any, pattern: re.Pattern[str], name: str) -> str:
    try:
        return PROTECTED._protected_text(path_value, pattern, name)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentFacilityRuntimeError(f"{name} is unavailable or invalid") from error


def _route(value: Any):
    fields = {
        "route_id", "phase", "reviewer_role", "reviewer_agent_id",
        "model_id", "model_version", "host_policy_version", "target_locale",
        "content_type", "task_policy_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SubagentFacilityRuntimeError("facility route fields are invalid")
    try:
        return FACILITY.EXECUTOR.PinnedExecutorRoute(**value)
    except (TypeError, ValueError) as error:
        raise SubagentFacilityRuntimeError("facility route is invalid") from error


def load_facility_runtime_config(path: Path) -> tuple[dict, bytes]:
    value, raw = _protected_json(path, name="subagent facility configuration")
    expected = {
        "schema", "backend_id", "backend_version", "facility_id",
        "facility_version", "allow_loopback_http", "authentication",
        "ledger", "driver", "routes",
    }
    if set(value) != expected or value.get("schema") != CONFIG_SCHEMA:
        raise SubagentFacilityRuntimeError("facility configuration fields are invalid")
    for name in ("backend_id", "backend_version", "facility_id", "facility_version"):
        _identifier(value.get(name), name)
    if type(value.get("allow_loopback_http")) is not bool:
        raise SubagentFacilityRuntimeError("allow_loopback_http must be boolean")
    authentication = value.get("authentication")
    if (not isinstance(authentication, dict)
            or set(authentication) != {"scheme", "token_file"}
            or authentication.get("scheme") != "bearer"):
        raise SubagentFacilityRuntimeError("authentication configuration is invalid")
    ledger = value.get("ledger")
    if (not isinstance(ledger, dict)
            or set(ledger) != {"path", "max_concurrent_executions"}
            or not isinstance(ledger.get("path"), str)
            or len(ledger["path"]) > 4096 or not Path(ledger["path"]).is_absolute()
            or type(ledger.get("max_concurrent_executions")) is not int
            or not 1 <= ledger["max_concurrent_executions"] <= 256):
        raise SubagentFacilityRuntimeError("facility ledger configuration is invalid")
    driver = value.get("driver")
    fields = {
        "factory_file", "factory_callable", "factory_sha256", "driver_id",
        "driver_version", "config_file",
    }
    if (not isinstance(driver, dict) or set(driver) != fields
            or not isinstance(driver.get("factory_file"), str)
            or len(driver["factory_file"]) > 4096
            or not Path(driver["factory_file"]).is_absolute()
            or not isinstance(driver.get("factory_callable"), str)
            or CALLABLE_NAME.fullmatch(driver["factory_callable"]) is None
            or not isinstance(driver.get("factory_sha256"), str)
            or SHA256.fullmatch(driver["factory_sha256"]) is None
            or not isinstance(driver.get("config_file"), str)
            or len(driver["config_file"]) > 4096
            or not Path(driver["config_file"]).is_absolute()):
        raise SubagentFacilityRuntimeError("driver configuration is invalid")
    _identifier(driver.get("driver_id"), "driver_id")
    _identifier(driver.get("driver_version"), "driver_version")
    routes_value = value.get("routes")
    if not isinstance(routes_value, list) or not routes_value:
        raise SubagentFacilityRuntimeError("at least one facility route is required")
    routes = [_route(item) for item in routes_value]
    if len({item.route_id for item in routes}) != len(routes):
        raise SubagentFacilityRuntimeError("facility route IDs must be unique")
    try:
        FACILITY.EXECUTOR.PinnedExecutorPolicy(routes)
    except (TypeError, ValueError) as error:
        raise SubagentFacilityRuntimeError("facility route policy is invalid") from error
    return value, raw


def _driver_configuration(value: Mapping[str, Any]) -> tuple[dict, bytes]:
    document, raw = _protected_json(
        value["config_file"], name="subagent driver configuration",
    )
    if (set(document) != {"schema", "driver_id", "driver_version", "settings"}
            or document.get("schema") != DRIVER_SCHEMA
            or document.get("driver_id") != value["driver_id"]
            or document.get("driver_version") != value["driver_version"]
            or not isinstance(document.get("settings"), dict)):
        raise SubagentFacilityRuntimeError("driver configuration fields are invalid")
    return document, raw


def _factory(config: Mapping[str, Any]):
    path = Path(config["factory_file"])
    try:
        raw = PROTECTED._read_protected(path, MAX_FACTORY_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(digest, config["factory_sha256"]):
            raise SubagentFacilityRuntimeError("driver factory digest does not match")
        code = compile(raw, str(path), "exec", dont_inherit=True)
        module_name = "_blun_subagent_driver_" + hashlib.sha256(
            str(path).encode("utf-8") + b"\0" + raw
        ).hexdigest()
        with _FACTORY_LOCK:
            module = sys.modules.get(module_name)
            if module is None:
                module = types.ModuleType(module_name)
                module.__file__, module.__package__ = str(path), ""
                sys.modules[module_name] = module
                try:
                    exec(code, module.__dict__)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise
        result = getattr(module, config["factory_callable"], None)
    except SubagentFacilityRuntimeError:
        raise
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentFacilityRuntimeError("driver factory source is unavailable") from error
    except Exception as error:
        raise SubagentFacilityRuntimeError("driver factory is unavailable") from error
    if not callable(result):
        raise SubagentFacilityRuntimeError("driver factory is invalid")
    return result


def _create_driver(config: Mapping[str, Any], document: Mapping[str, Any]):
    factory = _factory(config)
    try:
        driver = factory(_copy(document["settings"]))
    except Exception as error:
        raise SubagentFacilityRuntimeError("driver factory failed") from error
    valid = (
        getattr(driver, "driver_id", None) == config["driver_id"]
        and getattr(driver, "driver_version", None) == config["driver_version"]
        and getattr(driver, "supports_atomic_idempotency", None) is True
        and getattr(driver, "supports_reconcile", None) is True
        and getattr(driver, "supports_hard_deadline", None) is True
        and getattr(driver, "supports_isolated_context", None) is True
        and all(callable(getattr(driver, name, None))
                for name in ("execute_idempotent", "reconcile"))
    )
    if not valid:
        closer = getattr(driver, "close", None)
        if callable(closer):
            closer()
        raise SubagentFacilityRuntimeError("driver factory returned an invalid driver")
    return driver


class _IsolatedDriver:
    """One killable process per execute/reconcile; never implicitly retries."""

    def __init__(self, config: Mapping[str, Any], config_sha256: str,
                 lifetime_fd: int):
        self._config, self._pid = _copy(config), os.getpid()
        self._config_sha256 = config_sha256
        self._lifetime_fd = lifetime_fd
        self.driver_id = config["driver_id"]
        self.driver_version = config["driver_version"]
        self.supports_atomic_idempotency = True
        self.supports_reconcile = True
        self.supports_hard_deadline = True
        self.supports_isolated_context = True

    def _check(self):
        if os.getpid() != self._pid:
            raise SubagentFacilityRuntimeError("facility runtime cannot be used after fork")

    def _call(self, payload: Mapping[str, Any], timeout: int):
        self._check()
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise SubagentFacilityRuntimeError("driver deadline is invalid")
        command = [
            sys.executable, "-I", "-S", str(Path(__file__).resolve()),
            "--driver-worker",
            "--factory-file", self._config["factory_file"],
            "--factory-callable", self._config["factory_callable"],
            "--factory-sha256", self._config["factory_sha256"],
            "--driver-id", self.driver_id,
            "--driver-version", self.driver_version,
            "--config-file", self._config["config_file"],
            "--config-sha256", self._config_sha256,
            "--lifetime-fd", str(self._lifetime_fd),
            "--deadline-seconds", str(timeout),
        ]
        encoded = _canonical(payload)
        if len(encoded) > FACILITY.MAX_BODY_BYTES:
            raise SubagentFacilityRuntimeError("driver request is too large")
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True,
            start_new_session=True, pass_fds=(self._lifetime_fd,),
            env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        try:
            output, _unused = process.communicate(encoded, timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate()
            raise SubagentFacilityRuntimeError("driver hard deadline exceeded") from None
        if process.returncode != 0 or not output \
                or len(output) > FACILITY.MAX_RESPONSE_BYTES:
            raise SubagentFacilityRuntimeError("driver worker failed")
        try:
            result = json.loads(
                output.decode("utf-8"), object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise SubagentFacilityRuntimeError("driver worker response is invalid") from None
        if not isinstance(result, dict) or set(result) != {"ok", "result", "retryable"} \
                or type(result.get("ok")) is not bool \
                or type(result.get("retryable")) is not bool:
            raise SubagentFacilityRuntimeError("driver worker response is invalid")
        if not result["ok"]:
            if result["result"] is not None:
                raise SubagentFacilityRuntimeError("driver worker response is invalid")
            raise _DriverWorkerFailed(retryable=result["retryable"])
        if result["retryable"] or not isinstance(result["result"], dict):
            raise SubagentFacilityRuntimeError("driver worker response is invalid")
        return result["result"]

    def execute_idempotent(self, assignment, model_input, **controls):
        payload = {
            "operation": "execute", "assignment": _copy(assignment),
            "model_input": _copy(model_input), "controls": _copy(controls),
        }
        return self._call(payload, assignment["deadline_seconds"])

    def reconcile(self, assignment, **controls):
        payload = {
            "operation": "reconcile", "assignment": _copy(assignment),
            "controls": _copy(controls),
        }
        return self._call(payload, assignment["deadline_seconds"])

    def close(self):
        self._check()


def _build_driver(config: Mapping[str, Any], document: Mapping[str, Any],
                  config_raw: bytes, lifetime_fd: int):
    driver = _create_driver(config, document)
    closer = getattr(driver, "close", None)
    if callable(closer):
        closer()
    return _IsolatedDriver(
        config, hashlib.sha256(config_raw).hexdigest(), lifetime_fd,
    )


def _ledger_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev, details.st_ino, details.st_nlink,
        stat.S_IMODE(details.st_mode), details.st_uid, details.st_gid,
    )


class _RuntimeLock:
    """Exclusive deployment lock held before crash fencing until shutdown."""

    def __init__(self, ledger_path: Path):
        self.path = ledger_path.with_name(ledger_path.name + ".runtime.lock")
        self._handle = None
        try:
            with PROTECTED._open_directory(self.path) as directory:
                flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
                descriptor = (os.open(self.path, flags, 0o600)
                              if directory is None else os.open(
                                  self.path.name, flags, 0o600,
                                  dir_fd=directory,
                              ))
                handle = os.fdopen(descriptor, "r+b", buffering=0)
                linked = PROTECTED._lstat(self.path, directory)
                details = os.fstat(handle.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                    or _ledger_identity(details) != _ledger_identity(linked)
                    or (os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077)
                    or (hasattr(os, "getuid") and details.st_uid != os.getuid())):
                raise SubagentFacilityRuntimeError("facility runtime lock is unsafe")
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
            if 'handle' in locals():
                handle.close()
            raise SubagentFacilityRuntimeError(
                "facility runtime lock directory is unsafe"
            ) from error
        except (BlockingIOError, OSError) as error:
            if 'handle' in locals():
                handle.close()
            raise SubagentFacilityRuntimeError(
                "facility runtime is already active"
            ) from error
        except Exception:
            if 'handle' in locals():
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

    def fileno(self) -> int:
        if self._handle is None:
            raise SubagentFacilityRuntimeError("facility runtime lock is closed")
        return self._handle.fileno()


def _prepare_ledger(path: Path, *, initialize: bool):
    if not path.is_absolute():
        raise SubagentFacilityRuntimeError("facility ledger path must be absolute")
    try:
        with PROTECTED._open_directory(path) as directory:
            try:
                details = PROTECTED._lstat(path, directory)
            except FileNotFoundError:
                if not initialize:
                    raise SubagentFacilityRuntimeError("facility ledger is missing") from None
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
                raise SubagentFacilityRuntimeError("facility ledger is unsafe")
            return _ledger_identity(details)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise SubagentFacilityRuntimeError("facility ledger directory is unsafe") from error
    except OSError as error:
        raise SubagentFacilityRuntimeError("facility ledger is unavailable") from error


def _bind_deployment(path: Path, binding_sha256: str, *, initialize: bool):
    try:
        connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            if initialize:
                connection.execute("BEGIN IMMEDIATE")
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )}
                if tables and "subagent_facility_deployment" not in tables:
                    connection.rollback()
                    raise SubagentFacilityRuntimeError(
                        "facility ledger is not an initialized deployment"
                    )
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS subagent_facility_deployment (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        schema_name TEXT NOT NULL,
                        binding_sha256 TEXT NOT NULL
                    )
                """)
            row = connection.execute(
                "SELECT schema_name,binding_sha256 FROM "
                "subagent_facility_deployment WHERE singleton=1"
            ).fetchone()
            expected = (DEPLOYMENT_SCHEMA, binding_sha256)
            if row is None and initialize:
                connection.execute(
                    "INSERT INTO subagent_facility_deployment VALUES (1,?,?)",
                    expected,
                )
            elif row is None:
                raise SubagentFacilityRuntimeError("facility ledger has no deployment binding")
            elif tuple(row) != expected:
                if initialize:
                    connection.rollback()
                raise SubagentFacilityRuntimeError("facility deployment binding changed")
            if initialize:
                connection.commit()
        finally:
            connection.close()
    except SubagentFacilityRuntimeError:
        raise
    except sqlite3.Error as error:
        raise SubagentFacilityRuntimeError("facility deployment ledger is invalid") from error


def _deployment_binding(config: Mapping[str, Any], driver_raw: bytes, bearer: str):
    projection = {
        "schema": DEPLOYMENT_SCHEMA,
        "backend_id": config["backend_id"],
        "backend_version": config["backend_version"],
        "facility_id": config["facility_id"],
        "facility_version": config["facility_version"],
        "allow_loopback_http": config["allow_loopback_http"],
        "authentication_sha256": hashlib.sha256(bearer.encode("ascii")).hexdigest(),
        "max_concurrent_executions": config["ledger"]["max_concurrent_executions"],
        "driver": {
            key: config["driver"][key] for key in (
                "factory_file", "factory_callable", "factory_sha256",
                "driver_id", "driver_version",
            )
        } | {"config_sha256": hashlib.sha256(driver_raw).hexdigest()},
        "routes": config["routes"],
    }
    return hashlib.sha256(_canonical(projection)).hexdigest()


@dataclass
class SubagentFacilityRuntime:
    facility: Any
    driver: _IsolatedDriver
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
            admitted = valid and not self._closing and not self._closed
            if admitted:
                self._active += 1
        if not admitted:
            body = b'{"error":{"code":"subagent_facility.runtime_unavailable","retryable":true}}'
            start_response("503 Service Unavailable", [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
            ])
            return [body]
        try:
            return self.facility(environ, start_response)
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
            self.driver.close()
        finally:
            self.runtime_lock.close()
            with self._condition:
                self._closed = True
                self._condition.notify_all()


def open_subagent_facility_runtime(path: Path, *, initialize_ledger: bool = False):
    if os.name != "posix":
        raise SubagentFacilityRuntimeError(
            "facility runtime requires POSIX process isolation"
        )
    config, _config_raw = load_facility_runtime_config(path)
    driver_document, driver_raw = _driver_configuration(config["driver"])
    ledger_path = Path(config["ledger"]["path"])
    runtime_lock = _RuntimeLock(ledger_path)
    driver = None
    try:
        driver = _build_driver(
            config["driver"], driver_document, driver_raw,
            runtime_lock.fileno(),
        )
        bearer = _protected_text(
            config["authentication"]["token_file"], PROTECTED.BEARER,
            "facility authentication token",
        )
        initial_identity = _prepare_ledger(ledger_path, initialize=initialize_ledger)
        _bind_deployment(
            ledger_path, _deployment_binding(config, driver_raw, bearer),
            initialize=initialize_ledger,
        )
        if _prepare_ledger(ledger_path, initialize=False) != initial_identity:
            raise SubagentFacilityRuntimeError("facility ledger changed during startup")
        routes = [_route(item) for item in config["routes"]]
        boot_id = hashlib.sha256(os.urandom(32)).hexdigest()
        ledger = FACILITY.SQLiteFacilityLedger(
            ledger_path,
            max_concurrent_executions=config["ledger"]["max_concurrent_executions"],
            boot_id=boot_id, initialize_schema=initialize_ledger,
        )
        ledger.recover_foreign_dispatches()
        identity = _prepare_ledger(ledger_path, initialize=False)
        if identity != initial_identity:
            raise SubagentFacilityRuntimeError("facility ledger changed during startup")
        application = FACILITY.SubagentFacilityApplication(
            backend_id=config["backend_id"],
            backend_version=config["backend_version"],
            facility_id=config["facility_id"],
            facility_version=config["facility_version"],
            bearer_token=bearer,
            policy=FACILITY.EXECUTOR.PinnedExecutorPolicy(routes),
            ledger=ledger, driver=driver,
            allow_loopback_http=config["allow_loopback_http"],
        )
        return SubagentFacilityRuntime(
            application, driver, ledger_path, identity, runtime_lock,
        )
    except Exception:
        if driver is not None:
            driver.close()
        runtime_lock.close()
        raise


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = False


def _loopback(value: str) -> bool:
    import socket
    if value == "::1":
        return True
    try:
        return socket.gethostbyname(value).startswith("127.")
    except OSError:
        return False


def _driver_worker(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--factory-file", required=True)
    parser.add_argument("--factory-callable", required=True)
    parser.add_argument("--factory-sha256", required=True)
    parser.add_argument("--driver-id", required=True)
    parser.add_argument("--driver-version", required=True)
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--lifetime-fd", required=True, type=int)
    parser.add_argument("--deadline-seconds", required=True, type=int)
    args = parser.parse_args(arguments)
    config = {
        "factory_file": args.factory_file,
        "factory_callable": args.factory_callable,
        "factory_sha256": args.factory_sha256,
        "driver_id": args.driver_id,
        "driver_version": args.driver_version,
        "config_file": args.config_file,
    }
    driver = None
    try:
        if (os.name != "posix" or args.lifetime_fd < 0
                or not 1 <= args.deadline_seconds <= 300):
            raise SubagentFacilityRuntimeError("driver lifetime fence is invalid")
        os.fstat(args.lifetime_fd)

        def hard_stop(_signum, _frame):
            os.killpg(os.getpid(), signal.SIGKILL)

        signal.signal(signal.SIGALRM, hard_stop)
        signal.alarm(args.deadline_seconds)
        document, config_raw = _driver_configuration(config)
        if (SHA256.fullmatch(args.config_sha256) is None
                or not hmac.compare_digest(
                    hashlib.sha256(config_raw).hexdigest(), args.config_sha256,
                )):
            raise SubagentFacilityRuntimeError("driver configuration changed")
        driver = _create_driver(config, document)
        raw = sys.stdin.buffer.read(FACILITY.MAX_BODY_BYTES + 1)
        if not raw or len(raw) > FACILITY.MAX_BODY_BYTES:
            raise SubagentFacilityRuntimeError("driver worker request is invalid")
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
        if not isinstance(payload, dict):
            raise SubagentFacilityRuntimeError("driver worker request is invalid")
        operation = payload.get("operation")
        if operation == "execute" and set(payload) == {
                "operation", "assignment", "model_input", "controls"}:
            result = driver.execute_idempotent(
                payload["assignment"], payload["model_input"],
                **payload["controls"],
            )
        elif operation == "reconcile" and set(payload) == {
                "operation", "assignment", "controls"}:
            result = driver.reconcile(payload["assignment"], **payload["controls"])
        else:
            raise SubagentFacilityRuntimeError("driver worker request is invalid")
        output = _canonical({
            "ok": True, "result": result, "retryable": False,
        })
        if not output or len(output) > FACILITY.MAX_RESPONSE_BYTES:
            raise SubagentFacilityRuntimeError("driver worker response is invalid")
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception as error:
        retryable = getattr(error, "retryable", True)
        if type(retryable) is not bool:
            retryable = True
        try:
            sys.stdout.buffer.write(_canonical({
                "ok": False, "result": None, "retryable": retryable,
            }))
            sys.stdout.buffer.flush()
            return 0
        except Exception:
            return 1
    finally:
        closer = getattr(driver, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the protected provider-neutral host-subagent facility",
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--initialize-ledger", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=47643)
    args = parser.parse_args()
    if not _loopback(args.listen_host) or not 1 <= args.listen_port <= 65535:
        print("BLOCK: facility must listen on loopback behind trusted TLS", file=sys.stderr)
        return 1
    runtime = None
    try:
        runtime = open_subagent_facility_runtime(
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
    if len(sys.argv) > 1 and sys.argv[1] == "--driver-worker":
        raise SystemExit(_driver_worker(sys.argv[2:]))
    raise SystemExit(main())
