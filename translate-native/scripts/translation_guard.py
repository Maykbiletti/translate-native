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
MIN_TOTAL_SOURCE_UNITS = 80
MIN_SEGMENT_SOURCE_UNITS = 24
MIN_IDENTITY_SOURCE_CHARACTERS = 200
MIN_SEGMENT_IDENTITY_UNITS = 24
MAX_FIXED_IDENTITY_CHARACTERS = 96
RIGHTS_RESERVED_LINE = re.compile(
    r"(?:all rights reserved|alle rechte vorbehalten|tous droits réservés|"
    r"todos los derechos reservados|todos os direitos reservados|"
    r"her hakkı saklıdır|všechna práva vyhrazena)",
    re.IGNORECASE,
)
COPYRIGHT_PREFIX = re.compile(
    r"^(?:(?:copyright\s*)?(?:©|\(c\))|copyright)\s*",
    re.IGNORECASE,
)
LOWERCASE_OWNER_CONNECTORS = {
    "and", "da", "de", "del", "do", "e", "et", "of", "the", "und", "y",
}
LEGAL_OWNER_SUFFIXES = {
    "ab", "ag", "aps", "as", "association", "bv", "co", "company", "corp",
    "corporation", "foundation", "gmbh", "inc", "incorporated", "kg", "limited",
    "llc", "ltd", "nv", "oy", "oyj", "plc", "pte", "pty", "sa", "sarl", "sas",
    "se", "spa", "srl",
}


def linguistic_units(text: str) -> int:
    """Count Unicode letters and numbers after excluding protected syntax."""
    masked = FENCED_CODE.sub("", text)
    for _, pattern in TOKEN_PATTERNS:
        masked = pattern.sub("", masked)
    return sum(unicodedata.category(character)[0] in {"L", "N"} for character in masked)


def canonical_identity_text(text: str) -> str:
    """Normalize transport-only differences without hiding changed characters."""
    return unicodedata.normalize(
        "NFC",
        text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n"),
    ).strip()


def _fixed_identity_segment(text: str) -> bool:
    """Recognize a complete short legal notice, never prose mentioning copyright."""
    normalized = " ".join(text.split())
    if not normalized or len(normalized) > MAX_FIXED_IDENTITY_CHARACTERS:
        return False
    if RIGHTS_RESERVED_LINE.fullmatch(normalized.rstrip(". ")):
        return True

    prefix = COPYRIGHT_PREFIX.match(normalized)
    if prefix is None:
        return False
    body = normalized[prefix.end():]
    rights = re.search(
        rf"(?:\.\s*)?{RIGHTS_RESERVED_LINE.pattern}\.?$",
        body,
        re.IGNORECASE,
    )
    if rights:
        body = body[:rights.start()]
    body = body.strip(" .")
    year = re.match(r"^(?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)\d{2})?\s+", body)
    if year is None:
        return False
    body = body[year.end():]

    # A legal owner is a compact proper name, not a title-cased sentence. Names
    # longer than three words must end in an explicit legal-entity designator.
    # This deliberately prefers a false block that can be reviewed over letting
    # unchanged prose through merely because every word begins with a capital.
    if not body or re.search(r"[!?:;]", body):
        return False
    owner_words = re.findall(r"[^\W_]+", body.replace(".", ""), re.UNICODE)
    if not owner_words:
        return False
    owner_shape = all(
        not word.islower() or word.casefold() in LOWERCASE_OWNER_CONNECTORS
        for word in owner_words
    )
    has_legal_suffix = owner_words[-1].casefold() in LEGAL_OWNER_SUFFIXES
    maximum_words = 8 if has_legal_suffix else 3
    return 1 <= len(owner_words) <= maximum_words and owner_shape


def _actionable_unchanged_segment(source: str, target: str) -> tuple[str, int] | None:
    canonical_source = canonical_identity_text(source)
    canonical_target = canonical_identity_text(target)
    source_units = linguistic_units(canonical_source)
    if (
        source_units >= MIN_SEGMENT_IDENTITY_UNITS
        and canonical_source == canonical_target
        and not _fixed_identity_segment(canonical_source)
    ):
        return canonical_source, source_units
    return None


def identity_errors(
    source: str | list[str],
    target: str | list[str],
    location: str = "$",
    segment_locations: list[str] | None = None,
) -> list[str]:
    """Block unchanged complete inputs or aligned linguistic segments."""
    if isinstance(source, list) and isinstance(target, list):
        if len(source) != len(target):
            # Structure and volume checks own count mismatches. Pairing shifted
            # lists here could blame an unrelated segment for being unchanged.
            return []
        errors: list[str] = []
        for index, (source_segment, target_segment) in enumerate(zip(source, target)):
            segment_location = (
                segment_locations[index]
                if segment_locations is not None and index < len(segment_locations)
                else f"{location}[{index}]"
            )
            unchanged = _actionable_unchanged_segment(source_segment, target_segment)
            if unchanged is not None:
                _, source_units = unchanged
                errors.append(
                    f"{segment_location}: linguistic segment is unchanged from the source "
                    f"across {source_units} units; untranslated segment is blocked"
                )
        return errors

    if not isinstance(source, str) or not isinstance(target, str):
        return []
    canonical_source = canonical_identity_text(source)
    canonical_target = canonical_identity_text(target)
    if (
        len(canonical_source) >= MIN_IDENTITY_SOURCE_CHARACTERS
        and canonical_source == canonical_target
    ):
        return [
            f"{location}: target is unchanged from the source across "
            f"{len(canonical_source)} characters; translation identity is blocked"
        ]
    return []


def unordered_identity_errors(
    source_segments: list[str],
    target_segments: list[str],
    location: str = "$segments",
    source_locations: list[str] | None = None,
) -> list[str]:
    """Find unchanged linguistic segments even when target order changes."""
    target_counts = Counter(canonical_identity_text(segment) for segment in target_segments)
    errors: list[str] = []
    for index, source_segment in enumerate(source_segments):
        canonical_source = canonical_identity_text(source_segment)
        if not target_counts[canonical_source]:
            continue
        unchanged = _actionable_unchanged_segment(source_segment, canonical_source)
        if unchanged is None:
            continue
        target_counts[canonical_source] -= 1
        _, source_units = unchanged
        segment_location = (
            source_locations[index]
            if source_locations is not None and index < len(source_locations)
            else f"{location}[{index}]"
        )
        errors.append(
            f"{segment_location}: linguistic segment is unchanged from the source "
            f"across {source_units} units; untranslated segment is blocked"
        )
    return errors


def _cjk_dominant(text: str) -> bool:
    letters = [character for character in text if unicodedata.category(character).startswith("L")]
    if not letters:
        return False
    cjk = sum(
        "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u9fff"
        or "\uac00" <= character <= "\ud7af"
        for character in letters
    )
    return cjk / len(letters) >= 0.4


def volume_errors(source_segments: list[str], target_segments: list[str], location: str = "$") -> list[str]:
    """Detect major omissions/additions without claiming semantic equivalence."""
    errors: list[str] = []
    source_nonempty = [segment for segment in source_segments if linguistic_units(segment)]
    target_nonempty = [segment for segment in target_segments if linguistic_units(segment)]
    source_total = sum(linguistic_units(segment) for segment in source_nonempty)
    target_total = sum(linguistic_units(segment) for segment in target_nonempty)

    if source_total >= MIN_TOTAL_SOURCE_UNITS:
        minimum_ratio = 0.18 if _cjk_dominant(" ".join(target_nonempty)) else 0.45
        ratio = target_total / source_total if source_total else 1.0
        if ratio < minimum_ratio:
            errors.append(
                f"{location}: target linguistic volume is {target_total}/{source_total} "
                f"units ({ratio:.1%}); minimum for this script is {minimum_ratio:.0%}"
            )
        if ratio > 3.0:
            errors.append(
                f"{location}: target linguistic volume is {target_total}/{source_total} "
                f"units ({ratio:.1%}); possible unsupported addition"
            )

    if len(source_nonempty) != len(target_nonempty):
        errors.append(
            f"{location}: linguistic segment count changed from {len(source_nonempty)} to {len(target_nonempty)}"
        )
        return errors

    for index, (source_segment, target_segment) in enumerate(zip(source_nonempty, target_nonempty)):
        source_units = linguistic_units(source_segment)
        target_units = linguistic_units(target_segment)
        if source_units < MIN_SEGMENT_SOURCE_UNITS:
            continue
        minimum_ratio = 0.12 if _cjk_dominant(target_segment) else 0.25
        ratio = target_units / source_units
        if ratio < minimum_ratio:
            errors.append(
                f"{location}[{index}]: target segment volume is {target_units}/{source_units} "
                f"units ({ratio:.1%}); possible truncation"
            )
    return errors


def json_segments(value: Any) -> list[str]:
    if isinstance(value, dict):
        # JSON object order is not semantic. Sort keys so source and target
        # segments stay aligned even when a formatter reorders properties.
        return [
            segment
            for key in sorted(value)
            for segment in json_segments(value[key])
        ]
    if isinstance(value, list):
        return [segment for child in value for segment in json_segments(child)]
    return [value] if isinstance(value, str) else []


def json_located_segments(value: Any, path: str = "$") -> list[tuple[str, str]]:
    """Return JSON string values with stable semantic paths."""
    if isinstance(value, dict):
        return [
            segment
            for key in sorted(value)
            for segment in json_located_segments(value[key], f"{path}.{key}")
        ]
    if isinstance(value, list):
        return [
            segment
            for index, child in enumerate(value)
            for segment in json_located_segments(child, f"{path}[{index}]")
        ]
    return [(path, value)] if isinstance(value, str) else []


def jsonld_segments(value: Any, key: str | None = None) -> list[str]:
    """Extract only Schema.org fields whose values are human-language copy."""
    if isinstance(value, dict):
        return [
            segment
            for child_key, child_value in value.items()
            for segment in jsonld_segments(child_value, child_key)
        ]
    if isinstance(value, list):
        return [segment for child in value for segment in jsonld_segments(child, key)]
    if isinstance(value, str) and key in JSONLD_LINGUISTIC_KEYS:
        return [value]
    return []


def xml_segments(text: str) -> list[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    return [segment for segment in root.itertext() if segment.strip()]


class LinguisticHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[str] = []
        self.locations: list[str] = []
        self.stack: list[dict[str, Any]] = []
        self.root_children: dict[str, int] = {}
        self.root_text_count = 0

    def _append_segment(self, value: str, location: str) -> None:
        self.segments.append(value)
        self.locations.append(location)

    def _next_element_path(self, tag: str) -> str:
        counts = self.stack[-1]["children"] if self.stack else self.root_children
        index = counts.get(tag, 0)
        counts[tag] = index + 1
        parent = self.stack[-1]["path"] if self.stack else "$html"
        return f"{parent}/{tag}[{index}]"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        attribute_map = {name.casefold(): value or "" for name, value in attrs}
        element_path = self._next_element_path(normalized_tag)
        self.stack.append({
            "tag": normalized_tag,
            "type": attribute_map.get("type", "").casefold(),
            "path": element_path,
            "children": {},
            "text_count": 0,
        })
        meta_name = attribute_map.get("name", "").casefold()
        meta_property = attribute_map.get("property", "").casefold()
        for name, value in attrs:
            normalized = name.casefold()
            linguistic_meta = normalized == "content" and (
                meta_name in TRANSLATABLE_META_NAMES or meta_property in TRANSLATABLE_META_PROPERTIES
            )
            if value and (normalized in TRANSLATABLE_HTML_ATTRIBUTES or linguistic_meta):
                self._append_segment(value, f"{element_path}/@{normalized}")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag.casefold():
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        current = self.stack[-1] if self.stack else None
        current_tag = current["tag"] if current else ""
        current_type = current["type"] if current else ""
        if current_tag in HTML_CODE_ELEMENTS:
            if current_tag == "script" and current_type == "application/ld+json":
                try:
                    for index, segment in enumerate(jsonld_segments(json.loads(data))):
                        self._append_segment(segment, f"{current['path']}/jsonld[{index}]")
                except json.JSONDecodeError:
                    pass
            return
        if data.strip():
            if current:
                text_index = current["text_count"]
                current["text_count"] = text_index + 1
                location = f"{current['path']}/text()[{text_index}]"
            else:
                text_index = self.root_text_count
                self.root_text_count += 1
                location = f"$html/text()[{text_index}]"
            self._append_segment(data, location)


def html_segments(text: str) -> list[str]:
    return [segment for _, segment in html_located_segments(text)]


def html_located_segments(text: str) -> list[tuple[str, str]]:
    parser = LinguisticHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return []
    return list(zip(parser.locations, parser.segments))


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


def po_segments(text: str) -> list[str]:
    entries = PO_ENTRY.findall(text)
    translated = [value for kind, value in entries if kind.startswith("msgstr") and value]
    return translated or [value for kind, value in entries if kind in {"msgid", "msgid_plural"} and value]


def apple_segments(text: str) -> list[str]:
    return [value for _, value in APPLE_STRING.findall(text)]


def subtitle_segments(text: str) -> list[str]:
    segments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or TIMESTAMP.fullmatch(line):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            continue
        if stripped.startswith("Dialogue:"):
            # ASS dialogue has nine fixed comma-separated fields before Text.
            fields = line.split(",", 9)
            if len(fields) == 10 and fields[-1].strip():
                segments.append(fields[-1])
            continue
        if stripped.startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "Format:")):
            continue
        segments.append(line)
    return segments


def detect_content_format(text: str) -> str:
    """Infer a structured format from measurable syntax, without caller trust."""
    stripped = text.lstrip("\ufeff\n\r\t ")
    if not stripped:
        return "text"
    if stripped[:1] in {"{", "["}:
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            pass
    if re.search(
        r"<(?:!doctype\s+html|html|head|body|main|section|article|nav|header|footer|div|p|h[1-6])\b",
        stripped,
        re.IGNORECASE,
    ):
        return "html"
    if stripped.startswith("<"):
        try:
            ET.fromstring(stripped)
            return "xml"
        except ET.ParseError:
            pass
    if re.search(r"^msg(?:id|str|ctxt)\b", text, re.MULTILINE):
        return "po"
    if APPLE_STRING.search(text):
        return "strings"
    if TIMESTAMP.search(text) or re.search(r"^Dialogue:", text, re.MULTILINE):
        return "subtitle"
    return "text"


def linguistic_segments(text: str, selected_format: str) -> list[str]:
    """Extract human-language segments for volume checks in one known format."""
    if selected_format == "json":
        try:
            return json_segments(json.loads(text.lstrip("\ufeff")))
        except json.JSONDecodeError:
            return [text]
    if selected_format == "html":
        return html_segments(text)
    if selected_format == "xml":
        return xml_segments(text)
    if selected_format == "po":
        return po_segments(text)
    if selected_format == "strings":
        return apple_segments(text)
    if selected_format == "subtitle":
        return subtitle_segments(text)
    return [text]


def translation_volume_errors(source: str, target: str) -> list[str]:
    """Run the unconditional, auto-detected volume gate used by MCP release."""
    selected_format = detect_content_format(source)
    return volume_errors(
        linguistic_segments(source, selected_format),
        linguistic_segments(target, selected_format),
        "$",
    )


def structured_identity_errors(
    source: str,
    target: str,
    selected_format: str,
) -> list[str]:
    """Compare aligned user-visible segments after whole-input identity passes."""
    if selected_format == "text":
        return []
    if selected_format == "json":
        try:
            source_data = json.loads(source.lstrip("\ufeff"))
            target_data = json.loads(target.lstrip("\ufeff"))
        except json.JSONDecodeError:
            return []
        source_located = json_located_segments(source_data)
        target_by_path = dict(json_located_segments(target_data))
        common = [
            (path, value, target_by_path[path])
            for path, value in source_located
            if path in target_by_path
        ]
        return identity_errors(
            [source_value for _, source_value, _ in common],
            [target_value for _, _, target_value in common],
            "$segments",
            [path for path, _, _ in common],
        )
    if selected_format == "html":
        source_located = html_located_segments(source)
        return unordered_identity_errors(
            [segment for _, segment in source_located],
            html_segments(target),
            "$segments",
            [path for path, _ in source_located],
        )
    return unordered_identity_errors(
        linguistic_segments(source, selected_format),
        linguistic_segments(target, selected_format),
        "$segments",
    )


def translation_identity_errors(source: str, target: str) -> list[str]:
    """Run the mandatory whole-input and auto-detected segment identity gate."""
    whole_input_errors = identity_errors(source, target, "$")
    if whole_input_errors:
        return whole_input_errors
    selected_format = detect_content_format(source)
    return structured_identity_errors(source, target, selected_format)


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
        description="Verify source-target non-identity, Unicode normalization, structure, volume, and protected tokens in a translation."
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
        return 2
    assert source_text is not None and target_text is not None

    errors.extend(normalization_errors(target_text, args.target))
    whole_identity_errors = identity_errors(source_text, target_text, "$")
    errors.extend(whole_identity_errors)
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

    if not whole_identity_errors:
        errors.extend(structured_identity_errors(source_text, target_text, selected_format))

    if selected_format == "json":
        source_data, parse_source_errors = parse_json(source_text, args.source)
        target_data, parse_target_errors = parse_json(target_text, args.target)
        errors.extend(parse_source_errors)
        errors.extend(parse_target_errors)
        if not parse_source_errors and not parse_target_errors:
            errors.extend(compare_json(source_data, target_data))
            errors.extend(volume_errors(json_segments(source_data), json_segments(target_data), "$"))
    elif selected_format == "html":
        errors.extend(compare_html(source_text, target_text))
        errors.extend(volume_errors(html_segments(source_text), html_segments(target_text), "$"))
    elif selected_format == "xml":
        errors.extend(compare_xml(source_text, target_text))
        errors.extend(volume_errors(xml_segments(source_text), xml_segments(target_text), "$"))
    elif selected_format == "po":
        errors.extend(compare_po(source_text, target_text))
        errors.extend(volume_errors(po_segments(source_text), po_segments(target_text), "$"))
    elif selected_format == "strings":
        errors.extend(compare_apple_strings(source_text, target_text))
        errors.extend(volume_errors(apple_segments(source_text), apple_segments(target_text), "$"))
    elif selected_format == "subtitle":
        errors.extend(compare_subtitles(source_text, target_text))
        errors.extend(volume_errors(subtitle_segments(source_text), subtitle_segments(target_text), "$"))
    else:
        errors.extend(compare_tokens(source_text, target_text, "$"))
        errors.extend(volume_errors([source_text], [target_text], "$"))

    if errors:
        print_errors(errors)
        return 1
    print(
        "OK: source-target and structured-segment identity thresholds, measurable structure, protected tokens, linguistic volume, and Unicode NFC are intact. "
        "This does not prove semantic fidelity, completeness, or native quality."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
