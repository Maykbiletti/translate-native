#!/usr/bin/env python3
"""Non-destructive installer, updater, and live doctor for BLUN Language Guard."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_URL = "https://github.com/Maykbiletti/translate-native.git"
TARGETS = {
    "codex": Path.home() / ".agents" / "skills" / "translate-native",
    "claude": Path.home() / ".claude" / "skills" / "translate-native",
    "blun": Path.home() / ".blun" / "skills" / "translate-native",
}
UPDATE_CONFIG = Path.home() / ".config" / "blun-language-guard" / "updater.json"
UPDATE_PAUSED_CONFIG = Path.home() / ".config" / "blun-language-guard" / "updater.rollback-paused.json"
UPDATE_STATE = Path.home() / ".config" / "blun-language-guard" / "update-state.json"
HEALTH_STATE = Path.home() / ".config" / "blun-language-guard" / "health-state.json"
HEALTH_CONFIG = Path.home() / ".config" / "blun-language-guard" / "health-monitor.json"
OPERATION_LOCK = Path.home() / ".config" / "blun-language-guard" / "operation.lock"
DELIVERY_COMMAND = Path.home() / ".local" / "bin" / "blun-language-deliver"
DELIVERY_POLICY = Path.home() / ".config" / "blun-language-guard" / "delivery-policy.json"
SIGNING_KEY = Path.home() / ".config" / "blun-language-guard" / "signing.key"
SERVICE_COMMAND = Path.home() / ".local" / "bin" / "blun-language-guard-service"
SERVICE_TOKEN = Path.home() / ".config" / "blun-language-guard" / "service.token"
AUDIT_LOG = Path.home() / ".config" / "blun-language-guard" / "audit.jsonl"
MCP_HTTP_COMMAND = Path.home() / ".local" / "bin" / "blun-language-guard-mcp"
MCP_HEADERS_COMMAND = Path.home() / ".local" / "bin" / "blun-language-guard-mcp-headers"
MCP_HTTP_TOKEN = Path.home() / ".config" / "blun-language-guard" / "mcp-http.token"
MCP_HTTP_URL = "http://127.0.0.1:47632/mcp"
CLAUDE_CONFIG = Path.home() / ".claude.json"
MCP_SERVER_NAME = "blun-language-guard"
CLAUDE_PLUGIN_NAME = "translate-native@blun-language-tools"
CLAUDE_MARKETPLACE_NAME = "blun-language-tools"
OPERATION_LOCK_STALE_SECONDS = 30 * 60
MAX_OPERATION_LOCK_BYTES = 4 * 1024
HEALTH_REPAIR_BACKOFF_SECONDS = (60, 120, 300, 900, 3600)
MAX_UPDATE_POLICY_BYTES = 64 * 1024
MAX_HEALTH_FILE_BYTES = 64 * 1024
MAX_UPDATE_STATE_BYTES = 64 * 1024
MAX_MCP_HTTP_TOKEN_BYTES = 64 * 1024
MAX_DELIVERY_POLICY_BYTES = 64 * 1024
MAX_BLUN_MCP_CONFIG_BYTES = 1024 * 1024
MAX_CLAUDE_CONFIG_BYTES = 16 * 1024 * 1024
MAX_PROJECT_MCP_CONFIG_BYTES = 1024 * 1024
MAX_SERVICE_DEFINITION_BYTES = 256 * 1024
SERVICE_ENDPOINT = (
    "tcp:127.0.0.1:47631"
    if os.name == "nt"
    else f"unix:{Path.home() / '.config' / 'blun-language-guard' / 'guard.sock'}"
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _installed_symlink_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _inspect_installed_symlink(destination: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        details = destination.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError(f"Cannot inspect installation target: {destination}") from error
    if not stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Refusing to overwrite existing non-symlink: {destination}")
    return _installed_symlink_identity(details)


def _assert_installed_symlink_unchanged(
    destination: Path, expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    current = _inspect_installed_symlink(destination)
    if expected is None and current is not None:
        raise RuntimeError(f"Installation target appeared before replacement: {destination}")
    if expected is not None and current != expected:
        raise RuntimeError(f"Installation target changed before replacement: {destination}")


def atomic_symlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = _inspect_installed_symlink(destination)
    temporary = None
    for _attempt in range(16):
        candidate = destination.with_name(
            f".{destination.name}.{secrets.token_hex(16)}.new"
        )
        try:
            candidate.symlink_to(source, target_is_directory=source.is_dir())
        except FileExistsError:
            continue
        temporary = candidate
        break
    if temporary is None:
        raise RuntimeError(f"Cannot reserve temporary installation link: {destination}")
    try:
        _assert_installed_symlink_unchanged(destination, expected)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _service_definition_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _validate_service_definition_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Service definition is not a regular file: {path}")
    if details.st_nlink != 1:
        raise RuntimeError(f"Service definition has additional hard links: {path}")
    if details.st_size > MAX_SERVICE_DEFINITION_BYTES:
        raise RuntimeError(f"Service definition exceeds the size limit: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(f"Service definition is writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"Service definition owner is invalid: {path}")


def _inspect_service_definition(
    path: Path,
) -> tuple[int, int, int, int, int, int] | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError(f"Cannot inspect service definition: {path}") from error
    _validate_service_definition_details(path, details)
    return _service_definition_identity(details)


def _assert_service_definition_unchanged(
    path: Path,
    expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    current = _inspect_service_definition(path)
    if expected is None and current is not None:
        raise RuntimeError(f"Service definition appeared before replacement: {path}")
    if expected is not None and current != expected:
        raise RuntimeError(f"Service definition changed before replacement: {path}")


def _service_directory_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _validate_service_directory_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Service-definition directory is not a directory: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(f"Service-definition directory is writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"Service-definition directory owner is invalid: {path}")


def _assert_service_directory_unchanged(
    path: Path, expected: tuple[int, int, int, int, int],
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise RuntimeError(f"Service-definition directory cannot be rechecked: {path}") from error
    _validate_service_directory_details(path, current)
    if _service_directory_identity(current) != expected:
        raise RuntimeError(f"Service-definition directory changed during operation: {path}")


@contextlib.contextmanager
def _open_service_definition_directory(
    path: Path,
    home: Path,
    *,
    create: bool = True,
    missing_ok: bool = False,
):
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise RuntimeError(f"Service-definition directory is outside the user home: {path}") from error
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    current_path = home
    try:
        descriptor = os.open(home, flags)
        _validate_service_directory_details(home, os.fstat(descriptor))
        for component in relative.parts:
            current_path = current_path / component
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    if not missing_ok:
                        raise
                    os.close(descriptor)
                    descriptor = None
                    break
                else:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=descriptor)
            if descriptor is None:
                break
            try:
                _validate_service_directory_details(current_path, os.fstat(child))
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        if descriptor is None:
            yield None
            return
        expected = _service_directory_identity(os.fstat(descriptor))
        yield descriptor
        _assert_service_directory_unchanged(path, expected)
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"Cannot safely open service-definition directory: {current_path}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _inspect_service_definition_at(
    directory: int, path: Path,
) -> tuple[int, int, int, int, int, int] | None:
    try:
        details = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError(f"Cannot inspect service definition: {path}") from error
    _validate_service_definition_details(path, details)
    return _service_definition_identity(details)


def _assert_service_definition_at_unchanged(
    directory: int,
    path: Path,
    expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    current = _inspect_service_definition_at(directory, path)
    if expected is None and current is not None:
        raise RuntimeError(f"Service definition appeared before replacement: {path}")
    if expected is not None and current != expected:
        raise RuntimeError(f"Service definition changed before replacement: {path}")


def _preflight_service_definition_removal_at(
    directory: int,
    path: Path,
    required_markers: tuple[str, ...],
) -> tuple[int, int, int, int, int, int] | None:
    expected = _inspect_service_definition_at(directory, path)
    if expected is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _service_definition_identity(opened) != expected:
                raise RuntimeError(f"Service definition changed while opening: {path}")
            raw = handle.read(MAX_SERVICE_DEFINITION_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        after_path = _inspect_service_definition_at(directory, path)
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"Cannot read service definition: {path}") from error
    if len(raw) > MAX_SERVICE_DEFINITION_BYTES:
        raise RuntimeError(f"Service definition exceeds the size limit: {path}")
    if _service_definition_identity(after_read) != expected or after_path != expected:
        raise RuntimeError(f"Service definition changed while reading: {path}")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise RuntimeError(f"Service definition is not valid UTF-8: {path}") from error
    if not all(marker in content for marker in required_markers):
        raise RuntimeError(f"Service definition is not managed by BLUN: {path}")
    return expected


@contextlib.contextmanager
def _prepare_service_definition_removals(
    home: Path,
    definitions: tuple[tuple[Path, tuple[str, ...]], ...],
):
    if not definitions:
        yield []
        return
    parent = definitions[0][0].parent
    if any(path.parent != parent for path, _markers in definitions):
        raise RuntimeError("Service definitions do not share one protected directory")
    with _open_service_definition_directory(
        parent,
        home,
        create=False,
        missing_ok=True,
    ) as directory:
        if directory is None:
            yield [(None, path, None) for path, _markers in definitions]
            return
        prepared = [
            (
                directory,
                path,
                _preflight_service_definition_removal_at(directory, path, markers),
            )
            for path, markers in definitions
        ]
        _assert_service_directory_unchanged(
            parent,
            _service_directory_identity(os.fstat(directory)),
        )
        yield prepared


def _remove_service_definition_at(
    directory: int | None,
    path: Path,
    expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    if expected is None:
        return
    if directory is None:
        raise RuntimeError(f"Service-definition directory disappeared before removal: {path.parent}")
    _assert_service_definition_at_unchanged(directory, path, expected)
    os.unlink(path.name, dir_fd=directory)


def _write_service_definition(path: Path, content: str, *, home: Path | None = None) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SERVICE_DEFINITION_BYTES:
        raise RuntimeError(f"Service definition exceeds the size limit: {path}")
    if home is not None:
        with _open_service_definition_directory(path.parent, home) as directory:
            expected = _inspect_service_definition_at(directory, path)
            temporary_name = ""
            for _attempt in range(16):
                candidate = f".{path.name}.{secrets.token_hex(16)}.tmp"
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(candidate, flags, 0o600, dir_fd=directory)
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            else:
                raise RuntimeError(f"Cannot reserve temporary service definition: {path}")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                _assert_service_definition_at_unchanged(directory, path, expected)
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                )
                temporary_name = ""
            finally:
                if temporary_name:
                    try:
                        os.unlink(temporary_name, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        return
    expected = _inspect_service_definition(path)
    _atomic_bytes(
        path,
        encoded,
        before_replace=lambda: _assert_service_definition_unchanged(path, expected),
    )


def _existing_signing_key_directory_anchor(path: Path) -> Path:
    """Return the nearest existing ancestor used to open a test/embedded path."""
    candidate = path
    while True:
        try:
            candidate.lstat()
            return candidate
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise RuntimeError(f"Signing-key directory has no existing anchor: {path}")
            candidate = parent


@contextlib.contextmanager
def _open_signing_key_directory(path: Path):
    """Hold the owner-controlled signing-key directory through one operation."""
    if os.name == "nt":
        path.parent.mkdir(parents=True, exist_ok=True)
        yield None
        return
    home = Path.home()
    try:
        path.parent.relative_to(home)
    except ValueError:
        # Tests and embedders may inject a path outside the account home. Start
        # at its nearest existing ancestor while retaining the same checks.
        home = _existing_signing_key_directory_anchor(path.parent)
    with _open_service_definition_directory(path.parent, home) as directory:
        yield directory


def _signing_key_lstat(path: Path, directory: int | None) -> os.stat_result:
    if directory is None:
        return path.lstat()
    return os.stat(path.name, dir_fd=directory, follow_symlinks=False)


def _open_signing_key_file(
    path: Path, directory: int | None, flags: int, mode: int,
) -> int:
    if directory is None:
        return os.open(path, flags, mode)
    return os.open(path.name, flags, mode, dir_fd=directory)


def _validate_signing_key_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Signing-key path is not a regular file: {path}")
    if details.st_size < 32 or details.st_size > 64 * 1024:
        raise RuntimeError(f"Signing key has an invalid size: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise RuntimeError(f"Signing-key permissions must be owner-only: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"Signing-key owner is invalid: {path}")


def ensure_signing_key(path: Path | None = None) -> None:
    """Create the local trust key once and never replace an existing key."""
    path = path or SIGNING_KEY
    with _open_signing_key_directory(path) as directory:
        try:
            details = _signing_key_lstat(path, directory)
        except FileNotFoundError:
            details = None
        if details is not None:
            _validate_signing_key_details(path, details)
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = _open_signing_key_file(path, directory, flags, 0o600)
        except FileExistsError:
            _validate_signing_key_details(path, _signing_key_lstat(path, directory))
            return
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(os.urandom(32))
                handle.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _service_token_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _validate_service_token_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Service-token path is not a regular file: {path}")
    if details.st_size < 32 or details.st_size > 64 * 1024:
        raise RuntimeError(f"Service token has an invalid size: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise RuntimeError(f"Service-token permissions must be owner-only: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"Service-token owner is invalid: {path}")


def _read_protected_service_token(path: Path) -> str:
    before = path.lstat()
    _validate_service_token_details(path, before)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        _validate_service_token_details(path, opened)
        if _service_token_identity(opened) != _service_token_identity(before):
            raise RuntimeError(f"Service token changed while opening: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(64 * 1024 + 1)
        after = os.fstat(descriptor)
        if _service_token_identity(after) != _service_token_identity(opened):
            raise RuntimeError(f"Service token changed while reading: {path}")
    finally:
        os.close(descriptor)
    if len(raw) > 64 * 1024:
        raise RuntimeError(f"Service token has an invalid size: {path}")
    try:
        token = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError(f"Service token is not valid UTF-8: {path}") from error
    if len(token) < 32:
        raise RuntimeError(f"Service token is invalid: {path}")
    return token


def ensure_service_token(path: Path | None = None) -> None:
    """Create a stable text token used only by host adapters and the MCP process."""
    path = path or SERVICE_TOKEN
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _read_protected_service_token(path)
        return
    except FileNotFoundError:
        pass
    token = os.urandom(32).hex().encode("ascii") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _read_protected_service_token(path)
        return
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(token)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mcp_http_token_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _validate_mcp_http_token_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"MCP access-token path is not a regular file: {path}")
    if details.st_size < 32 or details.st_size > MAX_MCP_HTTP_TOKEN_BYTES:
        raise RuntimeError(f"MCP access token has an invalid size: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise RuntimeError(f"MCP access-token permissions must be owner-only: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"MCP access-token owner is invalid: {path}")


def _read_protected_mcp_http_token(path: Path) -> str:
    before = path.lstat()
    _validate_mcp_http_token_details(path, before)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        _validate_mcp_http_token_details(path, opened)
        if _mcp_http_token_identity(opened) != _mcp_http_token_identity(before):
            raise RuntimeError(f"MCP access token changed while opening: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_MCP_HTTP_TOKEN_BYTES + 1)
        after_read = os.fstat(descriptor)
        after_path = path.lstat()
    finally:
        os.close(descriptor)
    if len(raw) > MAX_MCP_HTTP_TOKEN_BYTES:
        raise RuntimeError(f"MCP access token has an invalid size: {path}")
    if (
        _mcp_http_token_identity(after_read) != _mcp_http_token_identity(opened)
        or _mcp_http_token_identity(after_path) != _mcp_http_token_identity(opened)
    ):
        raise RuntimeError(f"MCP access token changed while reading: {path}")
    try:
        token = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError(f"MCP access token is not valid UTF-8: {path}") from error
    if len(token) < 32:
        raise RuntimeError(f"MCP access token is invalid: {path}")
    return token


def ensure_mcp_http_token(path: Path | None = None) -> None:
    """Create a stable bearer token for the loopback HTTP MCP endpoint."""
    path = path or MCP_HTTP_TOKEN
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _read_protected_mcp_http_token(path)
        return
    except FileNotFoundError:
        pass
    token = os.urandom(32).hex().encode("ascii") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _read_protected_mcp_http_token(path)
        return
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(token)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_delivery_boundary(root: Path) -> None:
    source = root / "integrations" / "enforced_delivery.py"
    if not source.is_file():
        raise RuntimeError(f"Missing delivery boundary: {source}")
    if DELIVERY_POLICY.exists() or DELIVERY_POLICY.is_symlink():
        _load_delivery_policy()
    source.chmod(source.stat().st_mode | 0o111)
    atomic_symlink(source, DELIVERY_COMMAND)
    ensure_signing_key()
    policy = json.loads((root / "integrations" / "delivery-policy.example.json").read_text(encoding="utf-8"))
    policy["isolated_service"] = {
        "required": True,
        "endpoint": SERVICE_ENDPOINT,
        "token_file": str(SERVICE_TOKEN),
        "audit_file": str(AUDIT_LOG),
    }
    _atomic_json(DELIVERY_POLICY, policy)
    print(f"Mandatory delivery command: {DELIVERY_COMMAND}")
    print(f"Fail-closed delivery policy: {DELIVERY_POLICY}")


def install_guard_runtime(root: Path) -> None:
    source = root / "integrations" / "guard_service.py"
    if not source.is_file():
        raise RuntimeError(f"Missing isolated guard service: {source}")
    source.chmod(source.stat().st_mode | 0o111)
    atomic_symlink(source, SERVICE_COMMAND)
    ensure_signing_key()
    ensure_service_token()
    print(f"Isolated guard service command: {SERVICE_COMMAND}")
    print(f"Content-free audit log: {AUDIT_LOG}")


def install_mcp_http_runtime(root: Path) -> None:
    gateway = root / "integrations" / "mcp_http_gateway.py"
    headers = root / "integrations" / "mcp_auth_headers.py"
    for source in (gateway, headers):
        if not source.is_file():
            raise RuntimeError(f"Missing persistent MCP component: {source}")
    ensure_mcp_http_token()
    for source in (gateway, headers):
        source.chmod(source.stat().st_mode | 0o111)
    atomic_symlink(gateway, MCP_HTTP_COMMAND)
    atomic_symlink(headers, MCP_HEADERS_COMMAND)
    print(f"Persistent MCP command: {MCP_HTTP_COMMAND}")
    print(f"Dynamic MCP headers command: {MCP_HEADERS_COMMAND}")


def _service_arguments(root: Path) -> list[str]:
    return [
        sys.executable,
        str(root / "integrations" / "guard_service.py"),
        "--endpoint", SERVICE_ENDPOINT,
        "--key-file", str(SIGNING_KEY),
        "--token-file", str(SERVICE_TOKEN),
        "--audit-file", str(AUDIT_LOG),
    ]


def _mcp_http_arguments(root: Path) -> list[str]:
    return [
        sys.executable,
        str(root / "integrations" / "mcp_http_gateway.py"),
        "--host", "127.0.0.1",
        "--port", "47632",
        "--path", "/mcp",
        "--access-token-file", str(MCP_HTTP_TOKEN),
        "--service-endpoint", SERVICE_ENDPOINT,
        "--service-token-file", str(SERVICE_TOKEN),
    ]


def _shell_command(arguments: list[str]) -> str:
    if platform.system() == "Windows":
        return subprocess.list2cmdline(arguments)
    return " ".join(shlex.quote(value) for value in arguments)


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _install_windows_restartable_task(task_name: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    executable = arguments[0]
    argument_line = subprocess.list2cmdline(arguments[1:])
    name = _powershell_literal(task_name)
    script = (
        "$ErrorActionPreference='Stop';"
        f"$action=New-ScheduledTaskAction -Execute {_powershell_literal(executable)} "
        f"-Argument {_powershell_literal(argument_line)};"
        "$trigger=New-ScheduledTaskTrigger -AtLogOn;"
        "$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
        "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-ExecutionTimeLimit (New-TimeSpan -Days 3650);"
        f"Register-ScheduledTask -TaskName {name} -Action $action -Trigger $trigger "
        "-Settings $settings -Force | Out-Null;"
        f"Start-ScheduledTask -TaskName {name}"
    )
    return _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def install_guard_autostart(root: Path) -> tuple[bool, str]:
    """Install and start a per-user service. Separate-user deployment stays an admin task."""
    arguments = _service_arguments(root)
    system = platform.system()
    if system == "Linux":
        home = Path.home()
        units = home / ".config" / "systemd" / "user"
        service = units / "blun-language-guard.service"
        _write_service_definition(
            service,
            "[Unit]\nDescription=BLUN isolated language release guard\n\n"
            "[Service]\nType=simple\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            f"ExecStart={_shell_command(arguments)}\nRestart=on-failure\nRestartSec=2\n\n"
            "[Install]\nWantedBy=default.target\n",
            home=home,
        )
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", service.name])
        return reload_result.returncode == 0 and enable_result.returncode == 0, str(service)
    if system == "Darwin":
        home = Path.home()
        agents = home / "Library" / "LaunchAgents"
        plist = agents / "ai.blun.language-guard.plist"
        program_arguments = "".join(f"<string>{_xml_escape(value)}</string>" for value in arguments)
        _write_service_definition(
            plist,
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict><key>Label</key><string>ai.blun.language-guard</string>"
            f"<key>ProgramArguments</key><array>{program_arguments}</array>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n",
            home=home,
        )
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _install_windows_restartable_task("BLUN Language Guard", arguments)
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard"
    return False, f"No guard-service adapter for {system}"


def install_mcp_http_autostart(root: Path) -> tuple[bool, str]:
    """Install the persistent Claude-facing HTTP MCP with OS-level restart policy."""
    arguments = _mcp_http_arguments(root)
    system = platform.system()
    if system == "Linux":
        home = Path.home()
        units = home / ".config" / "systemd" / "user"
        service = units / "blun-language-guard-mcp.service"
        _write_service_definition(
            service,
            "[Unit]\nDescription=BLUN persistent Streamable HTTP MCP\n"
            "After=blun-language-guard.service\nWants=blun-language-guard.service\n\n"
            "[Service]\nType=simple\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            f"ExecStart={_shell_command(arguments)}\nRestart=always\nRestartSec=1\n\n"
            "[Install]\nWantedBy=default.target\n",
            home=home,
        )
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", service.name])
        return reload_result.returncode == 0 and enable_result.returncode == 0, str(service)
    if system == "Darwin":
        home = Path.home()
        agents = home / "Library" / "LaunchAgents"
        plist = agents / "ai.blun.language-guard-mcp.plist"
        program_arguments = "".join(f"<string>{_xml_escape(value)}</string>" for value in arguments)
        _write_service_definition(
            plist,
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict><key>Label</key><string>ai.blun.language-guard-mcp</string>"
            f"<key>ProgramArguments</key><array>{program_arguments}</array>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            "<key>ThrottleInterval</key><integer>1</integer></dict></plist>\n",
            home=home,
        )
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _install_windows_restartable_task("BLUN Language Guard MCP", arguments)
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard MCP"
    return False, f"No persistent MCP adapter for {system}"


def restart_guard_runtime() -> tuple[bool, str]:
    system = platform.system()
    if system == "Linux":
        result = _run(["systemctl", "--user", "restart", "blun-language-guard.service"])
        return result.returncode == 0, "systemd user service"
    if system == "Darwin":
        result = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.blun.language-guard"])
        return result.returncode == 0, "LaunchAgent"
    if system == "Windows":
        _run(["schtasks", "/End", "/TN", "BLUN Language Guard"])
        result = _run(["schtasks", "/Run", "/TN", "BLUN Language Guard"])
        return result.returncode == 0, "Windows Task Scheduler"
    return False, f"No guard-service adapter for {system}"


def restart_mcp_http_runtime() -> tuple[bool, str]:
    system = platform.system()
    if system == "Linux":
        result = _run(["systemctl", "--user", "restart", "blun-language-guard-mcp.service"])
        return result.returncode == 0, "systemd user service"
    if system == "Darwin":
        result = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.blun.language-guard-mcp"])
        return result.returncode == 0, "LaunchAgent"
    if system == "Windows":
        _run(["schtasks", "/End", "/TN", "BLUN Language Guard MCP"])
        result = _run(["schtasks", "/Run", "/TN", "BLUN Language Guard MCP"])
        return result.returncode == 0, "Windows Task Scheduler"
    return False, f"No persistent MCP adapter for {system}"


def remove_mcp_http_autostart() -> None:
    system = platform.system()
    if system == "Linux":
        home = Path.home()
        service = home / ".config" / "systemd" / "user" / "blun-language-guard-mcp.service"
        definitions = ((
            service,
            ("BLUN persistent Streamable HTTP MCP", "mcp_http_gateway.py", "--port 47632"),
        ),)
        with _prepare_service_definition_removals(home, definitions) as prepared:
            _run(["systemctl", "--user", "disable", "--now", "blun-language-guard-mcp.service"])
            for directory, path, expected in prepared:
                _remove_service_definition_at(directory, path, expected)
            _run(["systemctl", "--user", "daemon-reload"])
    elif system == "Darwin":
        home = Path.home()
        plist = home / "Library" / "LaunchAgents" / "ai.blun.language-guard-mcp.plist"
        definitions = ((
            plist,
            ("ai.blun.language-guard-mcp", "mcp_http_gateway.py", "<string>47632</string>"),
        ),)
        with _prepare_service_definition_removals(home, definitions) as prepared:
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
            for directory, path, expected in prepared:
                _remove_service_definition_at(directory, path, expected)
    elif system == "Windows":
        _run(["schtasks", "/Delete", "/F", "/TN", "BLUN Language Guard MCP"])


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def probe_guard_service(timeout: float = 3.0) -> dict:
    root = repository_root()
    client_path = root / "translate-native" / "scripts" / "guard_service_client.py"
    spec = importlib.util.spec_from_file_location("blun_installer_guard_client", client_path)
    assert spec and spec.loader
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    token = client.load_service_token(SERVICE_TOKEN)
    return client.call_guard_service(
        SERVICE_ENDPOINT,
        {"operation": "health"},
        auth_token=token,
        timeout=timeout,
    )


def guard_service(action: str) -> int:
    root = repository_root()
    if action in {"install", "start"}:
        install_guard_runtime(root)
        ok, detail = install_guard_autostart(root)
        print(f"{'Guard service installed and started' if ok else 'Guard service installation failed'}: {detail}")
        return 0 if ok else 1
    if action == "stop":
        system = platform.system()
        if system == "Linux":
            result = _run(["systemctl", "--user", "stop", "blun-language-guard.service"])
        elif system == "Darwin":
            result = _run(["launchctl", "bootout", f"gui/{os.getuid()}/ai.blun.language-guard"])
        elif system == "Windows":
            result = _run(["schtasks", "/End", "/TN", "BLUN Language Guard"])
        else:
            return 2
        return int(result.returncode != 0)
    try:
        result = probe_guard_service()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"BLOCK guard service unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" and result.get("isolated_key") is True else 1


def claude_mcp_entry() -> dict:
    return {
        "type": "http",
        "url": MCP_HTTP_URL,
        "headersHelper": str(MCP_HEADERS_COMMAND),
    }


def configure_claude_mcp(path: Path | None = None) -> tuple[Path | None, int]:
    """Atomically install the user-scoped HTTP MCP and remove stale local shadows."""
    path = path or CLAUDE_CONFIG
    current, original, expected = _read_protected_claude_config(path)
    servers = current.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"Claude mcpServers must be an object: {path}")
    servers[MCP_SERVER_NAME] = claude_mcp_entry()

    removed_shadows = 0
    projects = current.get("projects", {})
    if isinstance(projects, dict):
        for project in projects.values():
            if not isinstance(project, dict):
                continue
            local_servers = project.get("mcpServers")
            if isinstance(local_servers, dict) and MCP_SERVER_NAME in local_servers:
                del local_servers[MCP_SERVER_NAME]
                removed_shadows += 1

    backup = None
    if original is not None:
        backup = path.with_suffix(path.suffix + ".bak")
        _atomic_bytes(backup, original)
    encoded = (json.dumps(current, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(
        path,
        encoded,
        before_replace=lambda: _assert_claude_config_unchanged(path, expected),
    )
    return backup, removed_shadows


def preflight_claude_config(path: Path | None = None) -> None:
    """Reject unsafe Claude user configuration before installation mutates runtime."""
    path = path or CLAUDE_CONFIG
    current, _original, _expected = _read_protected_claude_config(path)
    servers = current.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"Claude mcpServers must be an object: {path}")


def _project_mcp_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _validate_project_mcp_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Claude project MCP configuration is not a regular file: {path}")
    if details.st_nlink != 1:
        raise RuntimeError(
            f"Claude project MCP configuration has additional hard links: {path}"
        )
    if details.st_size > MAX_PROJECT_MCP_CONFIG_BYTES:
        raise RuntimeError(f"Claude project MCP configuration exceeds the size limit: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(
            f"Claude project MCP configuration is writable outside its owner: {path}"
        )
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"Claude project MCP configuration owner is invalid: {path}")


def _read_protected_project_mcp(path: Path) -> dict:
    """Read a project-scoped Claude MCP file without following or racing links."""
    try:
        before = path.lstat()
    except OSError as error:
        raise RuntimeError(f"Claude project MCP configuration is unreadable: {path}") from error
    _validate_project_mcp_details(path, before)
    expected = _project_mcp_identity(before)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _validate_project_mcp_details(path, opened)
            if _project_mcp_identity(opened) != expected:
                raise RuntimeError(
                    f"Claude project MCP configuration changed while opening: {path}"
                )
            raw = handle.read(MAX_PROJECT_MCP_CONFIG_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        after_path = path.lstat()
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"Claude project MCP configuration is unreadable: {path}") from error
    if len(raw) > MAX_PROJECT_MCP_CONFIG_BYTES:
        raise RuntimeError(f"Claude project MCP configuration exceeds the size limit: {path}")
    if (
        _project_mcp_identity(after_read) != expected
        or _project_mcp_identity(after_path) != expected
    ):
        raise RuntimeError(f"Claude project MCP configuration changed while reading: {path}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Claude project MCP configuration is invalid: {path}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Claude project MCP configuration root must be an object: {path}")
    servers = payload.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"Claude project mcpServers must be an object: {path}")
    return payload


def project_mcp_shadows(start: Path | None = None) -> list[Path]:
    """Return higher-precedence project MCP files that redefine the guard differently."""
    current = (start or Path.cwd()).resolve()
    candidates = [current, *current.parents]
    shadows: list[Path] = []
    for directory in candidates:
        path = directory / ".mcp.json"
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"Claude project MCP configuration is unreadable: {path}") from error
        payload = _read_protected_project_mcp(path)
        entry = payload.get("mcpServers", {}).get(MCP_SERVER_NAME)
        if entry is not None and entry != claude_mcp_entry():
            shadows.append(path)
    return shadows


def _mcp_http_request(path: str, payload: dict | None = None, *, timeout: float = 4.0) -> tuple[int, dict]:
    token = _read_protected_mcp_http_token(MCP_HTTP_TOKEN)
    url = MCP_HTTP_URL.removesuffix("/mcp") + path
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    method = "GET"
    if payload is not None:
        method = "POST"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        })
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        detail = json.loads(raw.decode("utf-8")) if raw else {}
        return error.code, detail


def probe_mcp_http(timeout: float = 4.0) -> dict:
    health_status, health = _mcp_http_request("/healthz", timeout=timeout)
    if health_status != 200 or health.get("status") != "ok" or health.get("isolated_key") is not True:
        raise RuntimeError(f"persistent MCP health failed with HTTP {health_status}")
    initialize_status, initialized = _mcp_http_request("/mcp", {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "blun-language-guard-doctor", "version": "1"},
        },
    }, timeout=timeout)
    tools_status, tools = _mcp_http_request("/mcp", {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }, timeout=timeout)
    canary_status, canary = _mcp_http_request("/mcp", {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "validate_text",
            "arguments": {"text": "Hälsokontrollen är aktiv.", "language": "sv-SE"},
        },
    }, timeout=timeout)
    names = {
        tool.get("name")
        for tool in tools.get("result", {}).get("tools", [])
        if isinstance(tool, dict)
    }
    canary_result = canary.get("result", {}).get("structuredContent", {})
    if initialize_status != 200 or tools_status != 200 or canary_status != 200 or not {
        "release_response", "release_translation", "verify_release_token",
    } <= names or canary_result.get("status") != "PASS" or canary_result.get("release_allowed") is not True:
        raise RuntimeError("persistent MCP initialize/tools/call probe failed")
    return {
        "health": health,
        "initialize": initialized,
        "tools": sorted(names),
        "canary": {"status": canary_result["status"], "language": canary_result.get("language")},
    }


def mcp_service(action: str) -> int:
    root = repository_root()
    if action in {"install", "start"}:
        install_mcp_http_runtime(root)
        ok, detail = install_mcp_http_autostart(root)
        print(f"{'Persistent MCP installed and started' if ok else 'Persistent MCP installation failed'}: {detail}")
        return 0 if ok else 1
    if action == "stop":
        system = platform.system()
        if system == "Linux":
            result = _run(["systemctl", "--user", "stop", "blun-language-guard-mcp.service"])
        elif system == "Darwin":
            result = _run(["launchctl", "bootout", f"gui/{os.getuid()}/ai.blun.language-guard-mcp"])
        elif system == "Windows":
            result = _run(["schtasks", "/End", "/TN", "BLUN Language Guard MCP"])
        else:
            return 2
        return int(result.returncode != 0)
    try:
        result = probe_mcp_http()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"BLOCK persistent MCP unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def install(targets: list[str], *, autostart_service: bool = True) -> int:
    blun_config = Path.home() / ".blun" / "mcp.json" if "blun" in targets else None
    if blun_config is not None:
        preflight_blun_mcp_config(blun_config)
    if "claude" in targets:
        preflight_claude_config()
    root = repository_root()
    skill = root / "translate-native"
    for target in targets:
        atomic_symlink(skill, TARGETS[target])
        print(f"OK {target}: {TARGETS[target]} -> {skill}")
    install_delivery_boundary(root)
    install_guard_runtime(root)
    if "claude" in targets:
        install_mcp_http_runtime(root)
    config = {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": sys.executable,
                "args": [str(skill / "scripts" / "blun_language_guard.py"), "serve"],
                "env": {
                    "BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT": SERVICE_ENDPOINT,
                    "BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE": str(SERVICE_TOKEN),
                },
            }
        }
    }
    output = Path.home() / ".config" / "blun-language-guard" / "mcp-snippet.json"
    _atomic_json(output, config)
    print(f"MCP snippet written without modifying host configuration: {output}")
    if blun_config is not None:
        backup = merge_blun_mcp_config(
            blun_config, config["mcpServers"][MCP_SERVER_NAME]
        )
        if backup:
            print(f"BLUN MCP backup: {backup}")
        print(f"BLUN MCP configuration merged: {blun_config}")
    if autostart_service:
        ok, detail = install_guard_autostart(root)
        print(f"{'Guard service installed and started' if ok else 'Guard service installation failed'}: {detail}")
        if not ok:
            return 1
        guard_ready = False
        for _attempt in range(15):
            try:
                health = probe_guard_service()
                guard_ready = health.get("status") == "ok" and health.get("isolated_key") is True
                if guard_ready:
                    break
            except (OSError, RuntimeError, ValueError):
                pass
            time.sleep(0.2)
        if not guard_ready:
            print("Guard service did not become healthy; Claude configuration was not changed.", file=sys.stderr)
            return 1
        if "claude" in targets:
            mcp_ok, mcp_detail = install_mcp_http_autostart(root)
            print(f"{'Persistent MCP installed and started' if mcp_ok else 'Persistent MCP installation failed'}: {mcp_detail}")
            if not mcp_ok:
                return 1
            mcp_ready = False
            for _attempt in range(15):
                try:
                    probe_mcp_http()
                    mcp_ready = True
                    break
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                    time.sleep(0.2)
            if not mcp_ready:
                print("Persistent MCP did not become healthy; Claude configuration was not changed.", file=sys.stderr)
                return 1
    if "claude" in targets:
        backup, removed_shadows = configure_claude_mcp()
        print(f"Claude user MCP configured as persistent HTTP: {CLAUDE_CONFIG}")
        if backup:
            print(f"Claude configuration backup: {backup}")
        if removed_shadows:
            print(f"Removed {removed_shadows} stale project-local Claude MCP shadow(s).")
        if autostart_service and health_monitor("install") != 0:
            return 1
    return 0


def _run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def _find_claude_plugin(value: object) -> dict | None:
    """Accept Claude's documented JSON list while tolerating a wrapped list."""
    if isinstance(value, list):
        plugins = value
    elif isinstance(value, dict):
        plugins = next(
            (
                value[name]
                for name in ("plugins", "installedPlugins", "installed", "availablePlugins", "available")
                if isinstance(value.get(name), list)
            ),
            [],
        )
    else:
        plugins = []
    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        name = str(plugin.get("name") or plugin.get("id") or plugin.get("plugin") or "")
        marketplace = str(plugin.get("marketplace") or plugin.get("sourceMarketplace") or "")
        if name == CLAUDE_PLUGIN_NAME or (
            name == "translate-native" and marketplace in {"", "blun-language-tools"}
        ):
            return plugin
    return None


def claude_plugin_status(expected_version: str, executable: str | None = None) -> dict:
    """Inspect the cached user plugin without reading Claude's private cache format."""
    command = executable or shutil.which("claude")
    if not command:
        return {"installed": False, "healthy": False, "reason": "claude-command-unavailable"}
    try:
        result = _run([command, "plugin", "list", "--json"])
    except OSError:
        return {"installed": False, "healthy": False, "reason": "claude-command-unavailable"}
    if result.returncode:
        return {"installed": False, "healthy": False, "reason": "plugin-list-failed"}
    try:
        plugin = _find_claude_plugin(json.loads(result.stdout))
    except json.JSONDecodeError:
        return {"installed": False, "healthy": False, "reason": "plugin-list-invalid-json"}
    if plugin is None:
        return {"installed": False, "healthy": False, "reason": "plugin-not-installed"}
    version = str(plugin.get("version") or plugin.get("installedVersion") or "")
    enabled = plugin.get("enabled") is not False
    errors = plugin.get("errors")
    errors = errors if isinstance(errors, list) else ([] if not errors else [errors])
    return {
        "installed": True,
        "healthy": enabled and not errors and version == expected_version,
        "enabled": enabled,
        "errors": errors,
        "version": version,
        "expected_version": expected_version,
    }


def claude_plugin_catalog_status(expected_version: str, executable: str | None = None) -> dict:
    """Require the refreshed public marketplace catalog to match the tested runtime."""
    command = executable or shutil.which("claude")
    if not command:
        return {"available": False, "healthy": False, "reason": "claude-command-unavailable"}
    try:
        result = _run([command, "plugin", "list", "--available", "--json"])
    except OSError:
        return {"available": False, "healthy": False, "reason": "claude-command-unavailable"}
    if result.returncode:
        return {"available": False, "healthy": False, "reason": "plugin-catalog-list-failed"}
    try:
        plugin = _find_claude_plugin(json.loads(result.stdout))
    except json.JSONDecodeError:
        return {"available": False, "healthy": False, "reason": "plugin-catalog-invalid-json"}
    if plugin is None:
        return {"available": False, "healthy": False, "reason": "plugin-not-in-refreshed-catalog"}
    version = str(plugin.get("availableVersion") or plugin.get("latestVersion") or plugin.get("version") or "")
    return {
        "available": True,
        "healthy": version == expected_version,
        "version": version,
        "expected_version": expected_version,
        **({"reason": "catalog-version-mismatch"} if version != expected_version else {}),
    }


def preflight_claude_plugin_update(
    expected_version: str,
    executable: str | None = None,
    plugin_root: Path | None = None,
) -> dict:
    """Prove a tested plugin is available before any runtime cutover."""
    before = claude_plugin_status(expected_version, executable)
    if before.get("reason") == "plugin-not-installed":
        return {
            "attempted": False,
            "ready": True,
            "needs_update": False,
            "status": before,
            "expected_version": expected_version,
        }
    if not before.get("installed"):
        return {
            "attempted": False,
            "ready": False,
            "needs_update": False,
            "status": before,
            "expected_version": expected_version,
        }
    if before.get("healthy") is True:
        return {
            "attempted": False,
            "ready": True,
            "needs_update": False,
            "status": before,
            "expected_version": expected_version,
        }
    command = executable or shutil.which("claude")
    assert command
    root = (plugin_root or repository_root()).resolve()
    try:
        validation = _run([command, "plugin", "validate", str(root), "--strict"])
    except OSError:
        return {
            "attempted": True,
            "ready": False,
            "needs_update": True,
            "status": before,
            "expected_version": expected_version,
            "validation": {
                "healthy": False,
                "reason": "claude-command-unavailable",
                "plugin_root": str(root),
            },
        }
    validation_status = {
        "healthy": validation.returncode == 0,
        "reason": "ok" if validation.returncode == 0 else "strict-plugin-validation-failed",
        "plugin_root": str(root),
    }
    if validation.returncode:
        return {
            "attempted": True,
            "ready": False,
            "needs_update": True,
            "returncode": validation.returncode,
            "status": before,
            "expected_version": expected_version,
            "validation": validation_status,
        }
    try:
        refresh = _run([command, "plugin", "marketplace", "update", CLAUDE_MARKETPLACE_NAME])
    except OSError:
        return {
            "attempted": True,
            "ready": False,
            "needs_update": True,
            "status": before,
            "expected_version": expected_version,
            "validation": validation_status,
            "catalog": {"available": False, "healthy": False, "reason": "claude-command-unavailable"},
        }
    if refresh.returncode:
        return {
            "attempted": True,
            "ready": False,
            "needs_update": True,
            "returncode": refresh.returncode,
            "status": before,
            "expected_version": expected_version,
            "validation": validation_status,
            "catalog": {"available": False, "healthy": False, "reason": "marketplace-update-failed"},
        }
    catalog = claude_plugin_catalog_status(expected_version, command)
    if catalog.get("healthy") is not True:
        return {
            "attempted": True,
            "ready": False,
            "needs_update": True,
            "returncode": 1,
            "status": before,
            "expected_version": expected_version,
            "validation": validation_status,
            "catalog": catalog,
        }
    return {
        "attempted": True,
        "ready": True,
        "needs_update": True,
        "status": before,
        "expected_version": expected_version,
        "validation": validation_status,
        "catalog": catalog,
    }


def _apply_claude_plugin_update(
    expected_version: str,
    executable: str | None,
    preflight: dict,
) -> dict:
    """Apply only a matching successful preflight and verify the public cache state."""
    if preflight.get("ready") is not True or preflight.get("expected_version") != expected_version:
        return {**preflight, "updated": False, "reload_required": False}
    if preflight.get("needs_update") is not True:
        status = claude_plugin_status(expected_version, executable)
        installed_or_absent = status.get("healthy") is True or status.get("reason") == "plugin-not-installed"
        return {
            **preflight,
            "updated": installed_or_absent,
            "status": status,
            "reload_required": False,
        }
    command = executable or shutil.which("claude")
    if not command:
        return {
            **preflight,
            "updated": False,
            "status": {"installed": False, "healthy": False, "reason": "claude-command-unavailable"},
            "reload_required": False,
        }
    current = claude_plugin_status(expected_version, command)
    if current.get("healthy") is True:
        return {**preflight, "updated": True, "status": current, "reload_required": False}
    if current.get("installed") is not True:
        return {
            **preflight,
            "updated": False,
            "status": {
                **current,
                "healthy": False,
                "reason": "plugin-disappeared-after-preflight",
            },
            "reload_required": False,
        }
    try:
        result = _run([command, "plugin", "update", CLAUDE_PLUGIN_NAME, "--scope", "user"])
    except OSError:
        return {
            **preflight,
            "updated": False,
            "status": {**current, "healthy": False, "reason": "claude-command-unavailable"},
            "reload_required": False,
        }
    after = claude_plugin_status(expected_version, command)
    updated = result.returncode == 0 and after.get("healthy") is True
    return {
        **preflight,
        "updated": updated,
        "returncode": result.returncode,
        "status": after,
        "reload_required": updated and preflight.get("status", {}).get("version") != expected_version,
    }


def update_claude_plugin(expected_version: str, executable: str | None = None) -> dict:
    """Preflight, update, and verify the exact tested plugin."""
    preflight = preflight_claude_plugin_update(expected_version, executable)
    before = preflight.get("status", {})
    result = _apply_claude_plugin_update(expected_version, executable, preflight)
    if result.get("updated") and before.get("version") != expected_version:
        result["reload_required"] = True
    return result


def doctor() -> int:
    root = repository_root()
    expected_version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    checks: list[tuple[str, bool, str]] = []
    for name, target in TARGETS.items():
        checks.append((f"{name} skill", target.is_symlink() and target.resolve() == (root / "translate-native").resolve(), str(target)))
    delivery_source = root / "integrations" / "enforced_delivery.py"
    checks.append((
        "mandatory delivery command",
        DELIVERY_COMMAND.is_symlink() and DELIVERY_COMMAND.resolve() == delivery_source.resolve(),
        str(DELIVERY_COMMAND),
    ))
    key_secure = SIGNING_KEY.is_file() and (os.name == "nt" or SIGNING_KEY.stat().st_mode & 0o077 == 0)
    checks.append(("signing key", key_secure, str(SIGNING_KEY)))
    service_source = root / "integrations" / "guard_service.py"
    checks.append((
        "isolated guard command",
        SERVICE_COMMAND.is_symlink() and SERVICE_COMMAND.resolve() == service_source.resolve(),
        str(SERVICE_COMMAND),
    ))
    try:
        _read_protected_service_token(SERVICE_TOKEN)
        token_secure = True
    except (OSError, RuntimeError):
        token_secure = False
    checks.append(("service authentication token", token_secure, str(SERVICE_TOKEN)))
    mcp_gateway_source = root / "integrations" / "mcp_http_gateway.py"
    mcp_headers_source = root / "integrations" / "mcp_auth_headers.py"
    checks.append((
        "persistent MCP command",
        MCP_HTTP_COMMAND.is_symlink() and MCP_HTTP_COMMAND.resolve() == mcp_gateway_source.resolve(),
        str(MCP_HTTP_COMMAND),
    ))
    checks.append((
        "dynamic MCP headers command",
        MCP_HEADERS_COMMAND.is_symlink() and MCP_HEADERS_COMMAND.resolve() == mcp_headers_source.resolve(),
        str(MCP_HEADERS_COMMAND),
    ))
    try:
        _read_protected_mcp_http_token(MCP_HTTP_TOKEN)
        mcp_token_secure = True
    except (OSError, RuntimeError):
        mcp_token_secure = False
    checks.append(("MCP HTTP access token", mcp_token_secure, str(MCP_HTTP_TOKEN)))
    claude_config_ok = False
    claude_config_detail = str(CLAUDE_CONFIG)
    try:
        claude_config, original_claude_config, _identity = _read_protected_claude_config(
            CLAUDE_CONFIG
        )
        if original_claude_config is not None:
            claude_entry = claude_config.get("mcpServers", {}).get(MCP_SERVER_NAME)
            local_shadows = sum(
                1
                for project in claude_config.get("projects", {}).values()
                if isinstance(project, dict)
                and MCP_SERVER_NAME in project.get("mcpServers", {})
            ) if isinstance(claude_config.get("projects", {}), dict) else 0
            claude_config_ok = claude_entry == claude_mcp_entry() and local_shadows == 0
            claude_config_detail += f"; stale local shadows={local_shadows}"
    except (OSError, RuntimeError, AttributeError, TypeError):
        claude_config_ok = False
    checks.append(("Claude user-scoped HTTP MCP", claude_config_ok, claude_config_detail))
    try:
        project_shadows = project_mcp_shadows()
        project_mcp_ok = not project_shadows
        project_mcp_detail = (
            "none" if not project_shadows else ", ".join(str(path) for path in project_shadows)
        )
    except RuntimeError as error:
        project_shadows = []
        project_mcp_ok = False
        project_mcp_detail = str(error)
    checks.append((
        "Claude project MCP precedence",
        project_mcp_ok,
        project_mcp_detail,
    ))
    try:
        policy_ok = _load_delivery_policy() is not None
    except RuntimeError:
        policy_ok = False
    checks.append(("fail-closed delivery policy", policy_ok, str(DELIVERY_POLICY)))
    service_live = guard_service("status") == 0
    checks.append(("isolated guard health", service_live, SERVICE_ENDPOINT))
    try:
        persistent_probe = probe_mcp_http()
        persistent_live = True
        persistent_detail = ", ".join(persistent_probe["tools"])
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        persistent_live = False
        persistent_detail = str(error)
    checks.append(("persistent Claude HTTP MCP", persistent_live, persistent_detail))
    plugin = claude_plugin_status(expected_version)
    if plugin.get("reason") != "claude-command-unavailable":
        checks.append((
            "Claude plugin cache",
            plugin.get("healthy") is True,
            json.dumps(plugin, ensure_ascii=False, sort_keys=True),
        ))
    tests = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], root)
    checks.append(("test suite", tests.returncode == 0, tests.stderr.strip() or tests.stdout.strip()))
    server = root / "translate-native" / "scripts" / "blun_language_guard.py"
    probe = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        '{"jsonrpc":"2.0","id":3,"method":"prompts/list"}\n'
    )
    mcp = subprocess.run([sys.executable, str(server), "serve"], input=probe, text=True, capture_output=True, check=False)
    tools_ok = (
        mcp.returncode == 0
        and "release_response" in mcp.stdout
        and "release_translation" in mcp.stdout
        and "verify_release_token" in mcp.stdout
        and "translate-native" in mcp.stdout
        and "Never use release_response to bypass" in mcp.stdout
    )
    checks.append(("live MCP tools", tools_ok, mcp.stderr.strip() or mcp.stdout[:240]))
    quality_path = root / "translate-native" / "scripts" / "language_quality.py"
    spec = importlib.util.spec_from_file_location("blun_doctor_quality", quality_path)
    assert spec and spec.loader
    quality = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quality)
    with tempfile.TemporaryDirectory(prefix="blun-doctor-") as directory:
        key = quality.load_or_create_key(Path(directory) / "signing.key")
        receipt = quality.issue_receipt("Hello", "Hej", "sv-SE", key)
        valid = quality.verify_receipt(receipt, "Hello", "Hej", "sv-SE", key)["valid"]
        tamper_blocked = not quality.verify_receipt(receipt, "Hello", "Hallå", "sv-SE", key)["valid"]
        purpose_blocked = not quality.verify_receipt(
            receipt, "Hello", "Hej", "sv-SE", key, purpose="response"
        )["valid"]
    checks.append((
        "signed receipt round-trip",
        valid and tamper_blocked and purpose_blocked,
        "valid receipt accepted; edited target and wrong purpose rejected",
    ))
    try:
        updater_config = _load_update_policy(UPDATE_CONFIG)
    except RuntimeError:
        checks.append(("automatic updater policy", False, str(UPDATE_CONFIG)))
        updater_config = None
    if updater_config is not None:
        try:
            state = _load_update_state() or {}
            maximum_age = int(updater_config.get("interval_hours", 24)) * 7200
            fresh = int(time.time()) - int(state.get("checked_at", 0)) <= maximum_age
        except (RuntimeError, TypeError, ValueError):
            fresh = False
        checks.append(("automatic updater heartbeat", fresh, str(UPDATE_STATE)))
    try:
        monitor_enabled = health_monitor_enabled()
    except RuntimeError:
        checks.append(("automatic health monitor policy", False, str(HEALTH_CONFIG)))
        monitor_enabled = False
    if TARGETS["claude"].is_symlink() and monitor_enabled:
        try:
            health_state = _load_health_state() or {}
            health_fresh = int(time.time()) - int(health_state.get("checked_at", 0)) <= 180
            monitor_ok = health_fresh and health_state.get("status") in {"ok", "recovered"}
        except (RuntimeError, TypeError, ValueError):
            monitor_ok = False
        checks.append(("automatic health monitor", monitor_ok, str(HEALTH_STATE)))
    failed = False
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
        failed |= not passed
    return int(failed)


def _atomic_json(path: Path, payload: dict, *, before_replace=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _claude_config_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _validate_claude_config_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Claude configuration is not a regular file: {path}")
    if details.st_nlink != 1:
        raise RuntimeError(f"Claude configuration has additional hard links: {path}")
    if details.st_size > MAX_CLAUDE_CONFIG_BYTES:
        raise RuntimeError(f"Claude configuration exceeds the size limit: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(f"Claude configuration is writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"Claude configuration owner is invalid: {path}")


def _read_protected_claude_config(
    path: Path,
) -> tuple[dict, bytes | None, tuple[int, int, int, int, int, int] | None]:
    """Read Claude's user configuration without following links or racing replacement."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {}, None, None
    except OSError as error:
        raise RuntimeError(f"Claude configuration is unreadable: {path}") from error
    _validate_claude_config_details(path, before)
    expected = _claude_config_identity(before)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _validate_claude_config_details(path, opened)
            if _claude_config_identity(opened) != expected:
                raise RuntimeError(f"Claude configuration changed while opening: {path}")
            raw = handle.read(MAX_CLAUDE_CONFIG_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        after_path = path.lstat()
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"Claude configuration is unreadable: {path}") from error
    if len(raw) > MAX_CLAUDE_CONFIG_BYTES:
        raise RuntimeError(f"Claude configuration exceeds the size limit: {path}")
    if (
        _claude_config_identity(after_read) != expected
        or _claude_config_identity(after_path) != expected
    ):
        raise RuntimeError(f"Claude configuration changed while reading: {path}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Claude configuration is invalid: {path}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Claude configuration root must be an object: {path}")
    return payload, raw, expected


def _assert_claude_config_unchanged(
    path: Path, expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        if expected is None:
            return
        raise RuntimeError(f"Claude configuration disappeared before replacement: {path}")
    except OSError as error:
        raise RuntimeError(f"Claude configuration cannot be rechecked: {path}") from error
    if expected is None:
        raise RuntimeError(f"Claude configuration appeared during installation: {path}")
    _validate_claude_config_details(path, current)
    if _claude_config_identity(current) != expected:
        raise RuntimeError(f"Claude configuration changed before replacement: {path}")


def _blun_config_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _validate_blun_config_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"BLUN MCP configuration is not a regular file: {path}")
    if details.st_nlink != 1:
        raise RuntimeError(f"BLUN MCP configuration has additional hard links: {path}")
    if details.st_size > MAX_BLUN_MCP_CONFIG_BYTES:
        raise RuntimeError(f"BLUN MCP configuration exceeds the size limit: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(f"BLUN MCP configuration is writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"BLUN MCP configuration owner is invalid: {path}")


def _read_protected_blun_config(path: Path) -> tuple[dict, bytes | None, tuple[int, int, int, int, int, int] | None]:
    """Read BLUN's security-relevant MCP config without following or racing links."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {}, None, None
    except OSError as error:
        raise RuntimeError(f"BLUN MCP configuration is unreadable: {path}") from error
    _validate_blun_config_details(path, before)
    expected = _blun_config_identity(before)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _validate_blun_config_details(path, opened)
            if _blun_config_identity(opened) != expected:
                raise RuntimeError(f"BLUN MCP configuration changed while opening: {path}")
            raw = handle.read(MAX_BLUN_MCP_CONFIG_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        after_path = path.lstat()
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"BLUN MCP configuration is unreadable: {path}") from error
    if len(raw) > MAX_BLUN_MCP_CONFIG_BYTES:
        raise RuntimeError(f"BLUN MCP configuration exceeds the size limit: {path}")
    if (
        _blun_config_identity(after_read) != expected
        or _blun_config_identity(after_path) != expected
    ):
        raise RuntimeError(f"BLUN MCP configuration changed while reading: {path}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"BLUN MCP configuration is invalid: {path}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"BLUN MCP configuration root must be an object: {path}")
    return payload, raw, expected


def _assert_blun_config_unchanged(
    path: Path, expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        if expected is None:
            return
        raise RuntimeError(f"BLUN MCP configuration disappeared before replacement: {path}")
    except OSError as error:
        raise RuntimeError(f"BLUN MCP configuration cannot be rechecked: {path}") from error
    if expected is None:
        raise RuntimeError(f"BLUN MCP configuration appeared during installation: {path}")
    _validate_blun_config_details(path, current)
    if _blun_config_identity(current) != expected:
        raise RuntimeError(f"BLUN MCP configuration changed before replacement: {path}")


def _atomic_bytes(path: Path, payload: bytes, *, before_replace=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def merge_blun_mcp_config(path: Path, entry: dict) -> Path | None:
    """Preserve unrelated BLUN MCP servers while atomically installing the guard."""
    current, original, expected = _read_protected_blun_config(path)
    servers = current.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"BLUN mcpServers must be an object: {path}")
    servers[MCP_SERVER_NAME] = entry
    backup = None
    if original is not None:
        backup = path.with_suffix(".json.bak")
        _atomic_bytes(backup, original)
    encoded = (json.dumps(current, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(
        path,
        encoded,
        before_replace=lambda: _assert_blun_config_unchanged(path, expected),
    )
    return backup


def preflight_blun_mcp_config(path: Path) -> None:
    """Reject unsafe BLUN configuration before installation mutates any runtime."""
    current, _original, _expected = _read_protected_blun_config(path)
    servers = current.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"BLUN mcpServers must be an object: {path}")


def _validate_update_policy_details(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Unsafe updater policy file type: {path}")
    if details.st_size > MAX_UPDATE_POLICY_BYTES:
        raise RuntimeError(f"Updater policy exceeds size limit: {path}")


def _read_update_policy(
    path: Path,
) -> tuple[dict | None, tuple[int, int, int, int, int, int] | None]:
    """Read one bounded regular policy file without following symbolic links."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise RuntimeError(f"Unreadable updater policy: {path}") from error
    _validate_update_policy_details(path, metadata)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _validate_update_policy_details(path, opened)
            if _protected_state_identity(metadata) != _protected_state_identity(opened):
                raise RuntimeError(f"Updater policy changed while opening: {path}")
            raw = handle.read(MAX_UPDATE_POLICY_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        after_path = path.lstat()
    except (OSError, RuntimeError) as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"Unreadable updater policy: {path}") from error
    if len(raw) > MAX_UPDATE_POLICY_BYTES:
        raise RuntimeError(f"Updater policy exceeds size limit: {path}")
    if (
        _protected_state_identity(opened) != _protected_state_identity(after_read)
        or _protected_state_identity(opened) != _protected_state_identity(after_path)
    ):
        raise RuntimeError(f"Updater policy changed while reading: {path}")
    try:
        policy = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unreadable updater policy: {path}") from error
    if not isinstance(policy, dict):
        raise RuntimeError(f"Invalid updater policy: {path}")

    bool_fields = ("enabled", "require_signed_commits")
    for field in bool_fields:
        if field in policy and not isinstance(policy[field], bool):
            raise RuntimeError(f"Invalid updater policy field {field}: {path}")
    if "interval_hours" in policy and (
        isinstance(policy["interval_hours"], bool)
        or not isinstance(policy["interval_hours"], int)
        or policy["interval_hours"] < 1
    ):
        raise RuntimeError(f"Invalid updater policy field interval_hours: {path}")
    for field in ("repository", "claude_command"):
        if field in policy and not isinstance(policy[field], str):
            raise RuntimeError(f"Invalid updater policy field {field}: {path}")
    return policy, _protected_state_identity(after_path)


def _load_update_policy(path: Path) -> dict | None:
    policy, _identity = _read_update_policy(path)
    return policy


def _assert_update_policy_unchanged(
    path: Path, expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        if expected is None:
            return
        raise RuntimeError(f"Updater policy disappeared before removal: {path}")
    except OSError as error:
        raise RuntimeError(f"Updater policy cannot be rechecked: {path}") from error
    if expected is None:
        raise RuntimeError(f"Updater policy appeared before removal: {path}")
    _validate_update_policy_details(path, current)
    if _protected_state_identity(current) != expected:
        raise RuntimeError(f"Updater policy changed before removal: {path}")


def _remove_update_policy(
    path: Path, expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    _assert_update_policy_unchanged(path, expected)
    if expected is not None:
        path.unlink()


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    if first.st_ino and second.st_ino:
        return (
            first.st_dev,
            first.st_ino,
            first.st_ctime_ns,
            first.st_size,
            first.st_mtime_ns,
        ) == (
            second.st_dev,
            second.st_ino,
            second.st_ctime_ns,
            second.st_size,
            second.st_mtime_ns,
        )
    return (
        first.st_mode,
        first.st_size,
        first.st_mtime_ns,
    ) == (
        second.st_mode,
        second.st_size,
        second.st_mtime_ns,
    )


def _protected_state_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _validate_protected_state_details(
    path: Path, label: str, maximum_bytes: int, details: os.stat_result,
) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"Unsafe {label} file type: {path}")
    if details.st_size > maximum_bytes:
        raise RuntimeError(f"{label.capitalize()} exceeds size limit: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise RuntimeError(f"{label.capitalize()} permissions must be owner-only: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"{label.capitalize()} owner is invalid: {path}")


def _read_protected_state_json(
    path: Path, label: str, maximum_bytes: int,
) -> tuple[dict | None, tuple[int, int, int, int, int, int] | None]:
    """Read one bounded owner-only JSON state file without following links."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise RuntimeError(f"Unreadable {label}: {path}") from error
    _validate_protected_state_details(path, label, maximum_bytes, before)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _validate_protected_state_details(path, label, maximum_bytes, opened)
            if not _same_file_identity(before, opened):
                raise RuntimeError(f"{label.capitalize()} changed while opening: {path}")
            raw = handle.read(maximum_bytes + 1)
            after_read = os.fstat(handle.fileno())
        after_path = path.lstat()
    except (OSError, RuntimeError) as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"Unreadable {label}: {path}") from error
    if len(raw) > maximum_bytes:
        raise RuntimeError(f"{label.capitalize()} exceeds size limit: {path}")
    if (
        not _same_file_identity(opened, after_read)
        or not _same_file_identity(opened, after_path)
    ):
        raise RuntimeError(f"{label.capitalize()} changed while reading: {path}")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unreadable {label}: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid {label}: {path}")
    return value, _protected_state_identity(after_path)


def _load_protected_state_json(path: Path, label: str, maximum_bytes: int) -> dict | None:
    value, _identity = _read_protected_state_json(path, label, maximum_bytes)
    return value


def _assert_protected_state_unchanged(
    path: Path,
    label: str,
    maximum_bytes: int,
    expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        if expected is None:
            return
        raise RuntimeError(f"{label.capitalize()} disappeared before replacement: {path}")
    except OSError as error:
        raise RuntimeError(f"{label.capitalize()} cannot be rechecked: {path}") from error
    if expected is None:
        raise RuntimeError(f"{label.capitalize()} appeared before replacement: {path}")
    _validate_protected_state_details(path, label, maximum_bytes, current)
    if _protected_state_identity(current) != expected:
        raise RuntimeError(f"{label.capitalize()} changed before replacement: {path}")


def _validate_health_config(value: dict | None) -> dict | None:
    if value is None:
        return None
    for field in ("enabled", "plugin_required"):
        if field in value and not isinstance(value[field], bool):
            raise RuntimeError(f"Invalid health-monitor policy field {field}: {HEALTH_CONFIG}")
    if "interval_seconds" in value and (
        isinstance(value["interval_seconds"], bool)
        or not isinstance(value["interval_seconds"], int)
        or value["interval_seconds"] < 1
    ):
        raise RuntimeError(
            f"Invalid health-monitor policy field interval_seconds: {HEALTH_CONFIG}"
        )
    if "claude_command" in value and not isinstance(value["claude_command"], str):
        raise RuntimeError(
            f"Invalid health-monitor policy field claude_command: {HEALTH_CONFIG}"
        )
    return value


def _read_health_config(
) -> tuple[dict | None, tuple[int, int, int, int, int, int] | None]:
    value, identity = _read_protected_state_json(
        HEALTH_CONFIG, "health-monitor policy", MAX_HEALTH_FILE_BYTES
    )
    return _validate_health_config(value), identity


def _load_health_config() -> dict | None:
    value, _identity = _read_health_config()
    return value


def _load_delivery_policy() -> dict | None:
    value = _load_protected_state_json(
        DELIVERY_POLICY, "delivery policy", MAX_DELIVERY_POLICY_BYTES
    )
    if value is None:
        return None
    isolated = value.get("isolated_service")
    if (
        value.get("mandatory") is not True
        or value.get("fail_closed") is not True
        or value.get("direct_delivery_allowed") is not False
        or value.get("raw_streaming_allowed") is not False
        or value.get("on_guard_error") != "block"
        or not isinstance(isolated, dict)
        or isolated.get("required") is not True
    ):
        raise RuntimeError(f"Invalid delivery policy: {DELIVERY_POLICY}")
    for field in ("endpoint", "token_file", "audit_file"):
        candidate = isolated.get(field)
        if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 4096:
            raise RuntimeError(
                f"Invalid delivery policy field isolated_service.{field}: {DELIVERY_POLICY}"
            )
    return value


def _validate_health_state(value: dict | None) -> dict | None:
    if value is None:
        return None
    integer_fields = ("checked_at", "consecutive_failures", "last_repair_at", "next_repair_at")
    for field in integer_fields:
        if field in value and (
            isinstance(value[field], bool)
            or not isinstance(value[field], int)
            or value[field] < 0
        ):
            raise RuntimeError(f"Invalid health-monitor state field {field}: {HEALTH_STATE}")
    for field in ("guard_healthy", "mcp_healthy", "plugin_required", "plugin_cache_healthy"):
        if field in value and not isinstance(value[field], bool):
            raise RuntimeError(f"Invalid health-monitor state field {field}: {HEALTH_STATE}")
    for field in ("status", "reason", "plugin_cache_version", "plugin_cache_reason"):
        if field in value and not isinstance(value[field], str):
            raise RuntimeError(f"Invalid health-monitor state field {field}: {HEALTH_STATE}")
    repairs = value.get("repairs")
    if repairs is not None and (
        not isinstance(repairs, list)
        or len(repairs) > 32
        or any(not isinstance(item, str) or len(item) > 128 for item in repairs)
    ):
        raise RuntimeError(f"Invalid health-monitor state field repairs: {HEALTH_STATE}")
    return value


def _read_health_state(
) -> tuple[dict | None, tuple[int, int, int, int, int, int] | None]:
    value, identity = _read_protected_state_json(
        HEALTH_STATE, "health-monitor state", MAX_HEALTH_FILE_BYTES
    )
    return _validate_health_state(value), identity


def _load_health_state() -> dict | None:
    value, _identity = _read_health_state()
    return value


def _validate_update_state(value: dict | None) -> dict | None:
    if value is None:
        return None
    for field in ("status", "runtime_version", "candidate_version", "paused_update_policy"):
        if field in value and not isinstance(value[field], str):
            raise RuntimeError(f"Invalid updater state field {field}: {UPDATE_STATE}")
    for field in ("revision", "previous", "candidate_revision", "rolled_back_from"):
        if field in value and (
            not isinstance(value[field], str)
            or re.fullmatch(r"[0-9a-f]{40}", value[field]) is None
        ):
            raise RuntimeError(f"Invalid updater state field {field}: {UPDATE_STATE}")
    if "checked_at" in value and (
        isinstance(value["checked_at"], bool)
        or not isinstance(value["checked_at"], int)
        or value["checked_at"] < 0
    ):
        raise RuntimeError(f"Invalid updater state field checked_at: {UPDATE_STATE}")
    for field in ("auto_update_paused", "runtime_unchanged"):
        if field in value and not isinstance(value[field], bool):
            raise RuntimeError(f"Invalid updater state field {field}: {UPDATE_STATE}")
    for field in ("claude_plugin", "health_monitor"):
        if field in value and not isinstance(value[field], dict):
            raise RuntimeError(f"Invalid updater state field {field}: {UPDATE_STATE}")
    return value


def _read_update_state(
) -> tuple[dict | None, tuple[int, int, int, int, int, int] | None]:
    value, identity = _read_protected_state_json(
        UPDATE_STATE, "updater state", MAX_UPDATE_STATE_BYTES
    )
    return _validate_update_state(value), identity


def _load_update_state() -> dict | None:
    value, _identity = _read_update_state()
    return value


def _write_update_state(
    value: dict,
    expected_identity: tuple[int, int, int, int, int, int] | None,
) -> None:
    _atomic_json(
        UPDATE_STATE,
        value,
        before_replace=lambda: _assert_protected_state_unchanged(
            UPDATE_STATE,
            "updater state",
            MAX_UPDATE_STATE_BYTES,
            expected_identity,
        ),
    )


def _windows_process_is_alive(pid: int) -> bool:
    """Query a Windows process handle without sending it a signal."""
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # Access denied still proves that the process exists.
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # Ambiguous inspection must preserve the existing lock.
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_start_identity(pid: int) -> str | None:
    """Return the immutable Windows creation time without signalling the process."""
    import ctypes

    class FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    )
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return f"windows:{creation.high:08x}{creation.low:08x}"
    finally:
        kernel32.CloseHandle(handle)


def _linux_process_start_identity(pid: int) -> str | None:
    """Bind a Linux PID to both its boot and kernel start tick."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
        process_stat = Path("/proc/self/stat") if pid == os.getpid() else Path(f"/proc/{pid}/stat")
        raw = process_stat.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id) is None or len(raw) > 4096:
        return None
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields = raw[closing_parenthesis + 1:].split()
    if len(fields) <= 19 or not fields[19].isdigit():
        return None
    return f"linux:{boot_id}:{fields[19]}"


def _posix_process_start_identity(pid: int) -> str | None:
    """Hash the stable process start timestamp exposed by portable ps."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            env={**os.environ, "LC_ALL": "C"},
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(result.stdout.split())
    if result.returncode or not started or len(started) > 256:
        return None
    return "posix:" + hashlib.sha256(started.encode("utf-8")).hexdigest()


def _process_start_identity(pid: int) -> str | None:
    """Return a process-generation identifier, or None when inspection is ambiguous."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_identity(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start_identity(pid)
    return _posix_process_start_identity(pid)


@contextlib.contextmanager
def _open_operation_lock_directory():
    """Hold the owner-controlled lock directory for one complete lock transition."""
    if os.name == "nt":
        OPERATION_LOCK.parent.mkdir(parents=True, exist_ok=True)
        yield None
        return
    home = Path.home()
    try:
        OPERATION_LOCK.parent.relative_to(home)
    except ValueError:
        # Tests and embedders may place the lock outside the account home. The
        # final parent still receives the same no-follow and identity checks.
        home = OPERATION_LOCK.parent
    with _open_service_definition_directory(OPERATION_LOCK.parent, home) as directory:
        yield directory


def _operation_lock_lstat(directory: int | None) -> os.stat_result:
    if directory is None:
        return OPERATION_LOCK.lstat()
    return os.stat(OPERATION_LOCK.name, dir_fd=directory, follow_symlinks=False)


def _open_operation_lock_file(directory: int | None, flags: int, mode: int | None = None) -> int:
    path: str | Path = OPERATION_LOCK if directory is None else OPERATION_LOCK.name
    kwargs = {} if directory is None else {"dir_fd": directory}
    if mode is None:
        return os.open(path, flags, **kwargs)
    return os.open(path, flags, mode, **kwargs)


def _unlink_operation_lock(directory: int | None) -> None:
    if directory is None:
        OPERATION_LOCK.unlink()
        return
    os.unlink(OPERATION_LOCK.name, dir_fd=directory)


def _read_operation_lock(
    metadata: os.stat_result, *, directory: int | None = None,
) -> dict | None:
    """Read the exact bounded lock instance described by metadata."""
    if not _operation_lock_details_safe(metadata):
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = _open_operation_lock_file(directory, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _operation_lock_details_safe(opened) or not _same_file_identity(metadata, opened):
                return None
            raw = handle.read(MAX_OPERATION_LOCK_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        after_path = _operation_lock_lstat(directory)
    except OSError:
        return None
    if (
        len(raw) > MAX_OPERATION_LOCK_BYTES
        or not _operation_lock_details_safe(after_read)
        or not _operation_lock_details_safe(after_path)
        or not _same_file_identity(opened, after_read)
        or not _same_file_identity(opened, after_path)
    ):
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _operation_lock_details_safe(details: os.stat_result) -> bool:
    """Accept only one owner-controlled regular maintenance-lock inode."""
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        return False
    if details.st_nlink != 1 or details.st_size > MAX_OPERATION_LOCK_BYTES:
        return False
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        return False
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        return False
    return True


def _operation_lock_owner_alive(
    metadata: os.stat_result, *, directory: int | None = None,
) -> bool | None:
    """Return the validated owner's liveness, or None for an untrusted lock body."""
    value = _read_operation_lock(metadata, directory=directory)
    if value is None:
        return None
    pid = value.get("pid")
    operation = value.get("operation")
    started_at = value.get("started_at")
    token = value.get("token")
    process_start_id = value.get("process_start_id")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(operation, str)
        or not operation
        or isinstance(started_at, bool)
        or not isinstance(started_at, int)
        or not isinstance(token, str)
        or re.fullmatch(r"[0-9a-f]{32}", token) is None
    ):
        return None
    alive = _process_is_alive(pid)
    if not alive or process_start_id is None:
        return alive
    if (
        not isinstance(process_start_id, str)
        or re.fullmatch(r"(?:linux|windows|posix):[0-9a-f:-]{1,128}", process_start_id) is None
    ):
        return None
    current_start_id = _process_start_identity(pid)
    if current_start_id is None:
        return True  # Ambiguous inspection must preserve the existing lock.
    return current_start_id == process_start_id


def _acquire_operation_lock(operation: str, *, now: int | None = None) -> str | None:
    """Take a same-user cross-platform lock without racing a living owner."""
    try:
        with _open_operation_lock_directory() as directory:
            return _acquire_operation_lock_at(directory, operation, now=now)
    except (OSError, RuntimeError):
        return None


def _acquire_operation_lock_at(
    directory: int | None, operation: str, *, now: int | None = None,
) -> str | None:
    timestamp = int(time.time()) if now is None else now
    token = os.urandom(16).hex()
    for attempt in range(2):
        try:
            descriptor = _open_operation_lock_file(
                directory,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            try:
                metadata = _operation_lock_lstat(directory)
                if not _operation_lock_details_safe(metadata):
                    return None
                stale = timestamp - int(metadata.st_mtime) > OPERATION_LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if not stale or attempt:
                return None
            if _operation_lock_owner_alive(metadata, directory=directory) is True:
                return None
            try:
                current = _operation_lock_lstat(directory)
                if (
                    not _operation_lock_details_safe(current)
                    or not _same_file_identity(metadata, current)
                ):
                    return None
                _unlink_operation_lock(directory)
            except OSError:
                return None
            continue
        lock_value = {"operation": operation, "pid": os.getpid(), "started_at": timestamp, "token": token}
        process_start_id = _process_start_identity(os.getpid())
        if process_start_id is not None:
            lock_value["process_start_id"] = process_start_id
        created = os.fstat(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(lock_value, handle)
            handle.write("\n")
            handle.flush()
            written = os.fstat(handle.fileno())
        try:
            installed = _operation_lock_lstat(directory)
        except OSError:
            return None
        if (
            not _operation_lock_details_safe(created)
            or not _operation_lock_details_safe(written)
            or not _operation_lock_details_safe(installed)
            or (created.st_dev, created.st_ino) != (written.st_dev, written.st_ino)
            or not _same_file_identity(written, installed)
        ):
            return None
        return token
    return None


def _release_operation_lock(token: str) -> None:
    """Release only the lock instance acquired by this process."""
    try:
        with _open_operation_lock_directory() as directory:
            _release_operation_lock_at(directory, token)
    except (OSError, RuntimeError):
        return


def _release_operation_lock_at(directory: int | None, token: str) -> None:
    try:
        metadata = _operation_lock_lstat(directory)
    except OSError:
        return
    if not _operation_lock_details_safe(metadata):
        return
    value = _read_operation_lock(metadata, directory=directory)
    if value is None or value.get("token") != token:
        return
    try:
        current = _operation_lock_lstat(directory)
        if _operation_lock_details_safe(current) and _same_file_identity(metadata, current):
            _unlink_operation_lock(directory)
    except OSError:
        return


def _effective_signed_commit_policy(requested: bool = False) -> bool:
    """Keep an enabled signature requirement monotonic across every entry point."""
    required = requested
    for path in (UPDATE_CONFIG, UPDATE_PAUSED_CONFIG):
        policy = _load_update_policy(path)
        if policy is None:
            continue
        saved = policy.get("require_signed_commits", False)
        required = required or saved
    return required


def _clean_checkout_revision(root: Path) -> str | None:
    """Return the exact HEAD only when every tracked and untracked path is clean."""
    head = _run(["git", "rev-parse", "--verify", "HEAD^{commit}"], root)
    revision = head.stdout.strip()
    if head.returncode or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        return None
    status_result = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        root,
    )
    if status_result.returncode or status_result.stdout.strip():
        return None
    return revision


def _update_artifact_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    """Bind rollback cleanup to the exact artifact created by this update."""
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _capture_update_artifact(
    path: Path,
) -> tuple[int, int, int, int, int, int, int]:
    try:
        details = path.lstat()
    except OSError as error:
        raise RuntimeError(f"Updated runtime artifact cannot be inspected: {path}") from error
    return _update_artifact_identity(details)


def _assert_update_artifact_unchanged(
    path: Path,
    expected: tuple[int, int, int, int, int, int, int],
    *,
    missing_ok: bool = False,
) -> bool:
    try:
        current = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise RuntimeError(f"Updated runtime artifact disappeared before rollback: {path}")
    except OSError as error:
        raise RuntimeError(f"Updated runtime artifact cannot be rechecked: {path}") from error
    if _update_artifact_identity(current) != expected:
        raise RuntimeError(f"Updated runtime artifact changed before rollback: {path}")
    return True


def _remove_created_update_artifact(
    path: Path,
    expected: tuple[int, int, int, int, int, int, int],
) -> None:
    if not _assert_update_artifact_unchanged(path, expected, missing_ok=True):
        return
    _assert_update_artifact_unchanged(path, expected)
    path.unlink()


def _restore_updated_claude_config(
    path: Path,
    payload: bytes,
    expected: tuple[int, int, int, int, int, int, int],
) -> None:
    _atomic_bytes(
        path,
        payload,
        before_replace=lambda: _assert_update_artifact_unchanged(path, expected),
    )


def _rollback_activated_update(
    root: Path,
    previous: str,
    activated: str,
) -> subprocess.CompletedProcess[str]:
    """Roll back only the exact, still-clean revision activated by this update."""
    command = ["git", "reset", "--keep", previous]
    if _clean_checkout_revision(root) != activated:
        observed = _run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], root
        ).stdout.strip()
        detail = (
            "HEAD changed independently before runtime rollback"
            if observed != activated
            else "the checkout changed before runtime rollback"
        )
        return subprocess.CompletedProcess(command, 1, "", detail)
    rollback = _run(command, root)
    if rollback.returncode == 0 and _clean_checkout_revision(root) == previous:
        return rollback
    return subprocess.CompletedProcess(
        rollback.args,
        1,
        rollback.stdout,
        ((rollback.stderr or "") + "\nruntime rollback did not restore the exact clean revision").strip(),
    )


def update(require_signed_commits: bool = False, claude_command: str | None = None) -> int:
    root = repository_root()
    if not (root / ".git").exists():
        print("Update requires a Git checkout; reinstall from the latest release.", file=sys.stderr)
        return 2
    try:
        signed_required = _effective_signed_commit_policy(require_signed_commits)
    except RuntimeError:
        print("Updater signature policy is unreadable; update is blocked fail-closed.", file=sys.stderr)
        return 2
    token = _acquire_operation_lock("update")
    if token is None:
        print("Another guard maintenance operation is active; update skipped safely.", file=sys.stderr)
        return 3
    try:
        return _update_unlocked(signed_required, claude_command)
    finally:
        _release_operation_lock(token)


def _update_unlocked(require_signed_commits: bool = False, claude_command: str | None = None) -> int:
    root = repository_root()
    previous = _clean_checkout_revision(root)
    if previous is None:
        print(
            "Update requires a valid, completely clean checkout; local files are unchanged.",
            file=sys.stderr,
        )
        return 2
    try:
        _stored_update_state, update_state_identity = _read_update_state()
    except RuntimeError as error:
        print(
            f"Updater state is unsafe; update is blocked before candidate execution: {error}",
            file=sys.stderr,
        )
        return 2

    def persist_update_state(value: dict) -> bool:
        try:
            _write_update_state(value, update_state_identity)
        except (OSError, RuntimeError) as error:
            print(
                f"Updater state changed before publication; the replacement was preserved: {error}",
                file=sys.stderr,
            )
            return False
        return True

    def update_state_unchanged(stage: str) -> bool:
        try:
            _assert_protected_state_unchanged(
                UPDATE_STATE,
                "updater state",
                MAX_UPDATE_STATE_BYTES,
                update_state_identity,
            )
        except RuntimeError as error:
            print(
                f"Updater state changed {stage}; activation is blocked: {error}",
                file=sys.stderr,
            )
            return False
        return True
    claude_installed = TARGETS["claude"].is_symlink()
    try:
        stored_monitor_config, monitor_config_identity = _read_health_config()
        initial_monitor_config = dict(stored_monitor_config or {})
        _stored_health_state, health_state_identity = _read_health_state()
    except RuntimeError as error:
        print(
            f"Health-monitor protected state is unsafe; update is blocked before candidate execution: {error}",
            file=sys.stderr,
        )
        return 2

    def health_state_unchanged(stage: str) -> bool:
        try:
            _assert_protected_state_unchanged(
                HEALTH_CONFIG,
                "health-monitor policy",
                MAX_HEALTH_FILE_BYTES,
                monitor_config_identity,
            )
            _assert_protected_state_unchanged(
                HEALTH_STATE,
                "health-monitor state",
                MAX_HEALTH_FILE_BYTES,
                health_state_identity,
            )
        except (OSError, RuntimeError) as error:
            print(
                f"Health-monitor protected state changed {stage}; stale maintenance is blocked: {error}",
                file=sys.stderr,
            )
            return False
        return True

    def persist_health_config(value: dict) -> bool:
        nonlocal monitor_config_identity
        try:
            _atomic_json(
                HEALTH_CONFIG,
                value,
                before_replace=lambda: (
                    _assert_protected_state_unchanged(
                        HEALTH_CONFIG,
                        "health-monitor policy",
                        MAX_HEALTH_FILE_BYTES,
                        monitor_config_identity,
                    ),
                    _assert_protected_state_unchanged(
                        HEALTH_STATE,
                        "health-monitor state",
                        MAX_HEALTH_FILE_BYTES,
                        health_state_identity,
                    ),
                ),
            )
            stored_config, monitor_config_identity = _read_health_config()
            if stored_config != value:
                raise RuntimeError(
                    "Health-monitor policy changed after updater replacement"
                )
        except (OSError, RuntimeError) as error:
            print(
                f"Health-monitor policy changed before updater publication; the replacement was preserved: {error}",
                file=sys.stderr,
            )
            return False
        return True

    def persist_health_state(value: dict) -> bool:
        nonlocal health_state_identity
        try:
            _atomic_json(
                HEALTH_STATE,
                value,
                before_replace=lambda: (
                    _assert_protected_state_unchanged(
                        HEALTH_CONFIG,
                        "health-monitor policy",
                        MAX_HEALTH_FILE_BYTES,
                        monitor_config_identity,
                    ),
                    _assert_protected_state_unchanged(
                        HEALTH_STATE,
                        "health-monitor state",
                        MAX_HEALTH_FILE_BYTES,
                        health_state_identity,
                    ),
                ),
            )
            stored_state, health_state_identity = _read_health_state()
            if stored_state != value:
                raise RuntimeError(
                    "Health-monitor state changed after updater replacement"
                )
        except (OSError, RuntimeError) as error:
            print(
                f"Health-monitor state changed before updater publication; the replacement was preserved: {error}",
                file=sys.stderr,
            )
            return False
        return True

    def remove_monitor_after_opt_out() -> None:
        try:
            replacement, _identity = _read_health_config()
            if replacement is not None and replacement.get("enabled") is False:
                remove_health_monitor()
        except (OSError, RuntimeError) as error:
            print(
                f"Health-monitor scheduler cleanup was blocked by unsafe replacement policy: {error}",
                file=sys.stderr,
            )

    monitor_enabled = initial_monitor_config.get("enabled") is not False
    claude_preflight: dict | None = None
    with tempfile.TemporaryDirectory(prefix="blun-language-guard-") as directory:
        candidate = Path(directory) / "repo"
        clone = _run(["git", "clone", "--depth", "1", REPO_URL, str(candidate)])
        if clone.returncode:
            print(clone.stderr, file=sys.stderr)
            return 1
        revision = _run(["git", "rev-parse", "HEAD"], candidate).stdout.strip()
        if require_signed_commits:
            verified = _run(["git", "verify-commit", revision], candidate)
            if verified.returncode:
                print("Candidate update is not signed by a trusted Git identity; current installation is unchanged.", file=sys.stderr)
                return 1
        tests = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], candidate)
        if tests.returncode:
            print("Candidate update failed tests; current installation is unchanged.", file=sys.stderr)
            return 1
        try:
            expected_version = (candidate / "VERSION").read_text(encoding="utf-8-sig").strip()
        except OSError:
            print("Candidate update has no readable VERSION; current installation is unchanged.", file=sys.stderr)
            return 1
        if claude_installed:
            claude_preflight = preflight_claude_plugin_update(
                expected_version,
                claude_command,
                candidate,
            )
            if claude_preflight.get("ready") is not True:
                if not persist_update_state({
                    "status": "degraded",
                    "revision": previous,
                    "previous": previous,
                    "candidate_revision": revision,
                    "checked_at": int(time.time()),
                    "runtime_version": (root / "VERSION").read_text(encoding="utf-8-sig").strip(),
                    "candidate_version": expected_version,
                    "claude_plugin": claude_preflight,
                    "runtime_unchanged": True,
                }):
                    return 2
                print(
                    "Claude plugin preflight failed; current repository and runtimes are unchanged. "
                    "The updater remains degraded and will retry safely.",
                    file=sys.stderr,
                )
                return 1
    if _clean_checkout_revision(root) != previous:
        print(
            "The active checkout changed during update preflight; candidate activation is blocked.",
            file=sys.stderr,
        )
        return 2
    if not update_state_unchanged("during candidate preflight"):
        return 2
    fetch = _run(["git", "fetch", "origin", revision], root)
    if fetch.returncode:
        print(fetch.stderr, file=sys.stderr)
        return 1
    if _clean_checkout_revision(root) != previous:
        print(
            "The active checkout changed while fetching the tested update; candidate activation is blocked.",
            file=sys.stderr,
        )
        return 2
    if not update_state_unchanged("while fetching the tested update"):
        return 2
    merge = _run(["git", "merge", "--ff-only", revision], root)
    if merge.returncode:
        print(merge.stderr, file=sys.stderr)
        return 1
    if _clean_checkout_revision(root) != revision:
        current = _run(["git", "rev-parse", "--verify", "HEAD^{commit}"], root).stdout.strip()
        if current == revision:
            rollback = _run(["git", "reset", "--keep", previous], root)
            restored_head = _run(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"], root
            ).stdout.strip()
            restored = rollback.returncode == 0 and restored_head == previous
            outcome = (
                "the tested revision was rolled back without discarding local work."
                if restored
                else "the safe repository rollback failed. Manual inspection is required."
            )
            print(
                "The active checkout changed during update cutover; runtime activation is blocked and "
                + outcome,
                file=sys.stderr,
            )
            return 2 if restored else 1
        print(
            "HEAD changed independently during update cutover; runtime activation is blocked and the "
            "independent commit was not reset.",
            file=sys.stderr,
        )
        return 2
    post_tests = _run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
        root,
    )
    post_revision = _clean_checkout_revision(root)
    if post_tests.returncode or post_revision != revision:
        observed = _run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], root
        ).stdout.strip()
        if observed == revision:
            rollback = _run(["git", "reset", "--keep", previous], root)
            restored_head = _run(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"], root
            ).stdout.strip()
            restored = rollback.returncode == 0 and restored_head == previous
            outcome = (
                "the previous revision was restored without discarding local work."
                if restored
                else "the safe repository rollback failed. Manual inspection is required."
            )
            if post_tests.returncode:
                print(
                    "Installed revision failed its post-update check; runtime activation is blocked and "
                    + outcome,
                    file=sys.stderr,
                )
                return 1
            print(
                "The active checkout changed while running post-update tests; runtime activation is "
                "blocked and " + outcome,
                file=sys.stderr,
            )
            return 2 if restored else 1
        detail = (
            "Installed revision failed its post-update check"
            if post_tests.returncode
            else "HEAD changed independently while running post-update tests"
        )
        print(
            detail
            + "; runtime activation is blocked and the independent commit was not reset.",
            file=sys.stderr,
        )
        return 1 if post_tests.returncode else 2
    if not update_state_unchanged("while running post-update tests"):
        rollback = _rollback_activated_update(root, previous, revision)
        return 2 if rollback.returncode == 0 else 1
    mcp_runtime_preexisting = MCP_HTTP_COMMAND.exists() or MCP_HTTP_COMMAND.is_symlink()
    mcp_headers_preexisting = MCP_HEADERS_COMMAND.exists() or MCP_HEADERS_COMMAND.is_symlink()
    mcp_token_preexisting = MCP_HTTP_TOKEN.exists() or MCP_HTTP_TOKEN.is_symlink()
    try:
        _claude_config, preflight_claude_config, _claude_config_identity_before = (
            _read_protected_claude_config(CLAUDE_CONFIG)
            if claude_installed
            else ({}, None, None)
        )
    except RuntimeError as error:
        rollback = _rollback_activated_update(root, previous, revision)
        restored = rollback.returncode == 0
        print(
            f"Claude configuration is unsafe; runtime activation is blocked ({error}) and repository rollback "
            + ("succeeded." if restored else "FAILED."),
            file=sys.stderr,
        )
        return 2 if restored else 1
    claude_config_preexisting = preflight_claude_config is not None
    claude_config_bytes: bytes | None = None
    installed_artifacts: dict[Path, tuple[int, int, int, int, int, int, int]] = {}
    created_artifacts: set[Path] = set()
    configured_claude_identity: tuple[int, int, int, int, int, int, int] | None = None

    def rollback_runtime() -> subprocess.CompletedProcess[str]:
        rollback = _rollback_activated_update(root, previous, revision)
        if rollback.returncode != 0:
            print(
                f"Runtime repository rollback blocked fail-closed: {rollback.stderr}",
                file=sys.stderr,
            )
            return rollback
        cleanup_error: OSError | RuntimeError | None = None
        if claude_installed:
            try:
                for artifact, identity in installed_artifacts.items():
                    _assert_update_artifact_unchanged(
                        artifact,
                        identity,
                        missing_ok=artifact in created_artifacts,
                    )
                if configured_claude_identity is not None:
                    _assert_update_artifact_unchanged(
                        CLAUDE_CONFIG,
                        configured_claude_identity,
                        missing_ok=not claude_config_preexisting,
                    )
                if MCP_HTTP_COMMAND in created_artifacts:
                    remove_mcp_http_autostart()
                for artifact in created_artifacts:
                    _remove_created_update_artifact(
                        artifact,
                        installed_artifacts[artifact],
                    )
                if configured_claude_identity is not None:
                    if claude_config_preexisting:
                        if claude_config_bytes is None:
                            raise RuntimeError(
                                "Claude configuration rollback backup is unavailable"
                            )
                        _restore_updated_claude_config(
                            CLAUDE_CONFIG,
                            claude_config_bytes,
                            configured_claude_identity,
                        )
                    else:
                        _remove_created_update_artifact(
                            CLAUDE_CONFIG,
                            configured_claude_identity,
                        )
            except (OSError, RuntimeError) as error:
                cleanup_error = error
        restart_guard_runtime()
        if mcp_runtime_preexisting and cleanup_error is None:
            restart_mcp_http_runtime()
        if cleanup_error is not None:
            print(
                f"Runtime rollback cleanup blocked fail-closed: {cleanup_error}",
                file=sys.stderr,
            )
            return subprocess.CompletedProcess(
                rollback.args,
                1,
                rollback.stdout,
                ((rollback.stderr or "") + f"\n{cleanup_error}").strip(),
            )
        return rollback

    if claude_installed:
        try:
            install_mcp_http_runtime(root)
            installed_artifacts[MCP_HTTP_COMMAND] = _capture_update_artifact(MCP_HTTP_COMMAND)
            installed_artifacts[MCP_HEADERS_COMMAND] = _capture_update_artifact(MCP_HEADERS_COMMAND)
            installed_artifacts[MCP_HTTP_TOKEN] = _capture_update_artifact(MCP_HTTP_TOKEN)
            if not mcp_runtime_preexisting:
                created_artifacts.add(MCP_HTTP_COMMAND)
            if not mcp_headers_preexisting:
                created_artifacts.add(MCP_HEADERS_COMMAND)
            if not mcp_token_preexisting:
                created_artifacts.add(MCP_HTTP_TOKEN)
            installed, detail = install_mcp_http_autostart(root)
            if not installed:
                raise RuntimeError(f"persistent MCP autostart failed: {detail}")
            claude_backup, _removed_shadows = configure_claude_mcp()
            configured_claude_identity = _capture_update_artifact(CLAUDE_CONFIG)
            if claude_backup is None:
                claude_config_preexisting = False
                claude_config_bytes = b""
            else:
                _backup_config, claude_config_bytes, _backup_identity = (
                    _read_protected_claude_config(claude_backup)
                )
                if claude_config_bytes is None:
                    raise RuntimeError("Claude configuration rollback backup disappeared")
                claude_config_preexisting = True
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            rollback = rollback_runtime()
            print(
                f"Claude persistent MCP activation failed ({error}); rollback "
                + ("succeeded." if rollback.returncode == 0 else "FAILED."),
                file=sys.stderr,
            )
            return 1
    if SERVICE_COMMAND.is_symlink() or SERVICE_COMMAND.exists():
        restarted, runtime = restart_guard_runtime()
        healthy = False
        if restarted:
            for _attempt in range(10):
                try:
                    health = probe_guard_service()
                    healthy = health.get("status") == "ok" and health.get("isolated_key") is True
                    if healthy:
                        break
                except (OSError, RuntimeError, ValueError):
                    pass
                time.sleep(0.2)
        if not restarted or not healthy:
            rollback = rollback_runtime()
            print(
                "Updated guard could not restart; rollback "
                + ("succeeded." if rollback.returncode == 0 else "FAILED."),
                file=sys.stderr,
            )
            return 1
        print(f"Restarted isolated guard through {runtime}.")
    if MCP_HTTP_COMMAND.is_symlink() or MCP_HTTP_COMMAND.exists():
        restarted, runtime = restart_mcp_http_runtime()
        healthy = False
        if restarted:
            for _attempt in range(15):
                try:
                    probe_mcp_http()
                    healthy = True
                    break
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                    time.sleep(0.2)
        if not restarted or not healthy:
            rollback = rollback_runtime()
            print(
                "Updated persistent MCP could not restart; rollback "
                + ("succeeded." if rollback.returncode == 0 else "FAILED."),
                file=sys.stderr,
            )
            return 1
        print(f"Restarted persistent MCP through {runtime}.")
    monitor_expected = claude_installed and monitor_enabled
    monitor_install = {
        "attempted": False,
        "installed": not monitor_expected,
        "detail": "explicitly-disabled" if claude_installed else "claude-skill-not-installed",
    }
    health_transition_blocked = False
    if monitor_expected:
        if not health_state_unchanged("before scheduler activation"):
            monitor_ok = False
            monitor_detail = "protected-health-state-changed"
            health_transition_blocked = True
        else:
            monitor_ok, monitor_detail = install_health_monitor()
            guard_now, mcp_now = _guard_stack_status(timeout=4.0)
            monitor_ok = monitor_ok and guard_now and mcp_now
            if monitor_ok:
                monitor_config = dict(initial_monitor_config)
                monitor_config.update({"enabled": True, "interval_seconds": 60})
                configured_claude = claude_command or _configured_claude_command(monitor_config)
                if configured_claude:
                    monitor_config["claude_command"] = configured_claude
                monitor_ok = persist_health_config(monitor_config)
                if monitor_ok:
                    monitor_ok = persist_health_state({
                        "status": "ok",
                        "checked_at": int(time.time()),
                        "guard_healthy": True,
                        "mcp_healthy": True,
                        "consecutive_failures": 0,
                        "last_repair_at": 0,
                        "next_repair_at": 0,
                        "repairs": [],
                    })
                if not monitor_ok:
                    monitor_detail = "protected-health-state-changed"
                    health_transition_blocked = True
                    remove_monitor_after_opt_out()
        monitor_install = {"attempted": True, "installed": monitor_ok, "detail": monitor_detail}
    expected_version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    plugin_update = _apply_claude_plugin_update(
        expected_version,
        claude_command,
        claude_preflight or {},
    ) if claude_installed and not health_transition_blocked else {
        "attempted": False,
        "updated": False,
        "status": {
            "reason": (
                "protected-health-state-changed"
                if health_transition_blocked
                else "claude-skill-not-installed"
            )
        },
    }
    if monitor_expected and plugin_update.get("status", {}).get("installed"):
        monitor_config = dict(initial_monitor_config)
        monitor_config.update({
            "enabled": True,
            "interval_seconds": 60,
            "plugin_required": True,
        })
        configured_claude = claude_command or _configured_claude_command(monitor_config)
        if configured_claude:
            monitor_config["claude_command"] = configured_claude
        if not persist_health_config(monitor_config):
            monitor_install = {
                "attempted": True,
                "installed": False,
                "detail": "protected-health-state-changed",
            }
            health_transition_blocked = True
            remove_monitor_after_opt_out()
    plugin_reason = plugin_update.get("status", {}).get("reason")
    plugin_failed = claude_installed and not plugin_update.get("updated") and plugin_reason != "plugin-not-installed"
    monitor_failed = monitor_expected and not monitor_install["installed"]
    if plugin_failed or monitor_failed:
        print(
            "Repository, guard service, and MCP updated successfully, but Claude maintenance did not "
            "reach a healthy synchronized state. The guard remains fail-closed; rerun the updater after "
            "repairing the reported plugin or health-monitor adapter.",
            file=sys.stderr,
        )
        if not persist_update_state({
            "status": "degraded",
            "revision": revision,
            "previous": previous,
            "checked_at": int(time.time()),
            "runtime_version": expected_version,
            "claude_plugin": plugin_update,
            "health_monitor": monitor_install,
        }):
            return 2
        return 1
    if not persist_update_state({
        "status": "ok",
        "revision": revision,
        "previous": previous,
        "checked_at": int(time.time()),
        "runtime_version": expected_version,
        "claude_plugin": plugin_update,
        "health_monitor": monitor_install,
    }):
        return 2
    print(f"Updated to tested revision {revision}; rollback revision is {previous}")
    if plugin_update.get("reload_required"):
        print("Claude plugin cache updated. Existing sessions still use their loaded hooks; run /reload-plugins or start a new session.")
    return 0


def _restart_installed_runtimes() -> tuple[bool, str]:
    """Restart and probe only runtimes that were installed before maintenance."""
    restarted_names: list[str] = []
    if SERVICE_COMMAND.exists() or SERVICE_COMMAND.is_symlink():
        restarted, runtime = restart_guard_runtime()
        healthy = False
        if restarted:
            for _attempt in range(10):
                try:
                    health = probe_guard_service()
                    healthy = health.get("status") == "ok" and health.get("isolated_key") is True
                    if healthy:
                        break
                except (OSError, RuntimeError, ValueError):
                    pass
                time.sleep(0.2)
        if not restarted or not healthy:
            return False, f"isolated guard failed through {runtime}"
        restarted_names.append("isolated guard")
    if MCP_HTTP_COMMAND.exists() or MCP_HTTP_COMMAND.is_symlink():
        restarted, runtime = restart_mcp_http_runtime()
        healthy = False
        if restarted:
            for _attempt in range(15):
                try:
                    probe_mcp_http()
                    healthy = True
                    break
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                    time.sleep(0.2)
        if not restarted or not healthy:
            return False, f"persistent MCP failed through {runtime}"
        restarted_names.append("persistent MCP")
    return True, ", ".join(restarted_names) if restarted_names else "no installed runtime"


def rollback(require_signed_commits: bool = False, claude_command: str | None = None) -> int:
    """Return to the updater-recorded previous revision without mixing runtime versions."""
    root = repository_root()
    if not (root / ".git").exists():
        print("Rollback requires a Git checkout.", file=sys.stderr)
        return 2
    token = _acquire_operation_lock("rollback")
    if token is None:
        print("Another guard maintenance operation is active; rollback skipped safely.", file=sys.stderr)
        return 3
    try:
        return _rollback_unlocked(require_signed_commits, claude_command)
    finally:
        _release_operation_lock(token)


def _rollback_unlocked(require_signed_commits: bool = False, claude_command: str | None = None) -> int:
    root = repository_root()
    try:
        state, update_state_identity = _read_update_state()
    except RuntimeError:
        print("Updater state is unsafe; rollback is blocked fail-closed.", file=sys.stderr)
        return 2
    if state is None:
        print("No valid updater state is available; refusing to guess a rollback target.", file=sys.stderr)
        return 2
    current = _clean_checkout_revision(root)
    if current is None:
        print(
            "Rollback requires a valid, completely clean checkout; current files are unchanged.",
            file=sys.stderr,
        )
        return 2
    try:
        rollback_monitor_config = _health_monitor_config()
        _load_health_state()
    except RuntimeError as error:
        print(
            f"Health-monitor protected state is unsafe; rollback is blocked before candidate execution: {error}",
            file=sys.stderr,
        )
        return 2
    recorded_current = state.get("revision")
    target = state.get("previous")
    sha = re.compile(r"[0-9a-f]{40}")
    if (
        state.get("status") not in {"ok", "degraded"}
        or not isinstance(recorded_current, str)
        or not isinstance(target, str)
        or sha.fullmatch(recorded_current) is None
        or sha.fullmatch(target) is None
        or current != recorded_current
        or target == current
    ):
        print("Updater state is stale or incomplete; refusing to guess a rollback target.", file=sys.stderr)
        return 2
    if _run(["git", "cat-file", "-e", f"{target}^{{commit}}"], root).returncode:
        print("Recorded rollback commit is unavailable; current files are unchanged.", file=sys.stderr)
        return 2
    if _run(["git", "merge-base", "--is-ancestor", target, current], root).returncode:
        print("Recorded rollback target is not an ancestor; current files are unchanged.", file=sys.stderr)
        return 2
    try:
        active_policy, active_policy_identity = _read_update_policy(UPDATE_CONFIG)
        paused_policy, paused_policy_identity = _read_update_policy(UPDATE_PAUSED_CONFIG)
        signed_required = require_signed_commits
        for policy in (active_policy, paused_policy):
            if policy is not None:
                signed_required = signed_required or policy.get(
                    "require_signed_commits", False
                )
    except RuntimeError:
        print("Updater signature policy is unreadable; rollback is blocked fail-closed.", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="blun-language-rollback-") as directory:
        candidate = Path(directory) / "repo"
        clone = _run(["git", "clone", "--no-hardlinks", "--no-checkout", str(root), str(candidate)])
        checkout = _run(["git", "checkout", "--detach", target], candidate) if not clone.returncode else clone
        if clone.returncode or checkout.returncode:
            print("Rollback candidate failed checkout; current installation is unchanged.", file=sys.stderr)
            return 1
        if signed_required and _run(["git", "verify-commit", target], candidate).returncode:
            print("Rollback target is not signed by a trusted Git identity; current installation is unchanged.", file=sys.stderr)
            return 1
        tests = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], candidate
        )
        if tests.returncode:
            print("Rollback candidate failed tests; current installation is unchanged.", file=sys.stderr)
            return 1
        try:
            target_version = (candidate / "VERSION").read_text(encoding="utf-8-sig").strip()
        except OSError:
            print("Rollback target has no readable VERSION; current installation is unchanged.", file=sys.stderr)
            return 1
    claude_installed = TARGETS["claude"].is_symlink()
    plugin_status: dict = {"installed": False, "reason": "claude-skill-not-installed"}
    if claude_installed:
        configured = claude_command or _configured_claude_command(rollback_monitor_config)
        plugin_status = claude_plugin_status(target_version, configured)
        if plugin_status.get("reason") != "plugin-not-installed" and plugin_status.get("healthy") is not True:
            print(
                "Claude plugin cache does not already match the rollback version. Anthropic's public CLI "
                "documents update-to-latest but no version-pinned downgrade; synchronize the marketplace "
                "cache first, then retry. Current installation is unchanged.",
                file=sys.stderr,
            )
            return 1
    if _clean_checkout_revision(root) != current:
        print(
            "The active checkout changed during rollback preflight; rollback activation is blocked.",
            file=sys.stderr,
        )
        return 2
    def block_changed_cutover(
        phase: str, *, restart_forward_runtime: bool = False
    ) -> int:
        observed = _run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], root
        ).stdout.strip()
        if observed == target:
            restored = _run(["git", "reset", "--keep", current], root)
            restored_head = _run(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"], root
            ).stdout.strip()
            repository_restored = restored.returncode == 0 and restored_head == current
            runtime_restored = True
            runtime_restore_detail = ""
            if repository_restored and restart_forward_runtime:
                runtime_restored, runtime_restore_detail = _restart_installed_runtimes()
            safe = repository_restored and runtime_restored
            if safe:
                outcome = "the forward revision and runtimes were restored without discarding local work."
            elif repository_restored:
                outcome = (
                    "the forward revision was restored without discarding local work, but its runtimes "
                    f"failed to restart ({runtime_restore_detail}). Manual inspection is required."
                )
            else:
                outcome = "the safe forward restoration failed. Manual inspection is required."
            print(
                f"The active checkout changed {phase}; runtime activation is blocked and "
                + outcome,
                file=sys.stderr,
            )
            return 2 if safe else 1
        print(
            f"HEAD changed independently {phase}; runtime activation is blocked and the "
            "independent commit was not reset.",
            file=sys.stderr,
        )
        return 2

    applied = _run(["git", "reset", "--keep", target], root)
    if applied.returncode:
        print("Rollback reset failed; current installation is unchanged.", file=sys.stderr)
        return 1
    if _clean_checkout_revision(root) != target:
        return block_changed_cutover("during rollback cutover")
    post_tests = _run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
        root,
    )
    if _clean_checkout_revision(root) != target:
        return block_changed_cutover("while running post-rollback tests")
    runtime_ok, runtime_detail = _restart_installed_runtimes() if not post_tests.returncode else (False, "post-rollback tests failed")
    if claude_installed and runtime_ok:
        configured = claude_command or _configured_claude_command(rollback_monitor_config)
        plugin_status = claude_plugin_status(target_version, configured)
        runtime_ok = plugin_status.get("healthy") is True or plugin_status.get("reason") == "plugin-not-installed"
        if not runtime_ok:
            runtime_detail = "Claude plugin cache changed during rollback"
    if _clean_checkout_revision(root) != target:
        return block_changed_cutover(
            "during rollback runtime verification", restart_forward_runtime=True
        )
    if not runtime_ok:
        restored = _run(["git", "reset", "--keep", current], root)
        _restart_installed_runtimes()
        print(
            f"Rollback verification failed ({runtime_detail}); forward restoration "
            + ("succeeded." if restored.returncode == 0 else "FAILED."),
            file=sys.stderr,
        )
        return 1
    try:
        _assert_protected_state_unchanged(
            UPDATE_STATE,
            "updater state",
            MAX_UPDATE_STATE_BYTES,
            update_state_identity,
        )
    except RuntimeError as error:
        restored = _run(["git", "reset", "--keep", current], root)
        _restart_installed_runtimes()
        print(
            f"Updater state changed during rollback verification ({error}); forward restoration "
            + ("succeeded." if restored.returncode == 0 else "FAILED."),
            file=sys.stderr,
        )
        return 2 if restored.returncode == 0 else 1
    try:
        _assert_update_policy_unchanged(UPDATE_CONFIG, active_policy_identity)
        _assert_update_policy_unchanged(UPDATE_PAUSED_CONFIG, paused_policy_identity)
        if active_policy is not None:
            _atomic_json(
                UPDATE_PAUSED_CONFIG,
                active_policy,
                before_replace=lambda: (
                    _assert_update_policy_unchanged(
                        UPDATE_CONFIG, active_policy_identity
                    ),
                    _assert_update_policy_unchanged(
                        UPDATE_PAUSED_CONFIG, paused_policy_identity
                    ),
                ),
            )
            _remove_update_policy(UPDATE_CONFIG, active_policy_identity)
    except (OSError, RuntimeError) as error:
        restored = _run(["git", "reset", "--keep", current], root)
        _restart_installed_runtimes()
        print(
            f"Rollback could not pause automatic updates ({error}); forward restoration "
            + ("succeeded." if restored.returncode == 0 else "FAILED."),
            file=sys.stderr,
        )
        return 1
    remove_scheduler()
    try:
        _write_update_state(
            {
                "status": "rolled_back",
                "revision": target,
                "rolled_back_from": current,
                "checked_at": int(time.time()),
                "runtime_version": target_version,
                "auto_update_paused": True,
                "paused_update_policy": (
                    str(UPDATE_PAUSED_CONFIG)
                    if UPDATE_PAUSED_CONFIG.exists()
                    else "not-enabled"
                ),
                "claude_plugin": plugin_status,
            },
            update_state_identity,
        )
    except (OSError, RuntimeError) as error:
        print(
            f"Updater state changed before rollback publication; the replacement was preserved: {error}",
            file=sys.stderr,
        )
        return 2
    print(
        f"Rolled back to tested revision {target}; automatic updates are paused. "
        "After inspection, run an explicit update and re-enable auto-update deliberately."
    )
    if claude_installed and plugin_status.get("installed"):
        print("Start a new Claude session or run /reload-plugins before relying on the rolled-back hooks.")
    return 0


def install_scheduler() -> tuple[bool, str]:
    command = f'"{sys.executable}" "{Path(__file__).resolve()}" auto-update run'
    system = platform.system()
    if system == "Linux":
        home = Path.home()
        units = home / ".config" / "systemd" / "user"
        service = units / "blun-language-guard-update.service"
        timer = units / "blun-language-guard-update.timer"
        _write_service_definition(
            service,
            "[Unit]\nDescription=Update BLUN Language Guard safely\n\n"
            "[Service]\nType=oneshot\nExecStart=" + command + "\n",
            home=home,
        )
        _write_service_definition(
            timer,
            "[Unit]\nDescription=Daily BLUN Language Guard update check\n\n"
            "[Timer]\nOnBootSec=5m\nOnUnitActiveSec=1h\nPersistent=true\n"
            "RandomizedDelaySec=10m\n\n[Install]\nWantedBy=timers.target\n",
            home=home,
        )
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", timer.name])
        ok = reload_result.returncode == 0 and enable_result.returncode == 0
        return ok, str(timer)
    if system == "Darwin":
        home = Path.home()
        agents = home / "Library" / "LaunchAgents"
        plist = agents / "ai.blun.language-guard-updater.plist"
        _write_service_definition(plist, """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>ai.blun.language-guard-updater</string>
<key>ProgramArguments</key><array><string>""" + sys.executable + "</string><string>" + str(Path(__file__).resolve()) + "</string><string>auto-update</string><string>run</string></array>\n<key>StartInterval</key><integer>3600</integer><key>RunAtLoad</key><true/></dict></plist>\n", home=home)
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _run(["schtasks", "/Create", "/F", "/SC", "HOURLY", "/TN", "BLUN Language Guard Updater", "/TR", command])
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard Updater"
    return False, f"No scheduler adapter for {system}"


def remove_scheduler() -> None:
    system = platform.system()
    if system == "Linux":
        home = Path.home()
        units = home / ".config" / "systemd" / "user"
        definitions = (
            (
                units / "blun-language-guard-update.service",
                ("Update BLUN Language Guard safely", "auto-update run"),
            ),
            (
                units / "blun-language-guard-update.timer",
                ("Daily BLUN Language Guard update check", "OnUnitActiveSec=1h"),
            ),
        )
        with _prepare_service_definition_removals(home, definitions) as prepared:
            _run(["systemctl", "--user", "disable", "--now", "blun-language-guard-update.timer"])
            for directory, path, expected in prepared:
                _remove_service_definition_at(directory, path, expected)
            _run(["systemctl", "--user", "daemon-reload"])
    elif system == "Darwin":
        home = Path.home()
        plist = home / "Library" / "LaunchAgents" / "ai.blun.language-guard-updater.plist"
        definitions = ((
            plist,
            ("ai.blun.language-guard-updater", "auto-update", "<string>run</string>"),
        ),)
        with _prepare_service_definition_removals(home, definitions) as prepared:
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
            for directory, path, expected in prepared:
                _remove_service_definition_at(directory, path, expected)
    elif system == "Windows":
        _run(["schtasks", "/Delete", "/F", "/TN", "BLUN Language Guard Updater"])


def _install_windows_health_task(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    executable = arguments[0]
    argument_line = subprocess.list2cmdline(arguments[1:])
    script = (
        "$ErrorActionPreference='Stop';"
        f"$action=New-ScheduledTaskAction -Execute {_powershell_literal(executable)} "
        f"-Argument {_powershell_literal(argument_line)};"
        "$trigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        "-RepetitionInterval (New-TimeSpan -Minutes 1);"
        "$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 2);"
        "Register-ScheduledTask -TaskName 'BLUN Language Guard Health' -Action $action "
        "-Trigger $trigger -Settings $settings -Force | Out-Null"
    )
    return _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def install_health_monitor(home: Path | None = None) -> tuple[bool, str]:
    """Install a one-minute dependency-aware health check without embedding secrets."""
    home = home or Path.home()
    arguments = [sys.executable, str(Path(__file__).resolve()), "health-monitor", "run"]
    system = platform.system()
    if system == "Linux":
        units = home / ".config" / "systemd" / "user"
        service = units / "blun-language-guard-health.service"
        timer = units / "blun-language-guard-health.timer"
        _write_service_definition(
            service,
            "[Unit]\nDescription=Verify and repair BLUN Language Guard\n"
            "After=blun-language-guard.service blun-language-guard-mcp.service\n\n"
            "[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            f"ExecStart={_shell_command(arguments)}\n",
            home=home,
        )
        _write_service_definition(
            timer,
            "[Unit]\nDescription=Monitor BLUN Language Guard every minute\n\n"
            "[Timer]\nOnBootSec=1m\nOnUnitActiveSec=1m\nAccuracySec=10s\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n",
            home=home,
        )
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", timer.name])
        return reload_result.returncode == 0 and enable_result.returncode == 0, str(timer)
    if system == "Darwin":
        agents = home / "Library" / "LaunchAgents"
        plist = agents / "ai.blun.language-guard-health.plist"
        program_arguments = "".join(f"<string>{_xml_escape(value)}</string>" for value in arguments)
        _write_service_definition(
            plist,
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict><key>Label</key>"
            "<string>ai.blun.language-guard-health</string>"
            f"<key>ProgramArguments</key><array>{program_arguments}</array>"
            "<key>RunAtLoad</key><true/><key>StartInterval</key><integer>60</integer>"
            "<key>ThrottleInterval</key><integer>10</integer></dict></plist>\n",
            home=home,
        )
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _install_windows_health_task(arguments)
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard Health"
    return False, f"No health-monitor adapter for {system}"


def remove_health_monitor(home: Path | None = None) -> None:
    home = home or Path.home()
    system = platform.system()
    if system == "Linux":
        units = home / ".config" / "systemd" / "user"
        definitions = (
            (
                units / "blun-language-guard-health.service",
                ("Verify and repair BLUN Language Guard", "health-monitor"),
            ),
            (
                units / "blun-language-guard-health.timer",
                ("Monitor BLUN Language Guard every minute", "OnUnitActiveSec=1m"),
            ),
        )
        with _prepare_service_definition_removals(home, definitions) as prepared:
            _run(["systemctl", "--user", "disable", "--now", "blun-language-guard-health.timer"])
            for directory, path, expected in prepared:
                _remove_service_definition_at(directory, path, expected)
            _run(["systemctl", "--user", "daemon-reload"])
    elif system == "Darwin":
        plist = home / "Library" / "LaunchAgents" / "ai.blun.language-guard-health.plist"
        definitions = ((
            plist,
            ("ai.blun.language-guard-health", "health-monitor", "<integer>60</integer>"),
        ),)
        with _prepare_service_definition_removals(home, definitions) as prepared:
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
            for directory, path, expected in prepared:
                _remove_service_definition_at(directory, path, expected)
    elif system == "Windows":
        _run(["schtasks", "/Delete", "/F", "/TN", "BLUN Language Guard Health"])


def health_monitor_enabled() -> bool:
    """Default existing Claude installations into the safe one-time migration."""
    value = _load_health_config()
    if value is None:
        return True
    return value.get("enabled") is not False


def _health_monitor_config() -> dict:
    return _load_health_config() or {}


def _configured_claude_command(config: dict | None = None) -> str:
    """Resolve the owner-approved Claude CLI path without guessing private cache paths."""
    config = config or _health_monitor_config()
    command = config.get("claude_command")
    if isinstance(command, str) and command:
        return command
    try:
        updater = _load_update_policy(UPDATE_CONFIG) or {}
    except RuntimeError:
        updater = {}
    command = updater.get("claude_command") if isinstance(updater, dict) else ""
    if isinstance(command, str) and command:
        return command
    return shutil.which("claude") or ""


def _claude_plugin_monitor_status(
    config: dict | None = None,
    *,
    config_identity: tuple[int, int, int, int, int, int] | None = None,
) -> dict:
    """Check an enrolled Claude plugin cache without installing a missing plugin."""
    if config is None:
        stored_config, observed_identity = _read_health_config()
        config = dict(stored_config or {})
        if config_identity is None:
            config_identity = observed_identity
    else:
        config = dict(config)
    required = config.get("plugin_required") is True
    if not TARGETS["claude"].is_symlink():
        return {"required": False, "healthy": True, "reason": "claude-skill-not-installed"}
    command = _configured_claude_command(config)
    if not command:
        return {
            "required": required,
            "healthy": not required,
            "reason": "claude-command-unavailable",
        }
    try:
        expected_version = (repository_root() / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return {"required": required, "healthy": False, "reason": "runtime-version-unavailable"}
    status = claude_plugin_status(expected_version, command)
    policy_enrolled = False
    if status.get("installed") and not required:
        config.update({
            "enabled": config.get("enabled") is not False,
            "interval_seconds": 60,
            "plugin_required": True,
            "claude_command": command,
        })
        _atomic_json(
            HEALTH_CONFIG,
            config,
            before_replace=lambda: _assert_protected_state_unchanged(
                HEALTH_CONFIG,
                "health-monitor policy",
                MAX_HEALTH_FILE_BYTES,
                config_identity,
            ),
        )
        required = True
        policy_enrolled = True
    if not required:
        return {
            "required": False,
            "healthy": True,
            "reason": status.get("reason", "plugin-not-enrolled"),
            "command": command,
        }
    return {
        "required": True,
        "healthy": status.get("healthy") is True,
        "reason": status.get("reason", "ok" if status.get("healthy") else "plugin-cache-unhealthy"),
        "version": status.get("version", ""),
        "expected_version": expected_version,
        "command": command,
        "status": status,
        "policy_enrolled": policy_enrolled,
    }


def _plugin_health_fields(plugin: dict) -> dict:
    return {
        "plugin_required": plugin.get("required") is True,
        "plugin_cache_healthy": plugin.get("healthy") is True,
        "plugin_cache_version": plugin.get("version", ""),
        "plugin_cache_reason": plugin.get("reason", ""),
    }


def _guard_stack_status(timeout: float = 1.0) -> tuple[bool, bool]:
    try:
        guard = probe_guard_service(timeout=timeout)
        guard_healthy = guard.get("status") == "ok" and guard.get("isolated_key") is True
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        guard_healthy = False
    if not guard_healthy:
        return False, False
    try:
        probe_mcp_http(timeout=timeout)
        mcp_healthy = True
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        mcp_healthy = False
    return guard_healthy, mcp_healthy


def _wait_for_stack(*, guard: bool = False, mcp: bool = False, attempts: int = 8) -> bool:
    for _attempt in range(attempts):
        guard_healthy, mcp_healthy = _guard_stack_status()
        if (not guard or guard_healthy) and (not mcp or mcp_healthy):
            return True
        time.sleep(0.2)
    return False


def _state_integer(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _health_repair_delay(consecutive_failures: int) -> int:
    """Return bounded exponential backoff after a failed repair attempt."""
    index = min(max(consecutive_failures - 1, 0), len(HEALTH_REPAIR_BACKOFF_SECONDS) - 1)
    return HEALTH_REPAIR_BACKOFF_SECONDS[index]


def health_monitor_run(*, now: int | None = None) -> int:
    """Probe signer, MCP, and enrolled Claude cache, then make one ordered repair pass."""
    timestamp = int(time.time()) if now is None else now
    token = _acquire_operation_lock("health-monitor", now=timestamp)
    if token is None:
        print(json.dumps({"status": "busy", "checked_at": timestamp}, sort_keys=True))
        return 0
    try:
        try:
            stored_config, config_identity = _read_health_config()
            config = dict(stored_config or {})
            stored_state, state_identity = _read_health_state()
            previous = dict(stored_state or {})
        except RuntimeError as error:
            print(json.dumps({
                "status": "blocked",
                "reason": "unsafe-health-state",
                "checked_at": timestamp,
                "detail": str(error),
            }, sort_keys=True), file=sys.stderr)
            return 2

        def block_state_transition(error: RuntimeError | OSError) -> int:
            print(json.dumps({
                "status": "blocked",
                "reason": "unsafe-health-state-transition",
                "checked_at": timestamp,
                "detail": str(error),
            }, sort_keys=True), file=sys.stderr)
            return 2

        def block_policy_transition(error: RuntimeError | OSError) -> int:
            print(json.dumps({
                "status": "blocked",
                "reason": "unsafe-health-policy-transition",
                "checked_at": timestamp,
                "detail": str(error),
            }, sort_keys=True), file=sys.stderr)
            return 2

        def policy_unchanged() -> bool:
            try:
                _assert_protected_state_unchanged(
                    HEALTH_CONFIG,
                    "health-monitor policy",
                    MAX_HEALTH_FILE_BYTES,
                    config_identity,
                )
            except (OSError, RuntimeError) as error:
                block_policy_transition(error)
                return False
            return True

        def persist_state(state: dict) -> bool:
            if not policy_unchanged():
                return False
            try:
                _atomic_json(
                    HEALTH_STATE,
                    state,
                    before_replace=lambda: (
                        _assert_protected_state_unchanged(
                            HEALTH_CONFIG,
                            "health-monitor policy",
                            MAX_HEALTH_FILE_BYTES,
                            config_identity,
                        ),
                        _assert_protected_state_unchanged(
                            HEALTH_STATE,
                            "health-monitor state",
                            MAX_HEALTH_FILE_BYTES,
                            state_identity,
                        ),
                    ),
                )
            except (OSError, RuntimeError) as error:
                if "health-monitor policy" in str(error).lower():
                    block_policy_transition(error)
                else:
                    block_state_transition(error)
                return False
            return True
        guard_healthy, mcp_healthy = _guard_stack_status()
        try:
            plugin = _claude_plugin_monitor_status(
                config,
                config_identity=config_identity,
            )
        except RuntimeError as error:
            print(json.dumps({
                "status": "blocked",
                "reason": "unsafe-health-policy-transition",
                "checked_at": timestamp,
                "detail": str(error),
            }, sort_keys=True), file=sys.stderr)
            return 2
        if plugin.get("policy_enrolled") is True:
            try:
                stored_config, config_identity = _read_health_config()
                if stored_config is None or stored_config.get("plugin_required") is not True:
                    raise RuntimeError(
                        "Health-monitor policy changed after Claude plugin enrollment"
                    )
                config = dict(stored_config)
            except RuntimeError as error:
                print(json.dumps({
                    "status": "blocked",
                    "reason": "unsafe-health-policy-transition",
                    "checked_at": timestamp,
                    "detail": str(error),
                }, sort_keys=True), file=sys.stderr)
                return 2
        if not policy_unchanged():
            return 2
        if guard_healthy and mcp_healthy and plugin.get("healthy") is True:
            state = {
                "status": "ok",
                "checked_at": timestamp,
                "guard_healthy": True,
                "mcp_healthy": True,
                "consecutive_failures": 0,
                "last_repair_at": previous.get("last_repair_at", 0),
                "next_repair_at": 0,
                "repairs": [],
                **_plugin_health_fields(plugin),
            }
            if not persist_state(state):
                return 2
            print(json.dumps(state, sort_keys=True))
            return 0
        previous_failures = _state_integer(previous.get("consecutive_failures"))
        last_repair = _state_integer(previous.get("last_repair_at"))
        next_repair = _state_integer(previous.get("next_repair_at"))
        if not next_repair and previous.get("status") == "blocked" and last_repair:
            next_repair = last_repair + _health_repair_delay(previous_failures)
        if timestamp < next_repair:
            state = {
                "status": "blocked",
                "reason": "repair-backoff",
                "checked_at": timestamp,
                "guard_healthy": guard_healthy,
                "mcp_healthy": mcp_healthy,
                "consecutive_failures": previous_failures,
                "last_repair_at": last_repair,
                "next_repair_at": next_repair,
                "repairs": [],
                **_plugin_health_fields(plugin),
            }
            if not persist_state(state):
                return 2
            print(json.dumps(state, sort_keys=True), file=sys.stderr)
            return 1
        try:
            _assert_protected_state_unchanged(
                HEALTH_STATE,
                "health-monitor state",
                MAX_HEALTH_FILE_BYTES,
                state_identity,
            )
        except RuntimeError as error:
            return block_state_transition(error)
        if not policy_unchanged():
            return 2
        repairs: list[str] = []
        if not guard_healthy:
            restarted, _detail = restart_guard_runtime()
            repairs.append("guard-restart")
            guard_healthy = restarted and _wait_for_stack(guard=True)
        if guard_healthy:
            _guard_now, mcp_healthy = _guard_stack_status()
            if not mcp_healthy:
                if not policy_unchanged():
                    return 2
                restarted, _detail = restart_mcp_http_runtime()
                repairs.append("mcp-restart")
                mcp_healthy = restarted and _wait_for_stack(guard=True, mcp=True)
        if (
            guard_healthy
            and mcp_healthy
            and plugin.get("required") is True
            and plugin.get("healthy") is not True
            and plugin.get("expected_version")
            and plugin.get("command")
        ):
            if not policy_unchanged():
                return 2
            plugin_update = update_claude_plugin(
                str(plugin.get("expected_version", "")),
                str(plugin.get("command", "")) or None,
            )
            if plugin_update.get("attempted"):
                repairs.append("claude-plugin-update")
        guard_healthy, mcp_healthy = _guard_stack_status()
        try:
            plugin = _claude_plugin_monitor_status(
                config,
                config_identity=config_identity,
            )
        except RuntimeError as error:
            return block_policy_transition(error)
        recovered = guard_healthy and mcp_healthy and plugin.get("healthy") is True
        failures = 0 if recovered else previous_failures + 1
        state = {
            "status": "recovered" if recovered else "blocked",
            "checked_at": timestamp,
            "guard_healthy": guard_healthy,
            "mcp_healthy": mcp_healthy,
            "consecutive_failures": failures,
            "last_repair_at": timestamp,
            "next_repair_at": 0 if recovered else timestamp + _health_repair_delay(failures),
            "repairs": repairs,
            **_plugin_health_fields(plugin),
        }
        if not persist_state(state):
            return 2
        print(json.dumps(state, sort_keys=True), file=sys.stdout if recovered else sys.stderr)
        return 0 if recovered else 1
    finally:
        _release_operation_lock(token)


def health_monitor(action: str) -> int:
    if action == "run":
        return health_monitor_run()
    if action == "status":
        try:
            enabled = health_monitor_enabled()
        except RuntimeError as error:
            print(f"Health-monitor policy is unsafe; status is blocked fail-closed: {error}", file=sys.stderr)
            return 2
        if not enabled:
            print(json.dumps({"status": "disabled"}))
            return 0
        try:
            state = _load_health_state()
        except RuntimeError as error:
            print(f"Health-monitor state is unsafe; status is blocked fail-closed: {error}", file=sys.stderr)
            return 2
        if state is None:
            print(json.dumps({"status": "not-run"}))
            return 1
        print(json.dumps(state, indent=2, sort_keys=True))
        fresh = int(time.time()) - int(state.get("checked_at", 0)) <= 180
        return 0 if fresh and state.get("status") in {"ok", "recovered"} else 1
    if action == "remove":
        try:
            _config, config_identity = _read_health_config()
            _state, state_identity = _read_health_state()
            _assert_protected_state_unchanged(
                HEALTH_CONFIG,
                "health-monitor policy",
                MAX_HEALTH_FILE_BYTES,
                config_identity,
            )
            _assert_protected_state_unchanged(
                HEALTH_STATE,
                "health-monitor state",
                MAX_HEALTH_FILE_BYTES,
                state_identity,
            )
        except RuntimeError as error:
            print(
                f"Health-monitor reset is blocked fail-closed by unsafe state: {error}",
                file=sys.stderr,
            )
            return 2
        remove_health_monitor()
        try:
            _atomic_json(
                HEALTH_CONFIG,
                {"enabled": False, "interval_seconds": 60},
                before_replace=lambda: _assert_protected_state_unchanged(
                    HEALTH_CONFIG,
                    "health-monitor policy",
                    MAX_HEALTH_FILE_BYTES,
                    config_identity,
                ),
            )
            _assert_protected_state_unchanged(
                HEALTH_STATE,
                "health-monitor state",
                MAX_HEALTH_FILE_BYTES,
                state_identity,
            )
            if state_identity is not None:
                HEALTH_STATE.unlink()
        except (OSError, RuntimeError) as error:
            print(
                f"Health-monitor reset stopped before unsafe state replacement: {error}",
                file=sys.stderr,
            )
            return 2
        print("Health monitor removed; guard services and secrets were preserved.")
        return 0
    check = health_monitor_run()
    if check == 2:
        return 2
    try:
        stored_config, config_identity = _read_health_config()
        config = dict(stored_config or {})
        config.update({
            "enabled": True,
            "interval_seconds": 60,
            "claude_command": _configured_claude_command(config),
        })
        _assert_protected_state_unchanged(
            HEALTH_CONFIG,
            "health-monitor policy",
            MAX_HEALTH_FILE_BYTES,
            config_identity,
        )
    except RuntimeError as error:
        print(
            f"Health-monitor installation is blocked fail-closed by unsafe policy: {error}",
            file=sys.stderr,
        )
        return 2
    ok, detail = install_health_monitor()
    if ok:
        try:
            _atomic_json(
                HEALTH_CONFIG,
                config,
                before_replace=lambda: _assert_protected_state_unchanged(
                    HEALTH_CONFIG,
                    "health-monitor policy",
                    MAX_HEALTH_FILE_BYTES,
                    config_identity,
                ),
            )
        except (OSError, RuntimeError) as error:
            cleanup = "scheduler rollback completed"
            try:
                remove_health_monitor()
            except (OSError, RuntimeError) as cleanup_error:
                cleanup = f"scheduler rollback failed: {cleanup_error}"
            print(
                "Health-monitor installation stopped before unsafe policy replacement; "
                f"{cleanup}: {error}",
                file=sys.stderr,
            )
            return 2
    print(f"{'Health monitor installed' if ok else 'Health monitor installation failed'}: {detail}")
    return 0 if ok and check == 0 else 1


def auto_update(action: str, interval_hours: int = 24, require_signed_commits: bool = False, scheduler: bool = True) -> int:
    if action == "enable":
        try:
            active_policy, active_identity = _read_update_policy(UPDATE_CONFIG)
            paused_policy, paused_identity = _read_update_policy(UPDATE_PAUSED_CONFIG)
            signed_required = require_signed_commits
            for policy in (active_policy, paused_policy):
                if policy is not None:
                    signed_required = signed_required or policy.get(
                        "require_signed_commits", False
                    )
            _assert_update_policy_unchanged(UPDATE_CONFIG, active_identity)
            _assert_update_policy_unchanged(UPDATE_PAUSED_CONFIG, paused_identity)
            _atomic_json(
                UPDATE_CONFIG,
                {
                    "enabled": True,
                    "interval_hours": max(1, interval_hours),
                    "require_signed_commits": signed_required,
                    "repository": REPO_URL,
                    "claude_command": shutil.which("claude") or "",
                },
                before_replace=lambda: _assert_update_policy_unchanged(
                    UPDATE_CONFIG, active_identity
                ),
            )
            _remove_update_policy(UPDATE_PAUSED_CONFIG, paused_identity)
        except (OSError, RuntimeError) as error:
            print(
                f"Updater policy state changed or is unreadable; automatic updates were not reconfigured: {error}",
                file=sys.stderr,
            )
            return 2
        print(f"Automatic updates enabled every {max(1, interval_hours)} hour(s).")
        if scheduler:
            ok, detail = install_scheduler()
            print(f"{'Scheduler installed' if ok else 'Scheduler installation failed'}: {detail}")
            return 0 if ok else 1
        return 0
    if action == "disable":
        try:
            _active, active_identity = _read_update_policy(UPDATE_CONFIG)
            _paused, paused_identity = _read_update_policy(UPDATE_PAUSED_CONFIG)
            _assert_update_policy_unchanged(UPDATE_CONFIG, active_identity)
            _assert_update_policy_unchanged(UPDATE_PAUSED_CONFIG, paused_identity)
        except RuntimeError as error:
            print(
                f"Updater reset is blocked fail-closed by unsafe policy state: {error}",
                file=sys.stderr,
            )
            return 2
        remove_scheduler()
        try:
            _remove_update_policy(UPDATE_CONFIG, active_identity)
            _remove_update_policy(UPDATE_PAUSED_CONFIG, paused_identity)
        except (OSError, RuntimeError) as error:
            print(
                f"Updater reset stopped before unsafe policy removal: {error}",
                file=sys.stderr,
            )
            return 2
        print("Automatic updates disabled.")
        return 0
    if action == "status":
        try:
            config = _load_update_policy(UPDATE_CONFIG)
        except RuntimeError:
            print("Updater policy is unreadable; status is blocked fail-closed.", file=sys.stderr)
            return 2
        print(json.dumps(config if config is not None else {"enabled": False}, indent=2))
        try:
            state = _load_update_state()
        except RuntimeError:
            print("Updater state is unsafe; status is blocked fail-closed.", file=sys.stderr)
            return 2
        if state is not None:
            print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    try:
        last = _load_update_state() or {}
    except RuntimeError:
        print("Updater state is unsafe; automatic update is blocked fail-closed.", file=sys.stderr)
        return 2
    if last.get("status") == "rolled_back" and last.get("auto_update_paused") is True:
        print("Automatic updates are paused after rollback; update explicitly, then re-enable auto-update.")
        return 0
    try:
        config = _load_update_policy(UPDATE_CONFIG)
    except RuntimeError:
        print("Updater policy is unreadable; automatic update is blocked fail-closed.", file=sys.stderr)
        return 2
    if config is None:
        print("Automatic updates are not enabled.", file=sys.stderr)
        return 2
    if config.get("enabled") is not True:
        print("Automatic update policy is not enabled; update is blocked fail-closed.", file=sys.stderr)
        return 2
    try:
        monitor_enabled = health_monitor_enabled()
        _load_health_state()
    except RuntimeError as error:
        print(
            f"Health-monitor protected state is unsafe; automatic update is blocked fail-closed: {error}",
            file=sys.stderr,
        )
        return 2
    due = (
        (
            TARGETS["claude"].is_symlink()
            and monitor_enabled
            and (not HEALTH_CONFIG.exists() or not HEALTH_STATE.exists())
        )
        or
        last.get("status") != "ok"
        or int(time.time()) - int(last.get("checked_at", 0)) >= int(config["interval_hours"]) * 3600
    )
    if not due:
        print("Update check is not due yet.")
        return 0
    return update(bool(config.get("require_signed_commits")), config.get("claude_command") or None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and diagnose BLUN Language Guard")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--target", action="append", choices=tuple(TARGETS), dest="targets")
    install_parser.add_argument("--no-service-autostart", action="store_true")
    service_parser = sub.add_parser("service")
    service_parser.add_argument("action", choices=("install", "start", "stop", "status"))
    mcp_service_parser = sub.add_parser("mcp-service")
    mcp_service_parser.add_argument("action", choices=("install", "start", "stop", "status"))
    monitor_parser = sub.add_parser("health-monitor")
    monitor_parser.add_argument("action", choices=("install", "remove", "run", "status"))
    sub.add_parser("doctor")
    update_parser = sub.add_parser("update")
    update_parser.add_argument("--require-signed-commits", action="store_true")
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("--require-signed-commits", action="store_true")
    auto = sub.add_parser("auto-update")
    auto.add_argument("action", choices=("enable", "disable", "status", "run"))
    auto.add_argument("--interval-hours", type=int, default=24)
    auto.add_argument("--require-signed-commits", action="store_true")
    auto.add_argument("--no-scheduler", action="store_true", help="Write policy only; do not install an OS scheduler")
    args = parser.parse_args()
    if args.command == "install":
        return install(args.targets or list(TARGETS), autostart_service=not args.no_service_autostart)
    if args.command == "service":
        return guard_service(args.action)
    if args.command == "mcp-service":
        return mcp_service(args.action)
    if args.command == "health-monitor":
        return health_monitor(args.action)
    if args.command == "doctor":
        return doctor()
    if args.command == "update":
        return update(args.require_signed_commits)
    if args.command == "rollback":
        return rollback(args.require_signed_commits)
    return auto_update(args.action, args.interval_hours, args.require_signed_commits, not args.no_scheduler)


if __name__ == "__main__":
    raise SystemExit(main())
