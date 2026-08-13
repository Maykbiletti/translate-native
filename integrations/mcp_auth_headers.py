#!/usr/bin/env python3
"""Emit Claude Code HTTP MCP authentication headers without exposing the token in config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


DEFAULT_TOKEN_FILE = Path.home() / ".config" / "blun-language-guard" / "mcp-http.token"


def load_token(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"MCP access-token file does not exist: {path}")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError(f"MCP access-token permissions must be owner-only: {path}")
    token = path.read_text(encoding="utf-8-sig").strip()
    if len(token) < 32:
        raise RuntimeError("MCP access token must contain at least 32 characters")
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
