#!/usr/bin/env python3
"""Portable fail-closed hook: verify a receipt supplied as JSON on stdin."""

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
        request = json.load(sys.stdin)
        key_path = Path(os.environ.get("BLUN_LANGUAGE_GUARD_KEY_FILE", Path.home() / ".config" / "blun-language-guard" / "signing.key"))
        result = QUALITY.verify_receipt(
            request["release_token"], request["source_text"], request["target_text"],
            request["language"], QUALITY.load_or_create_key(key_path),
        )
    except (KeyError, json.JSONDecodeError, OSError) as error:
        print(json.dumps({"allow": False, "error": str(error)}))
        return 1
    print(json.dumps({"allow": result["valid"], "verification": result}))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
