#!/usr/bin/env python3
"""Protected deployment composition for the isolated review host.

The runtime wires the existing authenticated WSGI endpoint to an explicitly
configured deployment launcher.  It contains no model-provider implementation
and never passes HTTP credentials, attestation material or the host journal to
the launcher factory.
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
import socket
import sqlite3
import stat
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from wsgiref.simple_server import WSGIServer, make_server
from socketserver import ThreadingMixIn


ROOT = Path(__file__).resolve().parents[1]
HOST_PATH = ROOT / "integrations" / "website_localization_subagent_host.py"
PROTECTED_PATH = ROOT / "integrations" / "response_subagent_https_runtime.py"
CONFIG_SCHEMA = "translate-native.subagent-review-host-runtime.v1"
LAUNCHER_SCHEMA = "translate-native.subagent-review-launcher-config.v1"
DEPLOYMENT_SCHEMA = "translate-native.subagent-review-host-deployment.v1"
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


HOST = _load("blun_website_localization_subagent_host", HOST_PATH)
PROTECTED = _load("blun_response_subagent_https_runtime", PROTECTED_PATH)


class ReviewHostRuntimeError(RuntimeError):
    """A content-free startup failure; no host application is exposed."""


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
        raise ReviewHostRuntimeError(f"{name} is invalid")
    return value


def _protected_json(path_value: Any, *, name: str, maximum: int) -> tuple[dict, bytes]:
    if not isinstance(path_value, (str, Path)) or len(str(path_value)) > 4096:
        raise ReviewHostRuntimeError(f"{name} path is invalid")
    try:
        raw = PROTECTED._read_protected(Path(path_value), maximum)
        text = raw.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("BOM is not allowed")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise ReviewHostRuntimeError(f"{name} is unavailable") from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ReviewHostRuntimeError(f"{name} is invalid JSON") from error
    if not isinstance(value, dict):
        raise ReviewHostRuntimeError(f"{name} must be an object")
    return value, raw


def _protected_text(path_value: Any, pattern: re.Pattern[str], name: str) -> str:
    try:
        return PROTECTED._protected_text(path_value, pattern, name)
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise ReviewHostRuntimeError(f"{name} is unavailable or invalid") from error


def _route(value: Any):
    fields = {
        "route_id", "schema", "phase", "target_locale", "content_type",
        "task_policy_sha256", "model_id", "model_version",
        "host_policy_version", "reviewer_agent_id", "reviewer_role",
        "max_timeout_seconds", "max_output_tokens", "max_input_bytes",
        "cost_unit", "max_cost_units",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ReviewHostRuntimeError("review route fields are invalid")
    try:
        return HOST.PinnedReviewRoute(**value)
    except (TypeError, ValueError) as error:
        raise ReviewHostRuntimeError("review route is invalid") from error


def load_host_runtime_config(path: Path) -> tuple[dict, bytes]:
    value, raw = _protected_json(path, name="review host configuration",
                                 maximum=MAX_CONFIG_BYTES)
    expected = {
        "schema", "host_id", "allow_loopback_http", "authentication",
        "attestation", "ledger", "launcher", "routes",
    }
    if set(value) != expected or value.get("schema") != CONFIG_SCHEMA:
        raise ReviewHostRuntimeError("review host configuration fields are invalid")
    _identifier(value.get("host_id"), "host_id")
    if type(value.get("allow_loopback_http")) is not bool:
        raise ReviewHostRuntimeError("allow_loopback_http must be boolean")
    authentication = value.get("authentication")
    if (not isinstance(authentication, dict)
            or set(authentication) != {"scheme", "token_file"}
            or authentication.get("scheme") != "bearer"):
        raise ReviewHostRuntimeError("authentication configuration is invalid")
    attestation = value.get("attestation")
    if (not isinstance(attestation, dict)
            or set(attestation) != {"algorithm", "key_id", "secret_file"}
            or attestation.get("algorithm") != "hmac-sha256"):
        raise ReviewHostRuntimeError("attestation configuration is invalid")
    _identifier(attestation.get("key_id"), "attestation key_id")
    ledger = value.get("ledger")
    if (not isinstance(ledger, dict)
            or set(ledger) != {"path", "lease_seconds"}
            or not isinstance(ledger.get("path"), str)
            or len(ledger["path"]) > 4096
            or not Path(ledger["path"]).is_absolute()
            or type(ledger.get("lease_seconds")) is not int
            or not 5 <= ledger["lease_seconds"] <= 300):
        raise ReviewHostRuntimeError("ledger configuration is invalid")
    launcher = value.get("launcher")
    launcher_fields = {
        "factory_file", "factory_callable", "factory_sha256", "launcher_id",
        "launcher_version", "config_file",
    }
    if (not isinstance(launcher, dict) or set(launcher) != launcher_fields
            or not isinstance(launcher.get("factory_file"), str)
            or len(launcher["factory_file"]) > 4096
            or not Path(launcher["factory_file"]).is_absolute()
            or not isinstance(launcher.get("factory_callable"), str)
            or CALLABLE_NAME.fullmatch(launcher["factory_callable"]) is None
            or not isinstance(launcher.get("factory_sha256"), str)
            or SHA256.fullmatch(launcher["factory_sha256"]) is None
            or not isinstance(launcher.get("config_file"), str)
            or len(launcher["config_file"]) > 4096
            or not Path(launcher["config_file"]).is_absolute()):
        raise ReviewHostRuntimeError("launcher configuration is invalid")
    _identifier(launcher.get("launcher_id"), "launcher_id")
    _identifier(launcher.get("launcher_version"), "launcher_version")
    routes_value = value.get("routes")
    if not isinstance(routes_value, list) or not routes_value:
        raise ReviewHostRuntimeError("at least one review route is required")
    routes = [_route(item) for item in routes_value]
    route_ids = [item.route_id for item in routes]
    if len(route_ids) != len(set(route_ids)):
        raise ReviewHostRuntimeError("review route IDs must be unique")
    native_agents = {
        item.reviewer_agent_id for item in routes if item.phase == HOST.NATIVE_PHASE
    }
    fidelity_agents = {
        item.reviewer_agent_id for item in routes if item.phase == HOST.FIDELITY_PHASE
    }
    if native_agents & fidelity_agents:
        raise ReviewHostRuntimeError("native and fidelity reviewers must be distinct")
    try:
        HOST.PinnedReviewPolicy(routes)
    except (TypeError, ValueError) as error:
        raise ReviewHostRuntimeError("review route policy is invalid") from error
    return value, raw


def _launcher_configuration(value: Mapping[str, Any]) -> tuple[dict, bytes]:
    launcher, raw = _protected_json(
        value["config_file"], name="launcher configuration",
        maximum=MAX_CONFIG_BYTES,
    )
    if (set(launcher) != {
            "schema", "launcher_id", "launcher_version", "settings",
        } or launcher.get("schema") != LAUNCHER_SCHEMA
            or launcher.get("launcher_id") != value["launcher_id"]
            or launcher.get("launcher_version") != value["launcher_version"]
            or not isinstance(launcher.get("settings"), dict)):
        raise ReviewHostRuntimeError("launcher configuration fields are invalid")
    return launcher, raw


def _factory_file(config: Mapping[str, Any]) -> tuple[Any, Path]:
    path = Path(config["factory_file"])
    try:
        raw = PROTECTED._read_protected(path, MAX_FACTORY_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(digest, config["factory_sha256"]):
            raise ReviewHostRuntimeError("launcher factory digest does not match")
        code = compile(raw, str(path), "exec", dont_inherit=True)
        module_name = "_blun_review_launcher_" + hashlib.sha256(
            str(path).encode("utf-8") + b"\0" + raw
        ).hexdigest()
        with _FACTORY_EXECUTION_LOCK:
            module = sys.modules.get(module_name)
            if module is None:
                module = types.ModuleType(module_name)
                module.__file__ = str(path)
                module.__package__ = ""
                sys.modules[module_name] = module
                try:
                    exec(code, module.__dict__)
                except Exception:
                    if sys.modules.get(module_name) is module:
                        sys.modules.pop(module_name, None)
                    raise
        callable_factory = getattr(module, config["factory_callable"], None)
    except ReviewHostRuntimeError:
        raise
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise ReviewHostRuntimeError("launcher factory source is unavailable") from error
    except Exception as error:
        raise ReviewHostRuntimeError("launcher factory is unavailable") from error
    if not callable(callable_factory):
        raise ReviewHostRuntimeError("launcher factory is invalid")
    return callable_factory, path


class _ProcessBoundLauncher:
    def __init__(self, launcher: Any, launcher_id: str, launcher_version: str):
        self._launcher = launcher
        self._pid = os.getpid()
        self.launcher_id = launcher_id
        self.launcher_version = launcher_version

    def _check(self):
        if os.getpid() != self._pid:
            raise ReviewHostRuntimeError("review host runtime cannot be used after fork")

    def execute_idempotent(self, assignment, model_input, **budgets):
        self._check()
        return self._launcher.execute_idempotent(assignment, model_input, **budgets)

    def reconcile(self, assignment, model_input, **budgets):
        self._check()
        return self._launcher.reconcile(assignment, model_input, **budgets)

    def close(self):
        closer = getattr(self._launcher, "close", None)
        if callable(closer):
            closer()


def _build_launcher(config: Mapping[str, Any], launcher_document: Mapping[str, Any]):
    factory, source = _factory_file(config)
    try:
        launcher = factory(_copy(launcher_document["settings"]))
    except Exception as error:
        raise ReviewHostRuntimeError("launcher factory failed") from error
    if (getattr(launcher, "launcher_id", None) != config["launcher_id"]
            or getattr(launcher, "launcher_version", None) != config["launcher_version"]
            or any(not callable(getattr(launcher, name, None))
                   for name in ("execute_idempotent", "reconcile"))):
        closer = getattr(launcher, "close", None)
        if callable(closer):
            closer()
        raise ReviewHostRuntimeError("launcher factory returned an invalid launcher")
    return _ProcessBoundLauncher(
        launcher, config["launcher_id"], config["launcher_version"],
    ), source


def _ledger_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev, details.st_ino, details.st_nlink,
        stat.S_IMODE(details.st_mode), details.st_uid, details.st_gid,
    )


def _prepare_ledger(path: Path, *, initialize: bool):
    if not path.is_absolute():
        raise ReviewHostRuntimeError("ledger path must be absolute")
    try:
        with PROTECTED._open_directory(path) as directory:
            try:
                details = PROTECTED._lstat(path, directory)
            except FileNotFoundError:
                if not initialize:
                    raise ReviewHostRuntimeError("review host ledger is missing") from None
                flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
                descriptor = (os.open(path, flags, 0o600) if directory is None
                              else os.open(path.name, flags, 0o600, dir_fd=directory))
                os.close(descriptor)
                details = PROTECTED._lstat(path, directory)
                created = True
            else:
                created = False
            if (not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode)
                    or details.st_nlink != 1
                    or (os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077)
                    or (hasattr(os, "getuid") and details.st_uid != os.getuid())):
                raise ReviewHostRuntimeError("review host ledger is unsafe")
            return _ledger_identity(details), created
    except PROTECTED.ResponseReviewRuntimeError as error:
        raise ReviewHostRuntimeError("review host ledger directory is unsafe") from error
    except OSError as error:
        raise ReviewHostRuntimeError("review host ledger is unavailable") from error


def _bind_deployment(path: Path, binding_sha256: str, *, initialize: bool) -> None:
    try:
        target = str(path) if initialize else path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            target, timeout=10, isolation_level=None, uri=not initialize,
        )
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            if initialize:
                connection.execute("BEGIN IMMEDIATE")
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if tables and "review_host_deployment" not in tables:
                    connection.rollback()
                    raise ReviewHostRuntimeError(
                        "review host ledger is not an initialized deployment"
                    )
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS review_host_deployment (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        schema_name TEXT NOT NULL,
                        binding_sha256 TEXT NOT NULL
                    )
                """)
            row = connection.execute(
                "SELECT schema_name, binding_sha256 FROM review_host_deployment "
                "WHERE singleton = 1"
            ).fetchone()
            expected = (DEPLOYMENT_SCHEMA, binding_sha256)
            if row is None and initialize:
                connection.execute(
                    "INSERT INTO review_host_deployment VALUES (1, ?, ?)", expected,
                )
            elif row is None:
                raise ReviewHostRuntimeError(
                    "review host ledger has no deployment binding"
                )
            elif tuple(row) != expected:
                if initialize:
                    connection.rollback()
                raise ReviewHostRuntimeError("review host deployment binding changed")
            if initialize:
                connection.commit()
        finally:
            connection.close()
    except ReviewHostRuntimeError:
        raise
    except sqlite3.Error as error:
        raise ReviewHostRuntimeError("review host deployment ledger is invalid") from error


def _deployment_binding(config: Mapping[str, Any], launcher_raw: bytes,
                        bearer: str, secret: str) -> str:
    projection = {
        "schema": DEPLOYMENT_SCHEMA,
        "host_id": config["host_id"],
        "allow_loopback_http": config["allow_loopback_http"],
        "authentication_sha256": hashlib.sha256(bearer.encode("ascii")).hexdigest(),
        "attestation": {
            "algorithm": config["attestation"]["algorithm"],
            "key_id": config["attestation"]["key_id"],
            "secret_sha256": hashlib.sha256(secret.encode("ascii")).hexdigest(),
        },
        "lease_seconds": config["ledger"]["lease_seconds"],
        "launcher": {
            key: config["launcher"][key] for key in (
                "factory_file", "factory_callable", "factory_sha256",
                "launcher_id", "launcher_version",
            )
        } | {"config_sha256": hashlib.sha256(launcher_raw).hexdigest()},
        "routes": config["routes"],
    }
    return hashlib.sha256(_canonical(projection)).hexdigest()


@dataclass
class ReviewHostRuntime:
    """Process-bound WSGI runtime; ``application`` is safe for direct serving."""

    host: Any
    launcher: _ProcessBoundLauncher
    ledger_path: Path
    ledger_identity: tuple[int, int, int, int, int, int]

    def __post_init__(self):
        self._pid = os.getpid()
        self._closed = False
        self._closing = False
        self._active = 0
        self._condition = threading.Condition()
        self.application = self

    def __call__(self, environ, start_response):
        ledger_valid = False
        if not self._closed and os.getpid() == self._pid:
            try:
                details = self.ledger_path.lstat()
                ledger_valid = (
                    stat.S_ISREG(details.st_mode)
                    and not stat.S_ISLNK(details.st_mode)
                    and _ledger_identity(details) == self.ledger_identity
                )
            except OSError:
                ledger_valid = False
        with self._condition:
            if not ledger_valid or self._closing or self._closed:
                admitted = False
            else:
                self._active += 1
                admitted = True
        if not admitted:
            body = b'{"error":{"code":"review_host.runtime_unavailable","retryable":true}}'
            start_response("503 Service Unavailable", [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
            ])
            return [body]
        try:
            return self.host(environ, start_response)
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
            self.launcher.close()
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()


def open_review_host_runtime(path: Path, *, initialize_ledger: bool = False):
    """Build the exact protected deployment or fail before exposing an app."""
    config, _config_raw = load_host_runtime_config(path)
    launcher_document, launcher_raw = _launcher_configuration(config["launcher"])
    launcher, _factory_source = _build_launcher(config["launcher"], launcher_document)
    try:
        bearer = _protected_text(
            config["authentication"]["token_file"], PROTECTED.BEARER,
            "authentication token",
        )
        secret = _protected_text(
            config["attestation"]["secret_file"], PROTECTED.HMAC_SECRET,
            "attestation secret",
        )
        ledger_path = Path(config["ledger"]["path"])
        initial_identity, _created = _prepare_ledger(
            ledger_path, initialize=initialize_ledger,
        )
        binding = _deployment_binding(config, launcher_raw, bearer, secret)
        _bind_deployment(
            ledger_path, binding,
            initialize=initialize_ledger,
        )
        bound_identity, _created = _prepare_ledger(
            ledger_path, initialize=False,
        )
        if bound_identity != initial_identity:
            raise ReviewHostRuntimeError(
                "review host ledger changed during startup"
            )
        routes = [_route(item) for item in config["routes"]]
        ledger = HOST.SQLiteReviewLedger(
            ledger_path, lease_seconds=config["ledger"]["lease_seconds"],
            initialize_schema=initialize_ledger,
        )
        ledger_identity, _created = _prepare_ledger(
            ledger_path, initialize=False,
        )
        if ledger_identity != initial_identity:
            raise ReviewHostRuntimeError(
                "review host ledger changed during startup"
            )
        host = HOST.ReviewHostApplication(
            host_id=config["host_id"], bearer_token=bearer,
            signer=HOST.HMACAttestationSigner(
                secret, config["attestation"]["key_id"],
            ),
            policy=HOST.PinnedReviewPolicy(routes), ledger=ledger,
            launcher=launcher,
            allow_loopback_http=config["allow_loopback_http"],
        )
        return ReviewHostRuntime(host, launcher, ledger_path, ledger_identity)
    except Exception:
        launcher.close()
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
        description="Run the protected provider-neutral subagent review host",
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--initialize-ledger", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=47641)
    args = parser.parse_args()
    if not _loopback(args.listen_host) or not 1 <= args.listen_port <= 65535:
        print("BLOCK: review host must listen on loopback behind trusted TLS", file=sys.stderr)
        return 1
    runtime = None
    try:
        runtime = open_review_host_runtime(
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
