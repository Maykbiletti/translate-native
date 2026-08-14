#!/usr/bin/env python3
"""BLUN Language Guard: zero-dependency CLI and MCP release gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DIACRITICS_PATH = Path(__file__).with_name("check_diacritics.py")
VERSION = "6.12.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", PROTOCOL_VERSION}
EXACT_LANGUAGE_TAG = re.compile(r"^(?:[A-Za-z]{2,8}|x)(?:-[A-Za-z0-9]{1,8})*$")
MCP_INSTRUCTIONS = (
    "Treat every user-visible natural-language answer as an untrusted candidate. "
    "Before delivery, call release_response with the complete answer and exact language tag. "
    "For every translation, localization, transcreation, or target-language rewrite, first apply "
    "the installed translate-native skill/plugin and then call release_translation with the complete "
    "source-target pair and truthful seven-pass attestations. Never use release_response to bypass "
    "the translation gate. Release only after the exact current text receives a valid token. "
    "When BLUN_LANGUAGE_GUARD_MANDATORY=1, final stdout must be exactly one JSON object containing "
    "only target_text and release_token; never call a delivery channel directly or include host-owned policy fields."
)


def _load_diacritics_module():
    spec = importlib.util.spec_from_file_location("blun_check_diacritics", DIACRITICS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the bundled diacritics checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DIACRITICS = _load_diacritics_module()


def _load_quality_module():
    path = Path(__file__).with_name("language_quality.py")
    spec = importlib.util.spec_from_file_location("blun_language_quality", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load language quality primitives")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


QUALITY = _load_quality_module()


def _load_translation_module():
    path = Path(__file__).with_name("translation_guard.py")
    spec = importlib.util.spec_from_file_location("blun_translation_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load translation integrity primitives")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRANSLATION = _load_translation_module()


def _load_service_client():
    path = Path(__file__).with_name("guard_service_client.py")
    spec = importlib.util.spec_from_file_location("blun_guard_service_client", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load guard service client")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVICE_CLIENT = _load_service_client()
VERSION = QUALITY.VERSION
KEY_PATH = Path(os.environ.get("BLUN_LANGUAGE_GUARD_KEY_FILE", Path.home() / ".config" / "blun-language-guard" / "signing.key"))
SERVICE_ENDPOINT = os.environ.get("BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT", "").strip()
SERVICE_TOKEN_FILE = os.environ.get("BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE", "").strip()
LANGUAGE_CHARACTER_PROFILES = {
    "sv": set("åäöÅÄÖ"),
    "de": set("äöüßÄÖÜẞ"),
    "es": set("áéíóúüñ¿¡ÁÉÍÓÚÜÑ"),
    "cs": set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"),
    "ca": set("àçèéíïòóúü·ÀÇÈÉÍÏÒÓÚÜ"),
}
ASCII_FOLDING_PROFILES = {
    # Conventional transliterations whose density is measurable without a dictionary.
    # Thresholds deliberately avoid treating one ordinary letter sequence as proof.
    "de": {"patterns": (r"ae", r"oe", r"ue"), "native": "äöüÄÖÜ", "minimum": 3},
    "sv": {"patterns": (r"aa", r"ae", r"oe"), "native": "åäöÅÄÖ", "minimum": 1},
    "da": {"patterns": (r"aa", r"ae", r"oe"), "native": "åæøÅÆØ", "minimum": 1},
    "no": {"patterns": (r"aa", r"ae", r"oe"), "native": "åæøÅÆØ", "minimum": 1},
}


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    blocking: bool = True
    line: int | None = None
    language: str | None = None


def _service_token() -> str:
    direct = os.environ.get("BLUN_LANGUAGE_GUARD_SERVICE_TOKEN", "").strip()
    if direct:
        return direct
    if not SERVICE_TOKEN_FILE:
        return ""
    token = Path(SERVICE_TOKEN_FILE).read_text(encoding="utf-8-sig").strip()
    if len(token) < 32:
        raise SERVICE_CLIENT.GuardServiceError("guard service token is invalid")
    return token


def _isolated_release(task_kind: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if not SERVICE_ENDPOINT:
        return None
    request = dict(arguments)
    request.update({
        "operation": "release",
        "task_kind": task_kind,
        "agent_id": os.environ.get("BLUN_LANGUAGE_GUARD_AGENT_ID", ""),
        "channel": os.environ.get("BLUN_LANGUAGE_GUARD_CHANNEL", "mcp"),
    })
    if task_kind == "response":
        request["source_text"] = ""
    try:
        return SERVICE_CLIENT.call_guard_service(
            SERVICE_ENDPOINT,
            request,
            auth_token=_service_token(),
        )
    except (OSError, SERVICE_CLIENT.GuardServiceError) as error:
        return {
            "status": "BLOCK",
            "release_allowed": False,
            "reason": "isolated-guard-unavailable",
            "error": str(error),
        }


def _languages_for(text: str, language: str) -> tuple[str, ...]:
    if language == "all":
        return tuple(DIACRITICS.RULES)
    if language == "auto":
        return DIACRITICS.detect_languages(text)
    base = language.casefold().split("-", 1)[0].split("_", 1)[0]
    return (base,) if base in DIACRITICS.RULES else ()


def validate_text(
    text: str,
    language: str = "auto",
    glossary: dict[str, Any] | None = None,
    content_type: str = "prose",
    short_text_reviewed: bool = False,
) -> dict[str, Any]:
    findings: list[Finding] = []
    if not text.strip():
        findings.append(Finding("empty-target", "Target text is empty."))
    if text != unicodedata.normalize("NFC", text):
        findings.append(Finding("unicode-not-nfc", "Target text is not NFC-normalized."))
    if "\ufffd" in text:
        findings.append(Finding("replacement-character", "Target contains U+FFFD replacement characters."))
    if "\x00" in text:
        findings.append(Finding("nul-character", "Target contains a NUL character."))

    for bidi in QUALITY.bidi_findings(text):
        findings.append(Finding(bidi["code"], json.dumps(bidi, ensure_ascii=False)))

    script = QUALITY.script_report(text, language)
    if script.get("status") == "fail":
        findings.append(Finding("script-mismatch", json.dumps(script, ensure_ascii=False), language=language))
    base_language = language.casefold().replace("_", "-").split("-", 1)[0]
    profile = LANGUAGE_CHARACTER_PROFILES.get(base_language)
    profile_prose = DIACRITICS.mask_technical_text(text)
    if profile and len(profile_prose) >= 200 and not any(character in profile for character in profile_prose):
        findings.append(Finding(
            "missing-language-character-profile",
            f"Long {base_language} text contains none of the language's characteristic native characters; possible wholesale ASCII folding.",
            language=language,
        ))
    folding_profile = ASCII_FOLDING_PROFILES.get(base_language)
    if folding_profile:
        folded = sum(len(re.findall(pattern, profile_prose, re.IGNORECASE)) for pattern in folding_profile["patterns"])
        native = sum(profile_prose.count(character) for character in folding_profile["native"])
        if folded >= folding_profile["minimum"] and folded > native:
            findings.append(Finding(
                "ascii-folding-pressure",
                f"Measured ASCII-folding candidates ({folded}) exceed native characters ({native}); review the exact spelling.",
                language=language,
            ))
    # Kept as compatibility metadata only. It never suppresses a measurable finding.
    short_sensitive = content_type in {"title", "meta_description", "ui"} and len(profile_prose.strip()) < 200
    if short_sensitive and not short_text_reviewed and not findings:
        findings.append(Finding(
            "short-text-native-review-required",
            f"Short {content_type} text needs host-enforced review; an MCP Boolean is not independent proof.",
            language=language,
        ))
    for glossary_finding in QUALITY.glossary_findings(
        text, glossary if isinstance(glossary, dict) else {}
    ):
        findings.append(Finding(glossary_finding["code"], json.dumps(glossary_finding, ensure_ascii=False), language=language))

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
        "status": (
            "REVIEW_REQUIRED"
            if findings and all(finding.code == "short-text-native-review-required" for finding in findings)
            else "BLOCK" if findings else "PASS"
        ),
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
    isolated = _isolated_release("translation", arguments)
    if isolated is not None:
        return isolated
    source = arguments.get("source_text", "")
    target = arguments.get("target_text", "")
    language = arguments.get("language", "auto")
    source_is_text = isinstance(source, str)
    target_is_text = isinstance(target, str)
    language_is_exact = (
        isinstance(language, str)
        and language.casefold() not in {"auto", "all"}
        and EXACT_LANGUAGE_TAG.fullmatch(language) is not None
    )
    source = source if source_is_text else ""
    target = target if target_is_text else ""
    language = language if isinstance(language, str) else ""
    attestations = arguments.get("attestations") or {}
    if not isinstance(attestations, dict):
        attestations = {}
    required = (
        "meaning",
        "completeness",
        "precision",
        "nativeness",
        "locale_fit",
        "integrity",
        "orthography",
    )
    report = validate_text(
        target,
        language,
        arguments.get("glossary"),
        arguments.get("content_type", "prose"),
        arguments.get("short_text_reviewed") is True,
    )
    report["checks"].extend([
        "source-target-identity",
        "structured-segment-identity",
        "translation-volume-integrity",
    ])
    if not source_is_text:
        report["findings"].append(
            asdict(Finding("invalid-source-type", "source_text must be a string."))
        )
    if not target_is_text:
        report["findings"].append(
            asdict(Finding("invalid-target-type", "target_text must be a string."))
        )
    if not language_is_exact:
        report["findings"].append(
            asdict(Finding(
                "exact-language-required",
                "A host-supplied exact language or locale tag is required for translation release.",
            ))
        )
    missing = [name for name in required if attestations.get(name) is not True]
    if not source.strip():
        report["findings"].append(
            asdict(Finding("empty-source", "Source text is required for the fidelity gate."))
        )
    else:
        whole_identity_errors = TRANSLATION.identity_errors(source, target)
        for error in whole_identity_errors:
            report["findings"].append(
                asdict(Finding("source-target-identical", error))
            )
        if not whole_identity_errors:
            selected_format = TRANSLATION.detect_content_format(source)
            for error in TRANSLATION.structured_identity_errors(
                source, target, selected_format
            ):
                report["findings"].append(
                    asdict(Finding("unchanged-linguistic-segment", error))
                )
        for error in TRANSLATION.translation_volume_errors(source, target):
            report["findings"].append(
                asdict(Finding("translation-volume-integrity", error))
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
    review_only = report["findings"] and all(
        finding.get("code") == "short-text-native-review-required" for finding in report["findings"]
    )
    report["status"] = "REVIEW_REQUIRED" if review_only else "BLOCK" if report["findings"] else "PASS"
    report["release_allowed"] = not report["findings"]
    report["required_attestations"] = list(required)
    if report["release_allowed"]:
        key = QUALITY.load_or_create_key(KEY_PATH)
        report["release_token"] = QUALITY.issue_receipt(
            source, target, language, key,
            content_type=arguments.get("content_type", "prose"),
            short_text_reviewed=arguments.get("short_text_reviewed") is True,
            purpose="translation",
        )
    return report


def release_response(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate an agent's own final answer and bind a receipt to the exact text."""
    isolated = _isolated_release("response", arguments)
    if isolated is not None:
        return isolated
    target = arguments.get("target_text", "")
    language = arguments.get("language", "")
    attestations = arguments.get("attestations") or {}
    if not isinstance(attestations, dict):
        attestations = {}
    target_is_text = isinstance(target, str)
    language_is_exact = (
        isinstance(language, str)
        and language.casefold() not in {"auto", "all"}
        and EXACT_LANGUAGE_TAG.fullmatch(language) is not None
    )
    report = validate_text(
        target if target_is_text else "",
        language if isinstance(language, str) else "",
        arguments.get("glossary"),
        arguments.get("content_type", "prose"),
        arguments.get("short_text_reviewed") is True,
    )
    report["checks"].append("agent-response-native-orthography")
    if not target_is_text:
        report["findings"].append(
            asdict(Finding("invalid-target-type", "target_text must be a string."))
        )
    if not language_is_exact:
        report["findings"].append(
            asdict(Finding(
                "exact-language-required",
                "A host-supplied exact language or locale tag is required for response release.",
            ))
        )
    missing = [name for name in ("nativeness", "orthography") if attestations.get(name) is not True]
    if missing:
        report["findings"].append(
            asdict(Finding(
                "missing-response-attestations",
                "The following response checks were not explicitly passed: " + ", ".join(missing),
            ))
        )
    report["status"] = "BLOCK" if report["findings"] else "PASS"
    report["release_allowed"] = not report["findings"]
    report["required_attestations"] = ["nativeness", "orthography"]
    report["limitations"] = (
        "Deterministic checks cannot prove that every word is native or correctly accented. "
        "Response release also requires nativeness and orthography review plus a trusted host interceptor."
    )
    if report["release_allowed"]:
        key = QUALITY.load_or_create_key(KEY_PATH)
        report["release_token"] = QUALITY.issue_receipt(
            "", target, language, key,
            content_type=arguments.get("content_type", "prose"),
            short_text_reviewed=arguments.get("short_text_reviewed") is True,
            purpose="response",
        )
    return report


TOOLS = [
    {
        "name": "verify_release_token",
        "description": "Cryptographically verify that a BLUN release receipt is authentic, unexpired, and bound to the exact purpose, source when applicable, target, locale, and guard version. Never accept a receipt based on its appearance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "release_token": {"type": "string"},
                "source_text": {"type": "string"},
                "target_text": {"type": "string"},
                "language": {"type": "string"},
                "purpose": {"type": "string", "enum": ["translation", "response"], "default": "translation"},
                "content_type": {"type": "string", "enum": ["prose", "title", "meta_description", "ui"], "default": "prose"},
                "short_text_reviewed": {"type": "boolean", "default": False},
            },
            "required": ["release_token", "source_text", "target_text", "language"],
            "additionalProperties": False,
        },
    },
    {
        "name": "release_response",
        "description": "Mandatory final gate for an agent's own user-visible natural-language answer. Returns a purpose-bound token only after deterministic Unicode, script, native-diacritics, and explicit nativeness/orthography checks pass. Never use this tool for a translation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_text": {"type": "string"},
                "language": {"type": "string", "description": "Exact BCP 47 language or locale tag supplied by the host; auto and all are rejected."},
                "glossary": {"type": "object"},
                "content_type": {"type": "string", "enum": ["prose", "title", "meta_description", "ui"], "default": "prose"},
                "short_text_reviewed": {"type": "boolean", "description": "Compatibility metadata only; never suppresses measurable findings."},
                "attestations": {
                    "type": "object",
                    "properties": {
                        "nativeness": {"type": "boolean"},
                        "orthography": {"type": "boolean"},
                    },
                    "required": ["nativeness", "orthography"],
                    "additionalProperties": False,
                },
            },
            "required": ["target_text", "language", "attestations"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_text",
        "description": "Run deterministic Unicode, script-safety, and native-diacritics checks on target-language text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "language": {"type": "string", "default": "auto"},
                "glossary": {"type": "object", "description": "Optional source-term to required target-term or regex-rule map."},
                "content_type": {"type": "string", "enum": ["prose", "title", "meta_description", "ui"], "default": "prose"},
                "short_text_reviewed": {"type": "boolean", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "release_translation",
        "description": "Mandatory final gate. Returns a release token only after deterministic validation, whole-input and structured-segment source-target non-identity, auto-detected translation-volume integrity, and all seven quality attestations pass.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_text": {"type": "string"},
                "target_text": {"type": "string"},
                "language": {"type": "string"},
                "content_type": {"type": "string", "enum": ["prose", "title", "meta_description", "ui"], "default": "prose"},
                "short_text_reviewed": {"type": "boolean", "description": "Compatibility metadata only. Never suppresses measurable findings and is not independent proof."},
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
        "isError": payload.get("status") != "PASS",
    }


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        requested_protocol = params.get("protocolVersion")
        negotiated_protocol = (
            requested_protocol
            if isinstance(requested_protocol, str) and requested_protocol in SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": negotiated_protocol,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "blun-language-guard", "version": VERSION},
                "instructions": MCP_INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"prompts": [{
                "name": "translate-native",
                "title": "Translate Native mandatory workflow",
                "description": "Load the native translation workflow before drafting any translation.",
                "arguments": [],
            }]},
        }
    if method == "prompts/get":
        params = message.get("params") or {}
        if params.get("name") != "translate-native":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Unknown prompt"},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "description": "Mandatory native translation and orthography workflow.",
                "messages": [{
                    "role": "user",
                    "content": {"type": "text", "text": MCP_INSTRUCTIONS},
                }],
            },
        }
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "validate_text":
            payload = validate_text(
                arguments.get("text", ""), arguments.get("language", "auto"), arguments.get("glossary"),
                arguments.get("content_type", "prose"), arguments.get("short_text_reviewed") is True,
            )
        elif name == "release_translation":
            payload = release_translation(arguments)
        elif name == "release_response":
            payload = release_response(arguments)
        elif name == "verify_release_token":
            if SERVICE_ENDPOINT:
                try:
                    payload = SERVICE_CLIENT.call_guard_service(
                        SERVICE_ENDPOINT,
                        {
                            "operation": "verify",
                            "task_kind": arguments.get("purpose", "translation"),
                            "source_text": arguments.get("source_text", ""),
                            "target_text": arguments.get("target_text", ""),
                            "language": arguments.get("language", ""),
                            "release_token": arguments.get("release_token", ""),
                            "content_type": arguments.get("content_type", "prose"),
                            "short_text_reviewed": arguments.get("short_text_reviewed") is True,
                            "agent_id": os.environ.get("BLUN_LANGUAGE_GUARD_AGENT_ID", ""),
                            "channel": os.environ.get("BLUN_LANGUAGE_GUARD_CHANNEL", "mcp"),
                        },
                        auth_token=_service_token(),
                    )
                except (OSError, SERVICE_CLIENT.GuardServiceError) as error:
                    payload = {"valid": False, "status": "BLOCK", "error": str(error)}
            else:
                payload = QUALITY.verify_receipt(
                    arguments.get("release_token", ""),
                    arguments.get("source_text", ""),
                    arguments.get("target_text", ""),
                    arguments.get("language", ""),
                    QUALITY.load_or_create_key(KEY_PATH),
                    arguments.get("content_type", "prose"),
                    arguments.get("short_text_reviewed") is True,
                    arguments.get("purpose", "translation"),
                )
            payload["status"] = "PASS" if payload.get("valid") else "BLOCK"
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
            response = handle_message(json.loads(line.lstrip("\ufeff")))
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
