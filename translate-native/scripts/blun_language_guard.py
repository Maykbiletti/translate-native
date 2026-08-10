#!/usr/bin/env python3
"""BLUN Language Guard: zero-dependency CLI and MCP release gate."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import secrets
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DIACRITICS_PATH = ROOT / "translate-native" / "scripts" / "check_diacritics.py"
VERSION = "3.0.0"
PROTOCOL_VERSION = "2025-06-18"


def _load_diacritics_module():
    spec = importlib.util.spec_from_file_location("blun_check_diacritics", DIACRITICS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the bundled diacritics checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DIACRITICS = _load_diacritics_module()
FORBIDDEN_CONTROLS = {
    "\u202a": "LEFT-TO-RIGHT EMBEDDING",
    "\u202b": "RIGHT-TO-LEFT EMBEDDING",
    "\u202d": "LEFT-TO-RIGHT OVERRIDE",
    "\u202e": "RIGHT-TO-LEFT OVERRIDE",
    "\u2066": "LEFT-TO-RIGHT ISOLATE",
    "\u2067": "RIGHT-TO-LEFT ISOLATE",
    "\u2068": "FIRST STRONG ISOLATE",
    "\u2069": "POP DIRECTIONAL ISOLATE",
}


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    blocking: bool = True
    line: int | None = None
    language: str | None = None


def _languages_for(text: str, language: str) -> tuple[str, ...]:
    if language == "all":
        return tuple(DIACRITICS.RULES)
    if language == "auto":
        return DIACRITICS.detect_languages(text)
    base = language.casefold().split("-", 1)[0].split("_", 1)[0]
    return (base,) if base in DIACRITICS.RULES else ()


def validate_text(text: str, language: str = "auto") -> dict[str, Any]:
    findings: list[Finding] = []
    if not text.strip():
        findings.append(Finding("empty-target", "Target text is empty."))
    if text != unicodedata.normalize("NFC", text):
        findings.append(Finding("unicode-not-nfc", "Target text is not NFC-normalized."))
    if "\ufffd" in text:
        findings.append(Finding("replacement-character", "Target contains U+FFFD replacement characters."))
    if "\x00" in text:
        findings.append(Finding("nul-character", "Target contains a NUL character."))

    for character, name in FORBIDDEN_CONTROLS.items():
        if character in text:
            findings.append(
                Finding(
                    "unreviewed-bidi-control",
                    f"Target contains {name}; remove it unless the format explicitly requires it.",
                )
            )

    prose = DIACRITICS.mask_technical_text(text)
    for line, code, found, suggestion in DIACRITICS.iter_findings(
        prose, _languages_for(prose, language)
    ):
        findings.append(
            Finding(
                "suspected-ascii-substitution",
                f"{found!r} may require native spelling {suggestion!r}.",
                line=line,
                language=code,
            )
        )

    return {
        "status": "BLOCK" if findings else "PASS",
        "release_allowed": not findings,
        "language": language,
        "checks": [
            "non-empty",
            "unicode-nfc",
            "encoding-integrity",
            "bidi-control-safety",
            "native-diacritics-heuristics",
        ],
        "findings": [asdict(finding) for finding in findings],
        "limitations": (
            "Deterministic checks cannot prove semantic fidelity or native fluency. "
            "The release gate therefore also requires explicit seven-pass attestations."
        ),
    }


def release_translation(arguments: dict[str, Any]) -> dict[str, Any]:
    source = arguments.get("source_text", "")
    target = arguments.get("target_text", "")
    language = arguments.get("language", "auto")
    attestations = arguments.get("attestations") or {}
    required = (
        "meaning",
        "completeness",
        "precision",
        "nativeness",
        "locale_fit",
        "integrity",
        "orthography",
    )
    report = validate_text(target, language)
    missing = [name for name in required if attestations.get(name) is not True]
    if not source.strip():
        report["findings"].append(
            asdict(Finding("empty-source", "Source text is required for the fidelity gate."))
        )
    if missing:
        report["findings"].append(
            asdict(
                Finding(
                    "missing-attestations",
                    "The following release checks were not explicitly passed: "
                    + ", ".join(missing),
                )
            )
        )
    report["status"] = "BLOCK" if report["findings"] else "PASS"
    report["release_allowed"] = not report["findings"]
    report["required_attestations"] = list(required)
    if report["release_allowed"]:
        digest = hashlib.sha256(
            (source + "\0" + target + "\0" + language).encode("utf-8")
        ).hexdigest()[:16]
        report["release_token"] = f"blun-lg-{VERSION}-{digest}-{secrets.token_hex(4)}"
    return report


TOOLS = [
    {
        "name": "validate_text",
        "description": "Run deterministic Unicode, script-safety, and native-diacritics checks on target-language text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "language": {"type": "string", "default": "auto"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "release_translation",
        "description": "Mandatory final gate. Returns a release token only after deterministic validation and all seven quality attestations pass.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_text": {"type": "string"},
                "target_text": {"type": "string"},
                "language": {"type": "string"},
                "attestations": {
                    "type": "object",
                    "properties": {
                        name: {"type": "boolean"}
                        for name in (
                            "meaning",
                            "completeness",
                            "precision",
                            "nativeness",
                            "locale_fit",
                            "integrity",
                            "orthography",
                        )
                    },
                    "required": [
                        "meaning",
                        "completeness",
                        "precision",
                        "nativeness",
                        "locale_fit",
                        "integrity",
                        "orthography",
                    ],
                    "additionalProperties": False,
                },
            },
            "required": ["source_text", "target_text", "language", "attestations"],
            "additionalProperties": False,
        },
    },
]


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": payload.get("status") == "BLOCK",
    }


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "blun-language-guard", "version": VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "validate_text":
            payload = validate_text(arguments.get("text", ""), arguments.get("language", "auto"))
        elif name == "release_translation":
            payload = release_translation(arguments)
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": _tool_result(payload)}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def serve() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle_message(json.loads(line))
        except Exception as error:  # Keep the MCP process alive after malformed input.
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(error)},
            }
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BLUN Language Guard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the MCP server over stdio")
    validate = subparsers.add_parser("validate", help="Validate text from a file or stdin")
    validate.add_argument("path", nargs="?", type=Path)
    validate.add_argument("--language", default="auto")
    args = parser.parse_args()
    if args.command == "serve":
        return serve()
    text = args.path.read_text(encoding="utf-8") if args.path else sys.stdin.read()
    report = validate_text(text, args.language)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["release_allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
