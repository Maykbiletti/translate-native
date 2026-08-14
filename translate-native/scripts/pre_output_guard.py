#!/usr/bin/env python3
"""Installed-skill hook that verifies an exact current V6 release receipt."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path


QUALITY_PATH = Path(__file__).with_name("language_quality.py")
SPEC = importlib.util.spec_from_file_location("blun_installed_hook_quality", QUALITY_PATH)
assert SPEC and SPEC.loader
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)


def load_verification_key(path: Path) -> bytes:
    """Load an existing verifier key without creating or replacing trust state."""
    environment_key = os.environ.get("BLUN_LANGUAGE_GUARD_KEY")
    if environment_key:
        return hashlib.sha256(environment_key.encode("utf-8")).digest()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if os.name != "nt" and mode & 0o077:
            raise ValueError("signing key permissions are broader than owner-only")
        key = path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError("signing key is missing; verification fails closed") from error
    except OSError as error:
        raise ValueError("signing key cannot be read") from error
    if len(key) < 32:
        raise ValueError("signing key is invalid")
    return key


def main() -> int:
    try:
        request = json.loads(sys.stdin.read().lstrip("\ufeff"))
        task_kind = request["task_kind"]
        source = request.get("source_text", "")
        if task_kind not in {"translation", "response"}:
            raise ValueError("task_kind must be translation or response")
        if task_kind == "translation" and not source.strip():
            raise ValueError("translation receipts require source_text")
        if task_kind == "response" and source.strip():
            raise ValueError("response receipts cannot carry source_text")
        key_path = Path(os.environ.get("BLUN_LANGUAGE_GUARD_KEY_FILE", Path.home() / ".config" / "blun-language-guard" / "signing.key"))
        result = QUALITY.verify_receipt(
            request["release_token"], source, request["target_text"],
            request["language"], load_verification_key(key_path),
            request.get("content_type", "prose"), request.get("short_text_reviewed") is True,
            purpose=task_kind,
        )
    except (KeyError, ValueError, json.JSONDecodeError, OSError) as error:
        print(json.dumps({"allow": False, "error": str(error)}))
        return 1
    print(json.dumps({"allow": result["valid"], "verification": result}))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
