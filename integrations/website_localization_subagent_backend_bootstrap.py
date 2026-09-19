#!/usr/bin/env python3
"""Materialize one protected standard backend configuration locally.

This bootstrap deliberately does not discover trust from a network endpoint or
from pasted readiness JSON.  It opens the protected facility configuration and
initialized ledger through the real runtime, executes its content-free driver
preflight, verifies the executor route matrix, and creates the backend file
without replacing an existing deployment.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
FACILITY_RUNTIME_PATH = (
    ROOT / "integrations" / "website_localization_subagent_facility_runtime.py"
)
EXECUTOR_RUNTIME_PATH = (
    ROOT / "integrations" / "website_localization_subagent_executor_runtime.py"
)
BACKEND_PATH = ROOT / "integrations" / "website_localization_subagent_backend_http.py"
RECEIPT_SCHEMA = "translate-native.subagent-review-backend-bootstrap-receipt.v1"
MAX_CONFIG_BYTES = 512 * 1024


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


FACILITY_RUNTIME = _load("blun_subagent_bootstrap_facility_runtime", FACILITY_RUNTIME_PATH)
EXECUTOR_RUNTIME = _load("blun_subagent_bootstrap_executor_runtime", EXECUTOR_RUNTIME_PATH)
BACKEND = _load("blun_subagent_bootstrap_backend", BACKEND_PATH)
PROTECTED = FACILITY_RUNTIME.PROTECTED


class SubagentBackendBootstrapError(RuntimeError):
    """Content-free bootstrap failure."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _protected_json(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    try:
        value, raw = FACILITY_RUNTIME._protected_json(path, name=name)
    except FACILITY_RUNTIME.SubagentFacilityRuntimeError as error:
        raise SubagentBackendBootstrapError(f"{name} is unavailable or invalid") from error
    return value, raw


def _route_projection(routes: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = {
        "route_id", "phase", "reviewer_role", "reviewer_agent_id",
        "model_id", "model_version", "host_policy_version", "target_locale",
        "content_type", "task_policy_sha256",
    }
    if (not isinstance(routes, list) or not routes
            or any(not isinstance(route, Mapping) or set(route) != fields
                   for route in routes)):
        raise SubagentBackendBootstrapError("review route matrix is invalid")
    copied = json.loads(json.dumps(routes, ensure_ascii=False, allow_nan=False))
    if len({route["route_id"] for route in copied}) != len(copied):
        raise SubagentBackendBootstrapError("review route matrix is invalid")
    return sorted(copied, key=lambda route: route["route_id"])


def _standard_backend_factory(config: Mapping[str, Any]) -> None:
    backend = config["backend"]
    try:
        deployed = PROTECTED._read_protected(
            Path(backend["factory_file"]), EXECUTOR_RUNTIME.MAX_FACTORY_BYTES,
        )
        bundled = BACKEND_PATH.read_bytes()
    except (OSError, PROTECTED.ResponseReviewRuntimeError) as error:
        raise SubagentBackendBootstrapError(
            "standard backend factory is unavailable"
        ) from error
    deployed_sha256 = hashlib.sha256(deployed).hexdigest()
    if (backend["factory_callable"] != "build_backend"
            or not hmac.compare_digest(deployed_sha256, backend["factory_sha256"])
            or not hmac.compare_digest(deployed_sha256, hashlib.sha256(bundled).hexdigest())):
        raise SubagentBackendBootstrapError("standard backend factory binding is invalid")


def _template(
        document: Mapping[str, Any], executor: Mapping[str, Any],
        facility: Mapping[str, Any]) -> dict[str, Any]:
    if (not isinstance(document, Mapping)
            or set(document) != {"schema", "backend_id", "backend_version", "settings"}
            or document.get("schema") != EXECUTOR_RUNTIME.BACKEND_SCHEMA
            or document.get("backend_id") != executor["backend"]["backend_id"]
            or document.get("backend_version") != executor["backend"]["backend_version"]
            or not isinstance(document.get("settings"), Mapping)):
        raise SubagentBackendBootstrapError("backend template binding is invalid")
    settings = json.loads(json.dumps(document["settings"], ensure_ascii=False))
    expected = {
        "schema", "backend_id", "backend_version", "facility_id",
        "facility_version", "endpoint", "authentication", "readiness",
        "request_timeout_seconds", "max_input_bytes", "max_output_tokens",
        "cost_unit", "max_cost_units", "allow_loopback_http",
    }
    if set(settings) != expected or settings.get("readiness") is not None:
        raise SubagentBackendBootstrapError("backend template must be unbound")
    for name in ("backend_id", "backend_version", "facility_id", "facility_version"):
        if settings.get(name) != facility[name]:
            raise SubagentBackendBootstrapError("backend and facility identities differ")
    authentication = settings.get("authentication")
    if (not isinstance(authentication, dict)
            or set(authentication) != {"scheme", "token_file", "token_sha256"}
            or authentication.get("scheme") != "bearer"
            or authentication.get("token_file")
            != facility["authentication"]["token_file"]):
        raise SubagentBackendBootstrapError("backend authentication binding is invalid")
    return settings


def _ledger_must_be_absent(config: Mapping[str, Any]) -> None:
    path = Path(config["ledger"]["path"])
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise SubagentBackendBootstrapError("executor ledger cannot be inspected") from error
    raise SubagentBackendBootstrapError("executor ledger already exists")


def _path_identity(path: Path, *, missing: bool = False) -> Path:
    try:
        parent = path.parent.resolve(strict=True)
        if missing:
            return parent / path.name
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SubagentBackendBootstrapError("protected path identity is invalid") from error


def _distinct_output(
        output: Path, facility_path: Path, template_path: Path,
        executor_path: Path, facility: Mapping[str, Any],
        executor: Mapping[str, Any]) -> None:
    output_identity = _path_identity(output, missing=True)
    protected = [
        (facility_path, False), (template_path, False), (executor_path, False),
        (Path(facility["ledger"]["path"]), False),
        (Path(facility["authentication"]["token_file"]), False),
        (Path(facility["driver"]["factory_file"]), False),
        (Path(facility["driver"]["config_file"]), False),
        (Path(executor["ledger"]["path"]), True),
        (Path(executor["authentication"]["token_file"]), False),
        (Path(executor["backend"]["factory_file"]), False),
    ]
    if output_identity in {
            _path_identity(path, missing=missing) for path, missing in protected}:
        raise SubagentBackendBootstrapError("backend output aliases protected state")


def _rename_noreplace(directory: int, source: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SubagentBackendBootstrapError("atomic no-replace output is unsupported")
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


def _create_or_match(path: Path, raw: bytes) -> bool:
    if not path.is_absolute() or len(str(path)) > 4096:
        raise SubagentBackendBootstrapError("backend output path is invalid")
    descriptor = None
    temporary_name = "." + path.name + ".bootstrap-" + secrets.token_hex(16)
    temporary_identity = published_identity = None
    completed = False
    try:
        with PROTECTED._open_directory(path) as directory:
            if directory is None:
                raise SubagentBackendBootstrapError(
                    "atomic no-replace output requires a protected POSIX directory"
                )
            try:
                existing = PROTECTED._read_protected(path, MAX_CONFIG_BYTES)
            except PROTECTED.ResponseReviewRuntimeError:
                existing = None
            if existing is not None:
                if not hmac.compare_digest(existing, raw):
                    raise SubagentBackendBootstrapError(
                        "backend output already exists with different content"
                    )
                completed = True
                return False
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
            details = os.stat(
                temporary_name, dir_fd=directory, follow_symlinks=False,
            )
            if (not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode)
                    or details.st_nlink != 1
                    or (details.st_dev, details.st_ino) != temporary_identity
                    or (os.name != "nt" and stat.S_IMODE(details.st_mode) != 0o600)
                    or (hasattr(os, "getuid") and details.st_uid != os.getuid())):
                raise SubagentBackendBootstrapError("backend output changed during creation")
            try:
                _rename_noreplace(directory, temporary_name, path.name)
            except FileExistsError:
                existing = PROTECTED._read_protected(path, MAX_CONFIG_BYTES)
                if not hmac.compare_digest(existing, raw):
                    raise SubagentBackendBootstrapError(
                        "backend output already exists with different content"
                    )
                completed = True
                return False
            temporary_name = ""
            published_identity = temporary_identity
            os.fsync(directory)
            details = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if ((details.st_dev, details.st_ino) != published_identity
                    or details.st_nlink != 1
                    or PROTECTED._read_protected(path, MAX_CONFIG_BYTES) != raw):
                raise SubagentBackendBootstrapError("backend output verification failed")
            completed = True
            return True
    except SubagentBackendBootstrapError:
        raise
    except (OSError, PROTECTED.ResponseReviewRuntimeError) as error:
        raise SubagentBackendBootstrapError("backend output could not be created") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name:
            try:
                with PROTECTED._open_directory(path) as directory:
                    details = os.stat(
                        temporary_name, dir_fd=directory, follow_symlinks=False,
                    )
                    if (details.st_dev, details.st_ino) == temporary_identity:
                        os.unlink(temporary_name, dir_fd=directory)
                        os.fsync(directory)
            except (FileNotFoundError, OSError, PROTECTED.ResponseReviewRuntimeError):
                pass
        if published_identity is not None and not completed:
            try:
                with PROTECTED._open_directory(path) as directory:
                    details = os.stat(
                        path.name, dir_fd=directory, follow_symlinks=False,
                    )
                    if (details.st_dev, details.st_ino) == published_identity:
                        os.unlink(path.name, dir_fd=directory)
                        os.fsync(directory)
            except (FileNotFoundError, OSError, PROTECTED.ResponseReviewRuntimeError):
                pass


def bootstrap_backend(
        facility_config_path: Path, backend_template_path: Path,
        executor_config_path: Path, output_path: Path) -> dict[str, Any]:
    """Create one standard backend config from a trusted local facility."""
    facility, facility_raw = FACILITY_RUNTIME.load_facility_runtime_config(
        facility_config_path
    )
    try:
        executor, executor_raw = EXECUTOR_RUNTIME.load_executor_runtime_config(
            executor_config_path
        )
    except EXECUTOR_RUNTIME.SubagentExecutorRuntimeError as error:
        raise SubagentBackendBootstrapError(
            "executor configuration is unavailable or invalid"
        ) from error
    template, template_raw = _protected_json(backend_template_path, "backend template")
    if Path(executor["backend"]["config_file"]) != output_path:
        raise SubagentBackendBootstrapError("backend output is not executor-bound")
    _distinct_output(
        output_path, facility_config_path, backend_template_path,
        executor_config_path, facility, executor,
    )
    _ledger_must_be_absent(executor)
    _standard_backend_factory(executor)
    settings = _template(template, executor, facility)
    if _route_projection(executor["routes"]) != _route_projection(facility["routes"]):
        raise SubagentBackendBootstrapError("executor and facility routes differ")

    runtime = executor_lock = None
    try:
        try:
            executor_lock = EXECUTOR_RUNTIME._RuntimeLock(
                Path(executor["ledger"]["path"])
            )
        except EXECUTOR_RUNTIME.SubagentExecutorRuntimeError as error:
            raise SubagentBackendBootstrapError("executor deployment is active") from error
        _ledger_must_be_absent(executor)
        runtime = FACILITY_RUNTIME.open_subagent_facility_runtime(facility_config_path)
        preflight = runtime.preflight
        settings["readiness"] = {
            "facility_ledger_instance_id": preflight["facility_ledger_instance_id"],
            "driver_deployment_sha256": preflight["driver_deployment_sha256"],
            "deployment_manifest_sha256": preflight["deployment_manifest_sha256"],
            "route_requirements_sha256": preflight["route_requirements_sha256"],
            "readiness_policy_sha256": preflight["readiness_policy_sha256"],
            "routes_count": preflight["routes_checked"],
        }
        try:
            BACKEND.build_backend(settings)
        except (OSError, RuntimeError, ValueError) as error:
            raise SubagentBackendBootstrapError(
                "materialized backend configuration is invalid"
            ) from error
        output = {
            "schema": EXECUTOR_RUNTIME.BACKEND_SCHEMA,
            "backend_id": template["backend_id"],
            "backend_version": template["backend_version"],
            "settings": settings,
        }
        raw = _canonical(output)
        if len(raw) > MAX_CONFIG_BYTES:
            raise SubagentBackendBootstrapError("materialized backend configuration is too large")

        facility_after, facility_after_raw = (
            FACILITY_RUNTIME.load_facility_runtime_config(facility_config_path)
        )
        try:
            executor_after, executor_after_raw = (
                EXECUTOR_RUNTIME.load_executor_runtime_config(executor_config_path)
            )
        except EXECUTOR_RUNTIME.SubagentExecutorRuntimeError as error:
            raise SubagentBackendBootstrapError(
                "executor configuration changed during bootstrap"
            ) from error
        template_after, template_after_raw = _protected_json(
            backend_template_path, "backend template"
        )
        if (facility_after != facility or facility_after_raw != facility_raw
                or executor_after != executor or executor_after_raw != executor_raw
                or template_after != template or template_after_raw != template_raw):
            raise SubagentBackendBootstrapError("bootstrap input changed during validation")
        _ledger_must_be_absent(executor)
        created = _create_or_match(output_path, raw)
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "created" if created else "already_materialized",
            "content_free": True,
            "backend_id": output["backend_id"],
            "backend_version": output["backend_version"],
            "facility_id": settings["facility_id"],
            "facility_version": settings["facility_version"],
            "facility_ledger_instance_id": preflight["facility_ledger_instance_id"],
            "backend_config_sha256": hashlib.sha256(raw).hexdigest(),
        }
    except FACILITY_RUNTIME.SubagentFacilityRuntimeError as error:
        raise SubagentBackendBootstrapError("facility preflight blocked") from error
    finally:
        if runtime is not None:
            runtime.close()
        if executor_lock is not None:
            executor_lock.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize a protected local host-subagent backend configuration",
    )
    parser.add_argument("--facility-config", required=True, type=Path)
    parser.add_argument("--backend-template", required=True, type=Path)
    parser.add_argument("--executor-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = bootstrap_backend(
            args.facility_config, args.backend_template,
            args.executor_config, args.output,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"BLOCK: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
