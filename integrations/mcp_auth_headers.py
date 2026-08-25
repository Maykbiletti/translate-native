#!/usr/bin/env python3
"""Emit Claude Code HTTP MCP authentication headers without exposing the token in config."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_TOKEN_FILE = Path.home() / ".config" / "blun-language-guard" / "mcp-http.token"
MAX_TOKEN_BYTES = 64 * 1024


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _directory_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _validate_token_directory(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"MCP access-token directory is invalid: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(f"MCP access-token directory is writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"MCP access-token directory owner is invalid: {path}")


def _existing_token_directory_anchor(path: Path) -> Path:
    candidate = path
    while True:
        try:
            candidate.lstat()
            return candidate
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise RuntimeError(f"MCP access-token directory has no existing anchor: {path}")
            candidate = parent


def _assert_token_directory_unchanged(
    path: Path, expected: tuple[int, int, int, int, int],
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise RuntimeError(f"MCP access-token directory cannot be rechecked: {path}") from error
    _validate_token_directory(path, current)
    if _directory_identity(current) != expected:
        raise RuntimeError(f"MCP access-token directory changed during read: {path}")


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
            expected = _directory_identity(os.fstat(descriptor))
        except (FileNotFoundError, RuntimeError):
            raise
        except OSError as error:
            raise RuntimeError(
                f"MCP access-token directory cannot be opened safely: {current_path}"
            ) from error
        try:
            yield descriptor
        finally:
            _assert_token_directory_unchanged(path.parent, expected)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_token_file(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"MCP access-token path is not a regular file: {path}")
    if details.st_size < 32 or details.st_size > MAX_TOKEN_BYTES:
        raise RuntimeError(f"MCP access token has an invalid size: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise RuntimeError(f"MCP access-token permissions must be owner-only: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"MCP access-token owner is invalid: {path}")


def _token_lstat(path: Path, directory: int | None) -> os.stat_result:
    if directory is None:
        return path.lstat()
    return os.stat(path.name, dir_fd=directory, follow_symlinks=False)


def _open_token_file(path: Path, directory: int | None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory is None:
        return os.open(path, flags)
    return os.open(path.name, flags, dir_fd=directory)


def _token_fstat(descriptor: int) -> os.stat_result:
    return os.fstat(descriptor)


def load_token(path: Path) -> str:
    with _open_token_directory(path) as directory:
        before = _token_lstat(path, directory)
        _validate_token_file(path, before)
        descriptor = _open_token_file(path, directory)
        try:
            opened = _token_fstat(descriptor)
            _validate_token_file(path, opened)
            if _file_identity(opened) != _file_identity(before):
                raise RuntimeError(f"MCP access token changed while opening: {path}")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(MAX_TOKEN_BYTES + 1)
            after_read = _token_fstat(descriptor)
            after_path = _token_lstat(path, directory)
        finally:
            os.close(descriptor)
    if len(raw) > MAX_TOKEN_BYTES:
        raise RuntimeError(f"MCP access token has an invalid size: {path}")
    if (
        _file_identity(after_read) != _file_identity(opened)
        or _file_identity(after_path) != _file_identity(opened)
    ):
        raise RuntimeError(f"MCP access token changed while reading: {path}")
    try:
        token = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError(f"MCP access token is not valid UTF-8: {path}") from error
    if len(token) < 32:
        raise RuntimeError(f"MCP access token is invalid: {path}")
    return token


def main() -> int:
    configured = os.environ.get("BLUN_LANGUAGE_GUARD_MCP_TOKEN_FILE", "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_TOKEN_FILE
    try:
        token = load_token(path)
    except (OSError, RuntimeError) as error:
        print(f"BLOCK: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"Authorization": f"Bearer {token}"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
