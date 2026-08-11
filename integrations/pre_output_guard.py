#!/usr/bin/env python3
"""Portable fail-closed hook: verify a current V6 receipt supplied as JSON on stdin."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATH = ROOT / "translate-native" / "scripts" / "language_quality.py"
SPEC = importlib.util.spec_from_file_location("blun_hook_quality", QUALITY_PATH)
assert SPEC and SPEC.loader
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)


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
            request["language"], QUALITY.load_or_create_key(key_path),
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
