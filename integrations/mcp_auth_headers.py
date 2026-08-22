#!/usr/bin/env python3
"""Emit Claude Code HTTP MCP authentication headers without exposing the token in config."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


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


def _validate_token_file(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"MCP access-token path is not a regular file: {path}")
    if details.st_size < 32 or details.st_size > MAX_TOKEN_BYTES:
        raise RuntimeError(f"MCP access token has an invalid size: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise RuntimeError(f"MCP access-token permissions must be owner-only: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"MCP access-token owner is invalid: {path}")


def load_token(path: Path) -> str:
    before = path.lstat()
    _validate_token_file(path, before)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        _validate_token_file(path, opened)
        if _file_identity(opened) != _file_identity(before):
            raise RuntimeError(f"MCP access token changed while opening: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_TOKEN_BYTES + 1)
        after_read = os.fstat(descriptor)
        after_path = path.lstat()
    finally:
        os.close(descriptor)
    if len(raw) > MAX_TOKEN_BYTES:
        raise RuntimeError(f"MCP access token has an invalid size: {path}")
    if _file_identity(after_read) != _file_identity(opened) or _file_identity(after_path) != _file_identity(opened):
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
