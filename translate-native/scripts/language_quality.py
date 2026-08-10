#!/usr/bin/env python3
"""Language identity, bidi, glossary, and signed release-receipt primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any


VERSION = "5.3.2"
DANGEROUS_BIDI = {"\u202a", "\u202b", "\u202c", "\u202d", "\u202e"}
ISOLATE_OPENERS = {"\u2066", "\u2067", "\u2068"}
ISOLATE_CLOSER = "\u2069"
SCRIPT_RANGES = {
    "Cyrl": ((0x0400, 0x052F),),
    "Grek": ((0x0370, 0x03FF),),
    "Arab": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF)),
    "Hebr": ((0x0590, 0x05FF),),
    "Hang": ((0xAC00, 0xD7AF),),
    "Hani": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF)),
    "Jpan": ((0x3040, 0x30FF), (0x3400, 0x9FFF)),
    "Latn": ((0x0041, 0x007A), (0x00C0, 0x024F)),
}
LANGUAGE_SCRIPTS = {
    "ar": "Arab", "fa": "Arab", "ur": "Arab", "he": "Hebr",
    "el": "Grek", "ru": "Cyrl", "uk": "Cyrl", "bg": "Cyrl",
    "mk": "Cyrl", "ko": "Hang", "ja": "Jpan", "zh": "Hani",
}


def canonical_text(text: str) -> str:
    """Normalize transport-only differences without hiding character corruption."""
    return unicodedata.normalize("NFC", text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n"))


def canonical_hash(text: str) -> str:
    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def load_or_create_key(path: Path) -> bytes:
    env_key = os.environ.get("BLUN_LANGUAGE_GUARD_KEY")
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes()
    key = os.urandom(32)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(key)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return key


def issue_receipt(
    source: str, target: str, language: str, key: bytes, ttl: int = 86400,
    content_type: str = "prose", short_text_reviewed: bool = False,
) -> str:
    now = int(time.time())
    payload = {
        "v": VERSION,
        "source_sha256": canonical_hash(source),
        "target_sha256": canonical_hash(target),
        "language": language,
        "content_type": content_type,
        "short_text_reviewed": short_text_reviewed,
        "iat": now,
        "exp": now + max(60, min(ttl, 604800)),
        "nonce": _b64encode(os.urandom(12)),
    }
    encoded = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64encode(hmac.new(key, encoded.encode(), hashlib.sha256).digest())
    return f"blg5.{encoded}.{signature}"


def verify_receipt(
    token: str, source: str, target: str, language: str, key: bytes,
    content_type: str = "prose", short_text_reviewed: bool = False,
) -> dict[str, Any]:
    try:
        prefix, encoded, signature = token.split(".")
        expected = _b64encode(hmac.new(key, encoded.encode(), hashlib.sha256).digest())
        if prefix != "blg5" or not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload = json.loads(_b64decode(encoded))
        checks = {
            "source": payload.get("source_sha256") == canonical_hash(source),
            "target": payload.get("target_sha256") == canonical_hash(target),
            "language": payload.get("language") == language,
            "content_type": payload.get("content_type") == content_type,
            "short_text_reviewed": payload.get("short_text_reviewed") is short_text_reviewed,
            "version": payload.get("v") == VERSION,
            "not_expired": int(payload.get("exp", 0)) >= int(time.time()),
        }
        return {"valid": all(checks.values()), "checks": checks, "payload": payload}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
        return {"valid": False, "error": str(error)}


def bidi_findings(text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    stack = 0
    for character in text:
        if character in DANGEROUS_BIDI:
            findings.append({"code": "dangerous-bidi-control", "character": f"U+{ord(character):04X}"})
        elif character in ISOLATE_OPENERS:
            stack += 1
        elif character == ISOLATE_CLOSER:
            if stack == 0:
                findings.append({"code": "unpaired-bidi-isolate", "character": "U+2069"})
            else:
                stack -= 1
    if stack:
        findings.append({"code": "unclosed-bidi-isolate", "count": str(stack)})
    return findings


def _in_script(character: str, script: str) -> bool:
    point = ord(character)
    return any(start <= point <= end for start, end in SCRIPT_RANGES.get(script, ()))


def script_report(text: str, language: str) -> dict[str, Any]:
    parts = language.replace("_", "-").split("-")
    base = parts[0].casefold()
    explicit = next((part.title() for part in parts[1:] if len(part) == 4), None)
    expected = explicit or LANGUAGE_SCRIPTS.get(base)
    if not expected:
        return {"status": "not-evaluated", "reason": "no deterministic script expectation"}
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        return {"status": "fail", "expected_script": expected, "ratio": 0.0}
    matching = sum(_in_script(c, expected) for c in letters)
    ratio = matching / len(letters)
    threshold = 0.45 if expected in {"Hani", "Jpan"} else 0.70
    return {"status": "pass" if ratio >= threshold else "fail", "expected_script": expected, "ratio": round(ratio, 3)}


def glossary_findings(target: str, glossary: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for source_term, rule in glossary.items():
        if isinstance(rule, str):
            pattern, flags = re.escape(rule), re.IGNORECASE
        elif isinstance(rule, dict):
            value = str(rule.get("target", ""))
            pattern = value if rule.get("regex") else re.escape(value)
            flags = 0 if rule.get("case_sensitive") else re.IGNORECASE
        else:
            continue
        if pattern and not re.search(pattern, target, flags):
            findings.append({"code": "missing-glossary-term", "source": source_term, "expected": pattern})
    return findings
