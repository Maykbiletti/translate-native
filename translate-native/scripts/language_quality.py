#!/usr/bin/env python3
"""Language identity, bidi, glossary, and signed release-receipt primitives."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
import time
import unicodedata
from pathlib import Path
from typing import Any


VERSION = "6.20.0"
MAX_SIGNING_KEY_BYTES = 64 * 1024
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


def _validate_signing_key_stat(details: os.stat_result) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError("signing key must be a regular file")
    if details.st_nlink != 1:
        raise ValueError("signing key must have exactly one hard link")
    if details.st_size < 32 or details.st_size > MAX_SIGNING_KEY_BYTES:
        raise ValueError("signing key has an invalid size")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("signing key permissions must be owner-only")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ValueError("signing key has the wrong owner")


def _signing_key_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _signing_key_directory_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _validate_signing_key_directory(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError(f"signing-key directory is not a directory: {path}")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise ValueError(f"signing-key directory is writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ValueError(f"signing-key directory has the wrong owner: {path}")


def _assert_signing_key_directory_unchanged(
    path: Path, expected: tuple[int, int, int, int, int],
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise ValueError(f"signing-key directory cannot be rechecked: {path}") from error
    _validate_signing_key_directory(path, current)
    if _signing_key_directory_identity(current) != expected:
        raise ValueError(f"signing-key directory changed during operation: {path}")


def _existing_signing_key_directory_anchor(path: Path) -> Path:
    candidate = path
    while True:
        try:
            candidate.lstat()
            return candidate
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise ValueError(f"signing-key directory has no existing anchor: {path}")
            candidate = parent


@contextlib.contextmanager
def _open_signing_key_directory(path: Path):
    if os.name == "nt":
        path.parent.mkdir(parents=True, exist_ok=True)
        yield None
        return
    anchor = Path.home()
    try:
        relative = path.parent.relative_to(anchor)
    except ValueError:
        anchor = _existing_signing_key_directory_anchor(path.parent)
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
            _validate_signing_key_directory(
                anchor, os.fstat(descriptor),
            )
            for component in relative.parts:
                current_path = current_path / component
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=descriptor)
                try:
                    _validate_signing_key_directory(
                        current_path, os.fstat(child),
                    )
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            expected = _signing_key_directory_identity(
                os.fstat(descriptor),
            )
        except ValueError:
            raise
        except OSError as error:
            raise ValueError(
                f"cannot safely open signing-key directory: {current_path}"
            ) from error
        try:
            yield descriptor
        finally:
            _assert_signing_key_directory_unchanged(path.parent, expected)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _signing_key_lstat(path: Path, directory: int | None) -> os.stat_result:
    if directory is None:
        return os.lstat(path)
    return os.stat(path.name, dir_fd=directory, follow_symlinks=False)


def _open_signing_key_file(
    path: Path, directory: int | None, flags: int, mode: int = 0o600,
) -> int:
    if directory is None:
        return os.open(path, flags, mode)
    return os.open(path.name, flags, mode, dir_fd=directory)


def _signing_key_fstat(descriptor: int) -> os.stat_result:
    return os.fstat(descriptor)


def _load_existing_key_at(path: Path, directory: int | None) -> bytes:
    before = _signing_key_lstat(path, directory)
    _validate_signing_key_stat(before)
    descriptor = _open_signing_key_file(
        path,
        directory,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = _signing_key_fstat(descriptor)
        _validate_signing_key_stat(opened)
        if _signing_key_identity(opened) != _signing_key_identity(before):
            raise ValueError("signing key changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            key = handle.read(MAX_SIGNING_KEY_BYTES + 1)
        after = _signing_key_fstat(descriptor)
        if _signing_key_identity(after) != _signing_key_identity(opened):
            raise ValueError("signing key changed while reading")
    finally:
        os.close(descriptor)
    if len(key) < 32 or len(key) > MAX_SIGNING_KEY_BYTES:
        raise ValueError("signing key has an invalid size")
    return key


def load_existing_key(path: Path) -> bytes:
    with _open_signing_key_directory(path) as directory:
        return _load_existing_key_at(path, directory)


def load_or_create_key(path: Path) -> bytes:
    env_key = os.environ.get("BLUN_LANGUAGE_GUARD_KEY")
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()
    with _open_signing_key_directory(path) as directory:
        try:
            return _load_existing_key_at(path, directory)
        except FileNotFoundError:
            pass
        key = os.urandom(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = _open_signing_key_file(path, directory, flags)
        except FileExistsError:
            return _load_existing_key_at(path, directory)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(key)
                handle.flush()
                os.fsync(descriptor)
            opened = _signing_key_fstat(descriptor)
            _validate_signing_key_stat(opened)
            installed = _signing_key_lstat(path, directory)
            _validate_signing_key_stat(installed)
            if _signing_key_identity(installed) != _signing_key_identity(opened):
                raise ValueError("signing key changed while creating")
        finally:
            os.close(descriptor)
        return key


def issue_receipt(
    source: str, target: str, language: str, key: bytes, ttl: int = 86400,
    content_type: str = "prose", short_text_reviewed: bool = False,
    purpose: str = "translation",
) -> str:
    now = int(time.time())
    payload = {
        "v": VERSION,
        "source_sha256": canonical_hash(source),
        "target_sha256": canonical_hash(target),
        "language": language,
        "purpose": purpose,
        "content_type": content_type,
        "short_text_reviewed": short_text_reviewed,
        "iat": now,
        "exp": now + max(60, min(ttl, 604800)),
        "nonce": _b64encode(os.urandom(12)),
    }
    encoded = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64encode(hmac.new(key, encoded.encode(), hashlib.sha256).digest())
    return f"blg6.{encoded}.{signature}"


def verify_receipt(
    token: str, source: str, target: str, language: str, key: bytes,
    content_type: str = "prose", short_text_reviewed: bool = False,
    purpose: str = "translation",
) -> dict[str, Any]:
    try:
        prefix, encoded, signature = token.split(".")
        expected = _b64encode(hmac.new(key, encoded.encode(), hashlib.sha256).digest())
        if prefix != "blg6" or not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload = json.loads(_b64decode(encoded))
        checks = {
            "source": payload.get("source_sha256") == canonical_hash(source),
            "target": payload.get("target_sha256") == canonical_hash(target),
            "language": payload.get("language") == language,
            "purpose": payload.get("purpose") == purpose,
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
