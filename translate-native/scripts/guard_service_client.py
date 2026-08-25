#!/usr/bin/env python3
"""Zero-dependency client for the isolated BLUN Language Guard service."""

from __future__ import annotations

import json
import os
import socket
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SERVICE_TOKEN_BYTES = 64 * 1024


class GuardServiceError(RuntimeError):
    """Raised when the isolated guard cannot evaluate a request safely."""


def _token_file_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _token_directory_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _validate_token_directory(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise GuardServiceError(f"guard service token directory is invalid: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise GuardServiceError(
            f"guard service token directory is writable outside its owner: {path}"
        )
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise GuardServiceError(f"guard service token directory has the wrong owner: {path}")


def _existing_token_directory_anchor(path: Path) -> Path:
    candidate = path
    while True:
        try:
            candidate.lstat()
            return candidate
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise GuardServiceError(
                    f"guard service token directory has no existing anchor: {path}"
                )
            candidate = parent


def _assert_token_directory_unchanged(
    path: Path, expected: tuple[int, int, int, int, int],
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise GuardServiceError(
            f"guard service token directory cannot be rechecked: {path}"
        ) from error
    _validate_token_directory(path, current)
    if _token_directory_identity(current) != expected:
        raise GuardServiceError(
            f"guard service token directory changed during read: {path}"
        )


@contextmanager
def _open_token_directory(path: Path) -> Iterator[int | None]:
    if os.name == "nt":
        yield None
        return
    anchor = Path.home()
    try:
        relative = path.parent.relative_to(anchor)
    except ValueError:
        anchor = _existing_token_directory_anchor(path.parent)
        relative = path.parent.relative_to(anchor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    current_path = anchor
    try:
        try:
            descriptor = os.open(anchor, flags)
            _validate_token_directory(anchor, os.fstat(descriptor))
            for component in relative.parts:
                current_path = current_path / component
                child = os.open(component, flags, dir_fd=descriptor)
                try:
                    _validate_token_directory(current_path, os.fstat(child))
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            expected = _token_directory_identity(os.fstat(descriptor))
        except (FileNotFoundError, GuardServiceError):
            raise
        except OSError as error:
            raise GuardServiceError(
                f"guard service token directory cannot be opened safely: {current_path}"
            ) from error
        try:
            yield descriptor
        finally:
            _assert_token_directory_unchanged(path.parent, expected)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_token_file(details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise GuardServiceError("guard service token must be a regular file")
    if details.st_nlink != 1:
        raise GuardServiceError("guard service token must not have additional hard links")
    if details.st_size < 32 or details.st_size > MAX_SERVICE_TOKEN_BYTES:
        raise GuardServiceError("guard service token has an invalid size")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise GuardServiceError("guard service token permissions must be owner-only")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise GuardServiceError("guard service token has the wrong owner")


def _token_lstat(path: Path, directory: int | None) -> os.stat_result:
    if directory is None:
        return os.lstat(path)
    return os.stat(path.name, dir_fd=directory, follow_symlinks=False)


def _open_token_file(path: Path, directory: int | None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory is None:
        return os.open(path, flags)
    return os.open(path.name, flags, dir_fd=directory)


def _token_fstat(descriptor: int) -> os.stat_result:
    return os.fstat(descriptor)


def load_service_token(path: Path) -> str:
    """Read a bounded owner-only service token without following a replaced file."""
    with _open_token_directory(path) as directory:
        before = _token_lstat(path, directory)
        _validate_token_file(before)
        descriptor = _open_token_file(path, directory)
        try:
            opened = _token_fstat(descriptor)
            _validate_token_file(opened)
            if _token_file_identity(opened) != _token_file_identity(before):
                raise GuardServiceError("guard service token changed while opening")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(MAX_SERVICE_TOKEN_BYTES + 1)
            after = _token_fstat(descriptor)
            after_path = _token_lstat(path, directory)
            if (
                _token_file_identity(after) != _token_file_identity(opened)
                or _token_file_identity(after_path) != _token_file_identity(opened)
            ):
                raise GuardServiceError("guard service token changed while reading")
        finally:
            os.close(descriptor)
    if len(raw) > MAX_SERVICE_TOKEN_BYTES:
        raise GuardServiceError("guard service token has an invalid size")
    try:
        token = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise GuardServiceError("guard service token is not valid UTF-8") from error
    if len(token) < 32:
        raise GuardServiceError("guard service token is invalid")
    return token


def parse_endpoint(endpoint: str) -> tuple[str, str | tuple[str, int]]:
    value = str(endpoint or "").strip()
    if value.startswith("unix:") and value[5:]:
        return "unix", value[5:]
    if value.startswith("tcp:"):
        host_port = value[4:]
        host, separator, raw_port = host_port.rpartition(":")
        if separator and host in {"127.0.0.1", "localhost", "::1"}:
            try:
                port = int(raw_port)
            except ValueError as error:
                raise GuardServiceError("invalid guard service TCP port") from error
            if 1 <= port <= 65535:
                return "tcp", (host, port)
    raise GuardServiceError("guard service endpoint must be unix:/path or loopback tcp:host:port")


def call_guard_service(
    endpoint: str,
    request: dict[str, Any],
    *,
    auth_token: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    transport, address = parse_endpoint(endpoint)
    payload = dict(request)
    if auth_token:
        payload["service_token"] = auth_token
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise GuardServiceError("guard service request is too large")
    try:
        if transport == "unix":
            if not hasattr(socket, "AF_UNIX"):
                raise GuardServiceError("Unix sockets are unavailable on this platform")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            client = socket.create_connection(address, timeout=timeout)
        with client:
            client.settimeout(timeout)
            if transport == "unix":
                client.connect(address)
            client.sendall(encoded)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise GuardServiceError("guard service response is too large")
                if b"\n" in chunk:
                    break
    except (OSError, TimeoutError) as error:
        raise GuardServiceError("guard service is unavailable") from error
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GuardServiceError("guard service returned an invalid response") from error
    if not isinstance(response, dict):
        raise GuardServiceError("guard service response must be an object")
    return response
