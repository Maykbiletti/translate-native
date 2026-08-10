#!/usr/bin/env python3
"""Check that a translation preserves structure and protected tokens."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


TOKEN_PATTERNS = (
    ("URL", re.compile(r"(?:https?://|mailto:)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")),
    ("email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")),
    ("Markdown destination", re.compile(r"(?<=\]\()[^\s)]+")),
    ("template", re.compile(r"\{\{[^{}]+\}\}|\$\{[^{}]+\}|%\{[^{}]+\}")),
    ("printf", re.compile(r"%(?:\d+\$)?[-+#0 ']*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlLjzt]*[diouxXfFeEgGaAcspn%@]")),
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
TRANSLATABLE_HTML_ATTRIBUTES = {
    "alt",
    "aria-description",
    "aria-label",
    "placeholder",
    "title",
}
HTML_CODE_ELEMENTS = {"script", "style"}
JSONLD_LINGUISTIC_KEYS = {
    "alternativeHeadline", "articleBody", "caption", "description", "headline",
    "keywords", "name", "text",
}
TRANSLATABLE_META_NAMES = {
    "application-name",
    "description",
    "keywords",
    "twitter:description",
    "twitter:title",
}
TRANSLATABLE_META_PROPERTIES = {
    "og:description",
    "og:site_name",
    "og:title",
    "twitter:description",
    "twitter:title",
}


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


def token_signature(text: str) -> tuple[tuple[tuple[str, str], int], ...]:
    return tuple(sorted(protected_tokens(text).items()))


class TranslationHTMLParser(HTMLParser):
    """Reduce HTML to the parts a translation must preserve."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.events: list[tuple[Any, ...]] = []
        self.open_elements: list[tuple[str, dict[str, str | None]]] = []

    @staticmethod
    def attribute_signature(
        tag: str, attrs: list[tuple[str, str | None]]
    ) -> tuple[Any, ...]:
        signature: list[tuple[Any, ...]] = []
        attribute_map = {name.casefold(): value for name, value in attrs}
        meta_name = (attribute_map.get("name") or "").casefold()
        meta_property = (attribute_map.get("property") or "").casefold()
        content_is_linguistic = tag.casefold() == "meta" and (
            meta_name in TRANSLATABLE_META_NAMES
            or meta_property in TRANSLATABLE_META_PROPERTIES
        )
        for name, value in attrs:
            normalized_name = name.casefold()
            if normalized_name in TRANSLATABLE_HTML_ATTRIBUTES or (
                normalized_name == "content" and content_is_linguistic
            ):
                signature.append((name, "translatable", token_signature(value or "")))
            else:
                signature.append((name, "fixed", value))
        return tuple(signature)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("start", tag, self.attribute_signature(tag, attrs)))
        self.open_elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("empty", tag, self.attribute_signature(tag, attrs)))

    def handle_endtag(self, tag: str) -> None:
        self.events.append(("end", tag))
        for index in range(len(self.open_elements) - 1, -1, -1):
            if self.open_elements[index][0] == tag:
                del self.open_elements[index:]
                break

    def handle_data(self, data: str) -> None:
        current = self.open_elements[-1] if self.open_elements else None
        current_element = current[0] if current else None
        if current_element == "script" and (current[1].get("type") or "").casefold() == "application/ld+json":
            try:
                self.events.append(("json-ld", jsonld_signature(json.loads(data))))
            except json.JSONDecodeError:
                self.events.append(("invalid json-ld", data))
            return
        if current_element in HTML_CODE_ELEMENTS:
            self.events.append(("code", current_element, data))
            return
        signature = token_signature(data)
        if signature:
            self.events.append(("text tokens", signature))

    def handle_comment(self, data: str) -> None:
        self.events.append(("comment", data))

    def handle_decl(self, decl: str) -> None:
        self.events.append(("declaration", decl))

    def handle_entityref(self, name: str) -> None:
        self.events.append(("entity", name))

    def handle_charref(self, name: str) -> None:
        self.events.append(("character reference", name))

    def handle_pi(self, data: str) -> None:
        self.events.append(("processing instruction", data))


def jsonld_signature(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return ("object", tuple((item_key, jsonld_signature(item_value, item_key)) for item_key, item_value in value.items()))
    if isinstance(value, list):
        return ("array", tuple(jsonld_signature(item, key) for item in value))
    if isinstance(value, str) and key in JSONLD_LINGUISTIC_KEYS:
        return ("translatable", token_signature(value))
    return (type(value).__name__, value)


def parse_html(text: str, location: str) -> tuple[list[tuple[Any, ...]], list[str]]:
    parser = TranslationHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # HTMLParser can surface malformed declarations.
        return [], [f"{location}: invalid HTML ({exc})"]
    return parser.events, []


def compare_html(source: str, target: str) -> list[str]:
    source_events, source_errors = parse_html(source, "source")
    target_events, target_errors = parse_html(target, "target")
    errors = source_errors + target_errors
    if errors:
        return errors

    if len(source_events) != len(target_events):
        errors.append(
            f"$: HTML event count changed from {len(source_events)} to {len(target_events)}"
        )
    for index, (source_event, target_event) in enumerate(zip(source_events, target_events)):
        if source_event != target_event:
            errors.append(
                f"$: HTML event {index} changed from {source_event!r} to {target_event!r}"
            )
            if len(errors) >= 20:
                errors.append("$: additional HTML differences omitted")
                break
    return errors


def compare_xml(source: str, target: str) -> list[str]:
    """Compare XML/Android/XLIFF structure while allowing linguistic text."""
    try:
        source_root, target_root = ET.fromstring(source), ET.fromstring(target)
    except ET.ParseError as error:
        return [f"$: invalid XML ({error})"]
    errors: list[str] = []

    def walk(left: ET.Element, right: ET.Element, path: str) -> None:
        if left.tag != right.tag:
            errors.append(f"{path}: tag changed from {left.tag!r} to {right.tag!r}")
            return
        if left.attrib != right.attrib:
            errors.append(f"{path}: attributes changed from {left.attrib!r} to {right.attrib!r}")
        errors.extend(compare_tokens(left.text or "", right.text or "", path + ".text"))
        errors.extend(compare_tokens(left.tail or "", right.tail or "", path + ".tail"))
        if len(left) != len(right):
            errors.append(f"{path}: child count changed from {len(left)} to {len(right)}")
        for index, (left_child, right_child) in enumerate(zip(left, right)):
            walk(left_child, right_child, f"{path}/{index}")
    walk(source_root, target_root, "$/$root")
    return errors


PO_ENTRY = re.compile(r'^(msgctxt|msgid|msgid_plural|msgstr(?:\[\d+\])?)\s+"(.*)"$', re.MULTILINE)
APPLE_STRING = re.compile(r'^\s*"((?:\\.|[^"\\])*)"\s*=\s*"((?:\\.|[^"\\])*)"\s*;\s*$', re.MULTILINE)
TIMESTAMP = re.compile(r"^\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}\s+-->\s+(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}.*$", re.MULTILINE)


def compare_po(source: str, target: str) -> list[str]:
    left, right = PO_ENTRY.findall(source), PO_ENTRY.findall(target)
    left_keys = [(kind, value) for kind, value in left if kind in {"msgctxt", "msgid", "msgid_plural"}]
    right_keys = [(kind, value) for kind, value in right if kind in {"msgctxt", "msgid", "msgid_plural"}]
    errors = [] if left_keys == right_keys else ["$: PO contexts and msgids changed"]
    left_values = [value for kind, value in left if kind.startswith("msgstr")]
    right_values = [value for kind, value in right if kind.startswith("msgstr")]
    if len(left_values) != len(right_values):
        errors.append("$: PO msgstr count changed")
    for index, (a, b) in enumerate(zip(left_values, right_values)):
        errors.extend(compare_tokens(a, b, f"$.msgstr[{index}]"))
    return errors


def compare_apple_strings(source: str, target: str) -> list[str]:
    left, right = APPLE_STRING.findall(source), APPLE_STRING.findall(target)
    if [key for key, _ in left] != [key for key, _ in right]:
        return ["$: Apple .strings keys or ordering changed"]
    errors: list[str] = []
    for (key, source_value), (_, target_value) in zip(left, right):
        errors.extend(compare_tokens(source_value, target_value, f"$.{key}"))
    return errors


def compare_subtitles(source: str, target: str) -> list[str]:
    left, right = TIMESTAMP.findall(source), TIMESTAMP.findall(target)
    return [] if left == right else ["$: subtitle timestamps or cue settings changed"]


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
    parser.add_argument("--format", choices=("auto", "text", "json", "html", "xml", "po", "strings", "subtitle"), default="auto")
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
        suffix = args.source.suffix.lower()
        if suffix == ".json":
            selected_format = "json"
        elif suffix in {".html", ".htm"}:
            selected_format = "html"
        elif suffix in {".xml", ".xliff", ".xlf"}:
            selected_format = "xml"
        elif suffix in {".po", ".pot"}:
            selected_format = "po"
        elif suffix == ".strings":
            selected_format = "strings"
        elif suffix in {".srt", ".vtt", ".ass"}:
            selected_format = "subtitle"
        else:
            selected_format = "text"

    if selected_format == "json":
        source_data, parse_source_errors = parse_json(source_text, args.source)
        target_data, parse_target_errors = parse_json(target_text, args.target)
        errors.extend(parse_source_errors)
        errors.extend(parse_target_errors)
        if not parse_source_errors and not parse_target_errors:
            errors.extend(compare_json(source_data, target_data))
    elif selected_format == "html":
        errors.extend(compare_html(source_text, target_text))
    elif selected_format == "xml":
        errors.extend(compare_xml(source_text, target_text))
    elif selected_format == "po":
        errors.extend(compare_po(source_text, target_text))
    elif selected_format == "strings":
        errors.extend(compare_apple_strings(source_text, target_text))
    elif selected_format == "subtitle":
        errors.extend(compare_subtitles(source_text, target_text))
    else:
        errors.extend(compare_tokens(source_text, target_text, "$"))

    if errors:
        print_errors(errors)
        return 1
    print("OK: structure, protected tokens, and Unicode NFC are intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
