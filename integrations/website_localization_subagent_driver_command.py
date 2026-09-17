#!/usr/bin/env python3
"""Pinned command adapter for a provider-neutral host-subagent facility.

The adapter is a factory for ``website_localization_subagent_facility_runtime``.
It gives an operator-owned executable one closed preflight, execute or reconcile
JSON request on stdin and accepts one closed JSON response on stdout. It never
invokes a shell, inherits no ambient environment, and can run only inside the
runtime's isolated worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - the facility runtime requires POSIX.
    fcntl = None

if sys.platform.startswith("linux") and fcntl is not None:
    F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
    F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
    F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
    F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
    F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
    F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)
else:  # pragma: no cover - sealed memfd execution is Linux-specific.
    F_ADD_SEALS = F_GET_SEALS = None
    F_SEAL_SEAL = F_SEAL_SHRINK = F_SEAL_GROW = F_SEAL_WRITE = None


SETTINGS_SCHEMA = "translate-native.subagent-review-command-driver.v2"
REQUEST_SCHEMA = "translate-native.subagent-review-command-request.v2"
RESPONSE_SCHEMA = "translate-native.subagent-review-command-response.v2"
PREFLIGHT_REQUEST_SCHEMA = (
    "translate-native.subagent-review-command-preflight-request.v1"
)
PREFLIGHT_RESPONSE_SCHEMA = (
    "translate-native.subagent-review-command-preflight-response.v1"
)
PREFLIGHT_REQUIREMENTS_SCHEMA = (
    "translate-native.subagent-review-facility-preflight.v1"
)
PREFLIGHT_RESULT_SCHEMA = (
    "translate-native.subagent-review-facility-preflight-result.v1"
)
PROTOCOL_VERSION = "2"
MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 4_500_000
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESULT_STATUSES = {"completed", "not_started", "running", "unknown", "cancel_pending"}
ISOLATION = {"inherit_context": False, "tools": [], "max_delegation_depth": 0}
PREFLIGHT_CAPABILITIES = {
    "atomic_idempotency": True,
    "read_only_reconcile": True,
    "hard_deadline": True,
    "isolated_context": True,
    "no_model_start": True,
}


class CommandSubagentDriverFailed(RuntimeError):
    """Content-free command-adapter failure understood by the facility."""

    def __init__(self, code: str, *, retryable: bool):
        if (not isinstance(code, str)
                or re.fullmatch(r"command_driver\.[a-z0-9_.-]{1,100}", code) is None
                or type(retryable) is not bool):
            raise ValueError("command-driver failure is invalid")
        self.code, self.retryable = code, retryable
        super().__init__(code)


def _failed(code: str, *, retryable: bool = False):
    return CommandSubagentDriverFailed(
        "command_driver." + code, retryable=retryable,
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _raw(value: Any, *, maximum: int = MAX_REQUEST_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _failed("payload_invalid") from None
    if not encoded or len(encoded) > maximum:
        raise _failed("payload_invalid")
    return encoded


def _copy(value: Any) -> Any:
    return json.loads(_raw(value))


def _sha(value: Any) -> str:
    return hashlib.sha256(_raw(value)).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise _failed(name + "_invalid")
    return value


def _open_directory(value: Any):
    if not isinstance(value, str) or len(value) > 4096:
        raise _failed("working_directory_invalid")
    path = Path(value)
    if not path.is_absolute():
        raise _failed("working_directory_invalid")
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        details = os.fstat(descriptor)
        linked = os.stat(path, follow_symlinks=False)
    except OSError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise _failed("working_directory_invalid") from None
    allowed_owner = os.getuid() if hasattr(os, "getuid") else details.st_uid
    if (not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) & 0o077
            or hasattr(details, "st_uid") and details.st_uid != allowed_owner
            or (details.st_dev, details.st_ino) != (linked.st_dev, linked.st_ino)):
        os.close(descriptor)
        raise _failed("working_directory_invalid")
    return descriptor


def _open_executable(path_value: Any, expected_sha256: Any):
    if (not isinstance(path_value, str) or len(path_value) > 4096
            or not Path(path_value).is_absolute()
            or not isinstance(expected_sha256, str)
            or SHA256.fullmatch(expected_sha256) is None):
        raise _failed("executable_invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source = snapshot = None
    try:
        required = (
            "memfd_create", "MFD_ALLOW_SEALING", "MFD_CLOEXEC",
        )
        if (not sys.platform.startswith("linux") or fcntl is None
                or any(not hasattr(os, name) for name in required)
                or any(value is None for value in (
                    F_ADD_SEALS, F_GET_SEALS, F_SEAL_SEAL, F_SEAL_SHRINK,
                    F_SEAL_GROW, F_SEAL_WRITE,
                ))):
            raise OSError("sealed executable snapshots are unavailable")
        source = os.open(path_value, flags)
        details = os.fstat(source)
        allowed_owner = os.getuid() if hasattr(os, "getuid") else details.st_uid
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or not stat.S_IMODE(details.st_mode) & stat.S_IXUSR
                or stat.S_IMODE(details.st_mode) & 0o077
                or not 1 <= details.st_size <= MAX_EXECUTABLE_BYTES
                or hasattr(details, "st_uid") and details.st_uid != allowed_owner):
            raise OSError("unsafe executable")
        snapshot = os.memfd_create(
            "translate-native-subagent-command",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EXECUTABLE_BYTES:
                raise OSError("executable grew while reading")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot, view)
                if written <= 0:
                    raise OSError("executable snapshot write failed")
                view = view[written:]
        if digest.hexdigest() != expected_sha256:
            raise OSError("executable digest mismatch")
        linked = os.stat(path_value, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino, linked.st_size) != (
                details.st_dev, details.st_ino, details.st_size):
            raise OSError("executable changed")
        os.fchmod(snapshot, 0o500)
        seals = (
            F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
        )
        fcntl.fcntl(snapshot, F_ADD_SEALS, seals)
        if fcntl.fcntl(snapshot, F_GET_SEALS) & seals != seals:
            raise OSError("executable snapshot is not sealed")
        os.lseek(snapshot, 0, os.SEEK_SET)
        os.close(source)
        source = None
        result = os.fdopen(snapshot, "rb", buffering=0)
        snapshot = None
        return result
    except OSError:
        if source is not None:
            os.close(source)
        if snapshot is not None:
            os.close(snapshot)
        raise _failed("executable_unavailable") from None


def _fd_path(descriptor: int) -> str:
    for root in ("/proc/self/fd", "/dev/fd"):
        candidate = f"{root}/{descriptor}"
        if os.path.exists(candidate):
            return candidate
    raise _failed("descriptor_execution_unsupported")


def _inside_isolated_worker() -> bool:
    if os.name != "posix" or os.environ.get("BLUN_SUBAGENT_DRIVER_WORKER") != "1":
        return False
    try:
        process = os.getpid()
        return process == os.getpgrp() == os.getsid(0)
    except OSError:
        return False


class CommandHostSubagentDriver:
    """Execute and reconcile one pinned operator host through closed JSON."""

    supports_atomic_idempotency = True
    supports_reconcile = True
    supports_hard_deadline = True
    supports_isolated_context = True
    supports_preflight = True

    def __init__(self, settings: Mapping[str, Any]):
        expected = {
            "schema", "driver_id", "driver_version", "command_id",
            "command_version", "executable",
            "executable_sha256", "arguments", "working_directory",
            "max_response_bytes", "deployment_manifest_sha256",
        }
        if not isinstance(settings, Mapping) or set(settings) != expected \
                or settings.get("schema") != SETTINGS_SCHEMA:
            raise _failed("settings_invalid")
        self.driver_id = _identifier(settings.get("driver_id"), "driver_id")
        self.driver_version = _identifier(
            settings.get("driver_version"), "driver_version",
        )
        self.command_id = _identifier(settings.get("command_id"), "command_id")
        self.command_version = _identifier(
            settings.get("command_version"), "command_version",
        )
        manifest = settings.get("deployment_manifest_sha256")
        if (not isinstance(manifest, str) or SHA256.fullmatch(manifest) is None
                or manifest == "0" * 64):
            raise _failed("deployment_manifest_invalid")
        self.deployment_manifest_sha256 = manifest
        arguments = settings.get("arguments")
        if (not isinstance(arguments, list) or len(arguments) > 64
                or any(not isinstance(item, str) or len(item) > 4096 or "\x00" in item
                       for item in arguments)):
            raise _failed("arguments_invalid")
        maximum = settings.get("max_response_bytes")
        if type(maximum) is not int or not 1024 <= maximum <= MAX_RESPONSE_BYTES:
            raise _failed("limits_invalid")
        self._executable = _open_executable(
            settings.get("executable"), settings.get("executable_sha256"),
        )
        try:
            self._working_directory = _open_directory(
                settings.get("working_directory"),
            )
        except BaseException:
            self._executable.close()
            raise
        self._arguments = tuple(arguments)
        self._max_response_bytes = maximum
        self._closed = False

    def _check(self):
        if self._closed or self._executable.closed:
            raise _failed("closed")
        if not _inside_isolated_worker():
            raise _failed("isolated_worker_required")

    @staticmethod
    def _kill_process(process: subprocess.Popen):
        try:
            process.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _abort_worker_group():
        if _inside_isolated_worker():
            os.killpg(os.getpgrp(), 9)
        raise _failed("worker_abort", retryable=True)

    def _invoke(self, unsigned: dict, *, timeout: int) -> dict:
        self._check()
        request = dict(unsigned)
        lifetime_read, lifetime_write = os.pipe()
        request["lifetime_fd"] = lifetime_read
        request["request_sha256"] = _sha({
            key: value for key, value in request.items() if key != "request_sha256"
        })
        encoded = _raw(request)
        descriptor = self._executable.fileno()
        directory_descriptor = self._working_directory
        command = [_fd_path(descriptor), *self._arguments]
        try:
            process = subprocess.Popen(
                command, executable=command[0], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=_fd_path(directory_descriptor),
                env={
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
                    "BLUN_SUBAGENT_COMMAND_PROTOCOL": PROTOCOL_VERSION,
                },
                close_fds=True,
                pass_fds=(descriptor, directory_descriptor, lifetime_read),
                start_new_session=False,
            )
        except OSError:
            os.close(lifetime_read)
            os.close(lifetime_write)
            raise _failed("start_failed", retryable=True) from None
        os.close(lifetime_read)
        output = bytearray()
        state = {"overflow": False, "writer_failed": False}

        def write_input():
            try:
                process.stdin.write(encoded)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                state["writer_failed"] = True

        def read_output():
            try:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    if len(output) + len(chunk) > self._max_response_bytes:
                        state["overflow"] = True
                        self._kill_process(process)
                        break
                    output.extend(chunk)
            except (OSError, ValueError):
                state["overflow"] = True
                self._kill_process(process)

        writer = threading.Thread(target=write_input, daemon=True)
        reader = threading.Thread(target=read_output, daemon=True)
        writer.start(), reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_process(process)
            process.wait()
            self._abort_worker_group()
        finally:
            os.close(lifetime_write)
        writer.join(1), reader.join(1)
        if writer.is_alive() or reader.is_alive() or state["overflow"]:
            self._kill_process(process)
            self._abort_worker_group()
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        if process.returncode != 0 or state["writer_failed"]:
            self._abort_worker_group()
        try:
            response = json.loads(
                bytes(output).decode("utf-8"), object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            self._abort_worker_group()
        preflight = request["operation"] == "preflight"
        expected = {
            "schema", "operation", "protocol_version", "driver_id",
            "driver_version", "command_id", "command_version",
            "deployment_manifest_sha256", "request_sha256", "ok",
            "retryable", "result",
        }
        if not preflight:
            expected |= {"provider_execution_key", "provider_request_sha256"}
        if (not isinstance(response, dict) or set(response) != expected
                or response.get("schema") != (
                    PREFLIGHT_RESPONSE_SCHEMA if preflight else RESPONSE_SCHEMA
                )
                or response.get("operation") != request["operation"]
                or response.get("protocol_version") != PROTOCOL_VERSION
                or response.get("driver_id") != self.driver_id
                or response.get("driver_version") != self.driver_version
                or response.get("command_id") != self.command_id
                or response.get("command_version") != self.command_version
                or response.get("deployment_manifest_sha256")
                != self.deployment_manifest_sha256
                or response.get("request_sha256") != request["request_sha256"]
                or not preflight and (
                    response.get("provider_execution_key")
                    != request["provider_execution_key"]
                    or response.get("provider_request_sha256")
                    != request["provider_request_sha256"]
                )):
            self._abort_worker_group()
        if type(response.get("ok")) is not bool \
                or type(response.get("retryable")) is not bool:
            self._abort_worker_group()
        if not response["ok"]:
            if response["result"] is not None:
                self._abort_worker_group()
            raise _failed(
                "host_rejected" if not response["retryable"] else "host_unavailable",
                retryable=response["retryable"],
            )
        if response["retryable"] or not isinstance(response.get("result"), dict):
            self._abort_worker_group()
        return response

    def preflight(
        self, requirements: Mapping[str, Any], *, deadline_seconds: int,
    ) -> dict:
        requirements = _copy(requirements)
        expected = {
            "schema", "challenge", "driver_deployment_sha256",
            "deployment_manifest_sha256", "route_requirements",
            "route_requirements_sha256", "required_capabilities",
        }
        routes = requirements.get("route_requirements")
        if (set(requirements) != expected
                or requirements.get("schema") != PREFLIGHT_REQUIREMENTS_SCHEMA
                or not isinstance(requirements.get("challenge"), str)
                or SHA256.fullmatch(requirements["challenge"]) is None
                or not isinstance(
                    requirements.get("driver_deployment_sha256"), str,
                )
                or SHA256.fullmatch(
                    requirements["driver_deployment_sha256"]
                ) is None
                or requirements.get("deployment_manifest_sha256")
                != self.deployment_manifest_sha256
                or not isinstance(routes, list) or not routes
                or requirements.get("route_requirements_sha256")
                != _sha(routes)
                or requirements.get("required_capabilities")
                != PREFLIGHT_CAPABILITIES
                or type(deadline_seconds) is not int
                or not 1 <= deadline_seconds <= 300):
            raise _failed("preflight_invalid")
        command_request = {
            "schema": PREFLIGHT_REQUEST_SCHEMA,
            "operation": "preflight",
            "protocol_version": PROTOCOL_VERSION,
            "driver_id": self.driver_id,
            "driver_version": self.driver_version,
            "command_id": self.command_id,
            "command_version": self.command_version,
            "deployment_manifest_sha256": self.deployment_manifest_sha256,
            "requirements": requirements,
            "isolation": ISOLATION,
            "deadline_seconds": deadline_seconds,
        }
        response = self._invoke(command_request, timeout=deadline_seconds)
        result = response["result"]
        expected_result = {
            "schema", "status", "challenge", "requirements_sha256",
            "driver_deployment_sha256", "deployment_manifest_sha256",
            "route_requirements_sha256", "capabilities",
        }
        if (set(result) != expected_result
                or result.get("schema") != PREFLIGHT_RESULT_SCHEMA
                or result.get("status") != "ready"
                or result.get("challenge") != requirements["challenge"]
                or result.get("requirements_sha256") != _sha(requirements)
                or result.get("driver_deployment_sha256")
                != requirements["driver_deployment_sha256"]
                or result.get("deployment_manifest_sha256")
                != self.deployment_manifest_sha256
                or result.get("route_requirements_sha256")
                != requirements["route_requirements_sha256"]
                or result.get("capabilities") != PREFLIGHT_CAPABILITIES):
            self._abort_worker_group()
        return _copy(result)

    def _operation(
        self, operation: str, assignment: Mapping[str, Any], *,
        provider_execution_key: str, provider_request_sha256: str,
        model_input: Mapping[str, Any] | None = None,
        budgets: Mapping[str, Any] | None = None,
        isolation: Mapping[str, Any] = ISOLATION,
    ) -> dict:
        if operation not in {"execute", "reconcile"}:
            raise _failed("operation_invalid")
        assignment = _copy(assignment)
        if (not isinstance(provider_execution_key, str)
                or SHA256.fullmatch(provider_execution_key) is None
                or not isinstance(provider_request_sha256, str)
                or SHA256.fullmatch(provider_request_sha256) is None
                or isolation != ISOLATION
                or type(assignment.get("deadline_seconds")) is not int
                or not 1 <= assignment["deadline_seconds"] <= 3600):
            raise _failed("operation_invalid")
        timeout = assignment["deadline_seconds"]
        execute = operation == "execute"
        if (execute != isinstance(model_input, Mapping)
                or execute != isinstance(budgets, Mapping)):
            raise _failed("operation_invalid")
        command_request = {
            "schema": REQUEST_SCHEMA,
            "operation": operation,
            "protocol_version": PROTOCOL_VERSION,
            "driver_id": self.driver_id,
            "driver_version": self.driver_version,
            "command_id": self.command_id,
            "command_version": self.command_version,
            "deployment_manifest_sha256": self.deployment_manifest_sha256,
            "provider_execution_key": provider_execution_key,
            "provider_request_sha256": provider_request_sha256,
            "assignment": assignment,
            "isolation": ISOLATION,
        }
        if execute:
            command_request["model_input"] = _copy(model_input)
            command_request["budgets"] = _copy(budgets)
        response = self._invoke(command_request, timeout=timeout)
        if (response.get("provider_execution_key") != provider_execution_key
                or response.get("provider_request_sha256") != provider_request_sha256
                or not isinstance(response.get("result"), dict)):
            self._abort_worker_group()
        result = response["result"]
        if (set(result) != {
                "status", "provider_execution_key", "provider_request_sha256",
                "actual_execution", "usage",
            } or result.get("status") not in RESULT_STATUSES
                or result.get("provider_execution_key") != provider_execution_key
                or result.get("provider_request_sha256") != provider_request_sha256
                or execute and result.get("status") == "not_started"):
            self._abort_worker_group()
        completed = result["status"] == "completed"
        if (completed != isinstance(result.get("actual_execution"), dict)
                or completed != isinstance(result.get("usage"), dict)):
            self._abort_worker_group()
        return _copy(result)

    def execute_idempotent(
        self, assignment: Mapping[str, Any], model_input: Mapping[str, Any], *,
        provider_execution_key: str, provider_request_sha256: str,
        budgets: Mapping[str, Any], isolation: Mapping[str, Any],
    ) -> dict:
        return self._operation(
            "execute", assignment,
            provider_execution_key=provider_execution_key,
            provider_request_sha256=provider_request_sha256,
            model_input=model_input, budgets=budgets, isolation=isolation,
        )

    def reconcile(
        self, assignment: Mapping[str, Any], *, provider_execution_key: str,
        provider_request_sha256: str,
    ) -> dict:
        return self._operation(
            "reconcile", assignment,
            provider_execution_key=provider_execution_key,
            provider_request_sha256=provider_request_sha256,
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._executable.close()
        finally:
            os.close(self._working_directory)


def build_driver(settings: Mapping[str, Any]):
    """Return the standard provider-neutral command driver."""
    return CommandHostSubagentDriver(settings)
