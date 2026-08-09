#!/usr/bin/env python3
"""Check that a translation preserves structure and protected tokens."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


TOKEN_PATTERNS = (
    ("URL", re.compile(r"(?:https?://|mailto:)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")),
    ("email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")),
    ("Markdown destination", re.compile(r"(?<=\]\()[^\s)]+")),
    ("template", re.compile(r"\{\{[^{}]+\}\}|\$\{[^{}]+\}|%\{[^{}]+\}")),
    ("printf", re.compile(r"%(?:\d+\$)?[-+#0 ']*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlLjzt]*[diouxXfFeEgGaAcspn%]")),
    ("XML entity", re.compile(r"&(?:[A-Za-z][A-Za-z0-9]+|#\d+|#x[0-9A-Fa-f]+);")),
    ("HTML tag", re.compile(r"</?[A-Za-z][^<>]*?>")),
    ("escape", re.compile(r"\\(?:[nrtbfv\\\"']|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2})")),
    ("inline code", re.compile(r"(?<!`)`[^`\n]+`(?!`)")),
    ("ICU argument", re.compile(r"\{\s*([A-Za-z_][\w.-]*)\s*(?=[,}])")),
    ("simple placeholder", re.compile(r"\{[A-Za-z_][\w.-]*\}")),
)

FENCED_CODE = re.compile(r"```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~", re.DOTALL)
ICU_HEADER = re.compile(
    r"\{\s*([A-Za-z_][\w.-]*)\s*,\s*(plural|selectordinal|select)\s*,",
    re.IGNORECASE,
)
ICU_SELECTOR_AT_POSITION = re.compile(r"\s*([=\w.-]+)\s*(?=\{)")


def icu_selectors(text: str, start: int) -> list[tuple[str, tuple[int, int]]]:
    """Read top-level selector names from one ICU plural/select expression."""
    selectors: list[tuple[str, tuple[int, int]]] = []
    depth = 0
    position = start
    while position < len(text):
        character = text[position]
        if character == "}" and depth == 0:
            break
        if depth == 0:
            match = ICU_SELECTOR_AT_POSITION.match(text, position)
            if match:
                selectors.append((match.group(1), match.span(1)))
                position = match.end()
                continue
        if character == "{":
            depth += 1
        elif character == "}" and depth:
            depth -= 1
        position += 1
    return selectors


def protected_tokens(text: str) -> Counter[tuple[str, str]]:
    """Return protected token counts, avoiding duplicate nested matches."""
    tokens: Counter[tuple[str, str]] = Counter()
    occupied: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < other_end and end > other_start for other_start, other_end in occupied)

    for match in FENCED_CODE.finditer(text):
        tokens[("fenced code", match.group(0))] += 1
        occupied.append(match.span())

    icu_headers = list(ICU_HEADER.finditer(text))
    for match in icu_headers:
        tokens[("ICU argument", match.group(1))] += 1
        tokens[("ICU formatter", match.group(2).lower())] += 1
        occupied.append(match.span())
        for selector, span in icu_selectors(text, match.end()):
            tokens[("ICU selector", selector)] += 1
            occupied.append(span)
    if icu_headers:
        tokens[("ICU number sign", "#")] += text.count("#")

    for label, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            if overlaps(*match.span()):
                continue
            value = match.group(1) if label == "ICU argument" else match.group(0)
            if label == "URL":
                # Sentence punctuation after a bare URL is not part of the URL.
                value = value.rstrip(".,;:!?")
            tokens[(label, value)] += 1
            occupied.append(match.span())
    return tokens


def compare_tokens(source: str, target: str, location: str) -> list[str]:
    source_tokens = protected_tokens(source)
    target_tokens = protected_tokens(target)
    errors: list[str] = []
    for token, count in (source_tokens - target_tokens).items():
        errors.append(f"{location}: missing {count}× {token[0]} {token[1]!r}")
    for token, count in (target_tokens - source_tokens).items():
        errors.append(f"{location}: added {count}× {token[0]} {token[1]!r}")
    return errors


def scalar_type(value: Any) -> type[Any]:
    # bool is a subclass of int; exact types matter in resource files.
    return type(value)


def compare_json(source: Any, target: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if scalar_type(source) is not scalar_type(target):
        return [f"{path}: type changed from {type(source).__name__} to {type(target).__name__}"]

    if isinstance(source, dict):
        source_keys = set(source)
        target_keys = set(target)
        for key in sorted(source_keys - target_keys):
            errors.append(f"{path}: missing key {key!r}")
        for key in sorted(target_keys - source_keys):
            errors.append(f"{path}: added key {key!r}")
        for key in source.keys() & target.keys():
            errors.extend(compare_json(source[key], target[key], f"{path}.{key}"))
        return errors

    if isinstance(source, list):
        if len(source) != len(target):
            errors.append(f"{path}: array length changed from {len(source)} to {len(target)}")
        for index, (source_item, target_item) in enumerate(zip(source, target)):
            errors.extend(compare_json(source_item, target_item, f"{path}[{index}]"))
        return errors

    if isinstance(source, str):
        return compare_tokens(source, target, path)

    if source != target:
        errors.append(f"{path}: non-string value changed from {source!r} to {target!r}")
    return errors


def read_utf8(path: Path) -> tuple[str | None, list[str]]:
    try:
        return path.read_text(encoding="utf-8"), []
    except UnicodeDecodeError as exc:
        return None, [f"{path}: not valid UTF-8 ({exc})"]
    except OSError as exc:
        return None, [f"{path}: cannot read file ({exc})"]


def normalization_errors(text: str, path: Path) -> list[str]:
    if unicodedata.is_normalized("NFC", text):
        return []
    return [f"{path}: target text is not Unicode NFC-normalized"]


def parse_json(text: str, path: Path) -> tuple[Any | None, list[str]]:
    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        return None, [f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]


def print_errors(errors: Iterable[str]) -> None:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Unicode normalization, structure, and protected tokens in a translation."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--format", choices=("auto", "text", "json"), default="auto")
    args = parser.parse_args()

    source_text, source_errors = read_utf8(args.source)
    target_text, target_errors = read_utf8(args.target)
    errors = source_errors + target_errors
    if errors:
        print_errors(errors)
        return 1
    assert source_text is not None and target_text is not None

    errors.extend(normalization_errors(target_text, args.target))
    selected_format = args.format
    if selected_format == "auto":
        selected_format = "json" if args.source.suffix.lower() == ".json" else "text"

    if selected_format == "json":
        source_data, parse_source_errors = parse_json(source_text, args.source)
        target_data, parse_target_errors = parse_json(target_text, args.target)
        errors.extend(parse_source_errors)
        errors.extend(parse_target_errors)
        if not parse_source_errors and not parse_target_errors:
            errors.extend(compare_json(source_data, target_data))
    else:
        errors.extend(compare_tokens(source_text, target_text, "$"))

    if errors:
        print_errors(errors)
        return 1
    print("OK: structure, protected tokens, and Unicode NFC are intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
