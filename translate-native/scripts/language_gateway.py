#!/usr/bin/env python3
"""Installed-skill entry point for the fail-closed BLUN Language Gateway."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


GUARD_PATH = Path(__file__).with_name("blun_language_guard.py")
SPEC = importlib.util.spec_from_file_location("blun_installed_gateway_guard", GUARD_PATH)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


def main() -> int:
    parser = argparse.ArgumentParser(description="BLUN fail-closed language output gateway")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    try:
        request = json.loads(args.input.read_text(encoding="utf-8-sig") if args.input else sys.stdin.read().lstrip("\ufeff"))
        missing = [key for key in ("source_text", "target_text", "language") if not request.get(key)]
        result = ({"status": "BLOCK", "release_allowed": False, "reason": "missing-fields", "fields": missing}
                  if missing else GUARD.release_translation(request))
        result["gateway"] = "blun-language-gateway/5.1"
    except (OSError, json.JSONDecodeError) as error:
        result = {"status": "BLOCK", "release_allowed": False, "reason": "invalid-input", "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("release_allowed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
