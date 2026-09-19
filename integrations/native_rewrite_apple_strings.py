"""Lossless long Apple ``.strings`` planning for native rewriting.

Only decoded, non-empty values become model-owned. Keys, comments, separators,
quoting, escapes, whitespace, line endings and empty values remain exact
host-owned bytes. The accepted grammar is intentionally conservative.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Callable


POLICY = "raw-apple-strings-value-spans-v1"
SELECTOR_PROFILE = "apple-strings-nonempty-values-v1"
PACKING_OVERHEAD_CHARS = 320
UNIT_RESERVED_CHARS = 768
CONTEXT_CHARS = 192
MAX_ENTRIES = 4096
MAX_TOKEN_CHARS = 65536
_HEX = frozenset("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r",
                   "t": "\t", "b": "\b", "f": "\f", "/": "/"}


class AppleStringsRewritePlanError(ValueError):
    def __init__(self, code: str):
        self.code = "long_apple_strings_" + code
        super().__init__(self.code)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def effective_policy() -> dict:
    return {
        "policy": POLICY,
        "selector_profile": SELECTOR_PROFILE,
        "grammar": "quoted-key-equals-quoted-value-comments-v1",
        "selected_fields": ["nonempty_value"],
        "empty_value_policy": "host-owned",
        "keys_comments_and_layout": "host-owned-exact-bytes",
        "supported_escapes": sorted(_SIMPLE_ESCAPES) + ["uXXXX", "UXXXX"],
        "comments": ["line", "block"],
        "require_unique_decoded_keys": True,
        "require_source_nfc_before_creator": True,
        "require_candidate_nfc": True,
        "max_entries": MAX_ENTRIES,
        "max_token_chars": MAX_TOKEN_CHARS,
        "packing_overhead_chars": PACKING_OVERHEAD_CHARS,
        "unit_reserved_chars": UNIT_RESERVED_CHARS,
        "context_chars": CONTEXT_CHARS,
    }


def _quoted(source: str, start: int) -> tuple[int, str]:
    if start >= len(source) or source[start] != '"':
        raise AppleStringsRewritePlanError("quoted_string_expected")
    position, output = start + 1, []
    while position < len(source):
        character = source[position]
        if character == '"':
            if position + 1 - start > MAX_TOKEN_CHARS:
                raise AppleStringsRewritePlanError("token_too_large")
            value = "".join(output)
            if "\x00" in value:
                raise AppleStringsRewritePlanError("value_invalid")
            return position + 1, value
        if character in "\r\n" or ord(character) < 0x20:
            raise AppleStringsRewritePlanError("raw_control_character")
        if character != "\\":
            output.append(character)
            position += 1
            continue
        position += 1
        if position >= len(source):
            raise AppleStringsRewritePlanError("unsupported_escape")
        escape = source[position]
        if escape in _SIMPLE_ESCAPES:
            output.append(_SIMPLE_ESCAPES[escape])
            position += 1
            continue
        if escape in {"u", "U"}:
            digits = source[position + 1:position + 5]
            if len(digits) != 4 or any(item not in _HEX for item in digits):
                raise AppleStringsRewritePlanError("unsupported_escape")
            codepoint = int(digits, 16)
            if 0xD800 <= codepoint <= 0xDBFF:
                # Require an adjacent escaped low surrogate and combine it.
                next_start = position + 5
                if (source[next_start:next_start + 2] not in {"\\u", "\\U"}
                        or len(source[next_start + 2:next_start + 6]) != 4
                        or any(item not in _HEX
                               for item in source[next_start + 2:next_start + 6])):
                    raise AppleStringsRewritePlanError("unsupported_escape")
                low = int(source[next_start + 2:next_start + 6], 16)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise AppleStringsRewritePlanError("unsupported_escape")
                output.append(chr(0x10000 + ((codepoint - 0xD800) << 10)
                                  + low - 0xDC00))
                position = next_start + 6
                continue
            if 0xDC00 <= codepoint <= 0xDFFF:
                raise AppleStringsRewritePlanError("unsupported_escape")
            output.append(chr(codepoint))
            position += 5
            continue
        raise AppleStringsRewritePlanError("unsupported_escape")
    raise AppleStringsRewritePlanError("unterminated_string")


def _skip_trivia(source: str, position: int) -> int:
    while position < len(source):
        if source[position].isspace():
            position += 1
            continue
        if source.startswith("//", position):
            cr = source.find("\r", position + 2)
            lf = source.find("\n", position + 2)
            endings = [item for item in (cr, lf) if item >= 0]
            if not endings:
                position = len(source)
            else:
                end = min(endings)
                position = end + (2 if source.startswith("\r\n", end) else 1)
            continue
        if source.startswith("/*", position):
            end = source.find("*/", position + 2)
            if end < 0:
                raise AppleStringsRewritePlanError("unterminated_comment")
            position = end + 2
            continue
        break
    return position


def _horizontal(source: str, position: int) -> int:
    while position < len(source) and source[position] in " \t":
        position += 1
    return position


def parse(source: str) -> dict:
    if not isinstance(source, str) or not source:
        raise AppleStringsRewritePlanError("invalid")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise AppleStringsRewritePlanError("unicode_invalid") from None
    if unicodedata.normalize("NFC", source) != source:
        raise AppleStringsRewritePlanError("non_nfc")
    position = 1 if source.startswith("\ufeff") else 0
    entries, decoded_keys = [], set()
    while True:
        position = _skip_trivia(source, position)
        if position == len(source):
            break
        key_start = position
        key_end, key = _quoted(source, position)
        if key in decoded_keys:
            raise AppleStringsRewritePlanError("duplicate_key")
        decoded_keys.add(key)
        position = _horizontal(source, key_end)
        if position >= len(source) or source[position] != "=":
            raise AppleStringsRewritePlanError("equals_expected")
        position = _horizontal(source, position + 1)
        value_start = position
        value_end, value = _quoted(source, position)
        position = _horizontal(source, value_end)
        if position >= len(source) or source[position] != ";":
            raise AppleStringsRewritePlanError("semicolon_expected")
        position += 1
        entries.append({"index": len(entries), "key": key, "value": value,
                        "key_span": (key_start, key_end),
                        "value_span": (value_start, value_end)})
        if len(entries) > MAX_ENTRIES:
            raise AppleStringsRewritePlanError("too_many_entries")
    if not entries:
        raise AppleStringsRewritePlanError("invalid")
    leaves = [{"path": f"entry[{item['index']}]/value", **item}
              for item in entries if item["value"]]
    if not leaves:
        raise AppleStringsRewritePlanError("no_rewritable_values")
    return {"entries": entries, "leaves": leaves}


def _skeleton(source: str, leaves: list[dict]) -> str:
    output, position = [], 0
    for leaf in leaves:
        start, end = leaf["value_span"]
        if start < position:
            raise AppleStringsRewritePlanError("invalid")
        output.extend((source[position:start], '"\x00strings:', leaf["path"], '\x00"'))
        position = end
    output.append(source[position:])
    return "".join(output)


def build_plan(source: str, chunk_chars: int, max_groups: int,
               split_text: Callable) -> tuple[dict, dict]:
    if (type(chunk_chars) is not int or chunk_chars < 256
            or type(max_groups) is not int or max_groups < 1):
        raise AppleStringsRewritePlanError("budget_invalid")
    parsed = parse(source)
    leaves, units = [], []
    unit_budget = max(256, chunk_chars - UNIT_RESERVED_CHARS)
    for leaf_index, leaf in enumerate(parsed["leaves"]):
        value = leaf["value"]
        internal = dict(leaf, prefix=value, suffix="", separators=[], unit_ids=[])
        try:
            _manifest, parts, separators, prefix, suffix = split_text(
                value, unit_budget, max_groups)
        except Exception as error:
            if getattr(error, "code", "").endswith("long_document_too_large"):
                raise AppleStringsRewritePlanError("value_too_large") from None
            raise
        internal.update(prefix=prefix, suffix=suffix, separators=separators)
        for part_index, (part, body) in enumerate(parts):
            value_id = "strings-value-" + _hash({
                "policy": POLICY, "source_sha256": _text_hash(source),
                "path": leaf["path"], "value_sha256": _text_hash(value),
                "part": part_index, "source_sha256_part": part["source_sha256"],
            })
            unit = {"value_id": value_id, "path": leaf["path"],
                    "leaf_index": leaf_index, "part_index": part_index,
                    "part_count": len(parts), "source": body,
                    "source_sha256": part["source_sha256"],
                    "previous_context": value[max(0, part["source_start"] - CONTEXT_CHARS):
                                              part["source_start"]],
                    "next_context": value[part["source_end"]:
                                          part["source_end"] + CONTEXT_CHARS]}
            internal["unit_ids"].append(value_id)
            units.append(unit)
        leaves.append(internal)
    groups, current, cost = [], [], 0
    for unit in units:
        unit_cost = (len(unit["source"]) + len(unit["previous_context"])
                     + len(unit["next_context"]) + PACKING_OVERHEAD_CHARS)
        if unit_cost > chunk_chars:
            raise AppleStringsRewritePlanError("value_too_large")
        if current and cost + unit_cost > chunk_chars:
            groups.append(current)
            current, cost = [], 0
        current.append(unit)
        cost += unit_cost
    if current:
        groups.append(current)
    if not groups or len(groups) > max_groups:
        raise AppleStringsRewritePlanError("too_large")
    public_groups = []
    for index, group in enumerate(groups):
        summary = [{"value_id": item["value_id"], "path": item["path"],
                    "source_sha256": item["source_sha256"],
                    "source_chars": len(item["source"]),
                    "source_bytes": len(item["source"].encode("utf-8"))}
                   for item in group]
        chunk_id = "rewrite-strings-chunk-" + _hash({
            "policy": POLICY, "source_sha256": _text_hash(source),
            "index": index, "values": summary})
        public_groups.append({"index": index, "chunk_id": chunk_id,
                              "values": summary})
    manifest = {
        "schema": "translate-native.long-apple-strings-manifest.v1",
        "policy": POLICY, "selector_profile": SELECTOR_PROFILE,
        "source_sha256": _text_hash(source), "source_chars": len(source),
        "source_bytes": len(source.encode("utf-8")),
        "skeleton_sha256": _text_hash(_skeleton(source, parsed["leaves"])),
        "chunk_chars": chunk_chars, "max_groups": max_groups,
        "values": [{"path": leaf["path"], "entry_index": leaf["index"],
                    "key_sha256": _text_hash(leaf["key"]),
                    "source_sha256": _text_hash(leaf["value"]),
                    "unit_ids": list(leaves[index]["unit_ids"])}
                   for index, leaf in enumerate(parsed["leaves"])],
        "groups": public_groups,
    }
    return manifest, {"manifest_leaves": leaves,
                      "groups": [dict(public_groups[index], units=group)
                                 for index, group in enumerate(groups)]}


def assemble(source: str, state: dict, candidates: dict[str, str]) -> str:
    replacements = []
    expected = {}
    for leaf in state["manifest_leaves"]:
        pieces = [leaf["prefix"]]
        for index, value_id in enumerate(leaf["unit_ids"]):
            candidate = candidates.get(value_id)
            if (not isinstance(candidate, str) or not candidate
                    or candidate != candidate.strip() or "\x00" in candidate
                    or unicodedata.normalize("NFC", candidate) != candidate):
                raise AppleStringsRewritePlanError("candidate_invalid")
            pieces.extend((candidate, leaf["separators"][index]))
        pieces.append(leaf["suffix"])
        revised = "".join(pieces)
        expected[leaf["path"]] = revised
        if revised != leaf["value"]:
            start, end = leaf["value_span"]
            replacements.append((start, end, json.dumps(revised, ensure_ascii=False)))
    output, position = [], 0
    for start, end, value in replacements:
        if start < position:
            raise AppleStringsRewritePlanError("invalid")
        output.extend((source[position:start], value))
        position = end
    output.append(source[position:])
    target = "".join(output)
    values, skeleton = target_value_map(target)
    if (skeleton != _text_hash(_skeleton(source, parse(source)["leaves"]))
            or values != expected):
        raise AppleStringsRewritePlanError("assembly_mismatch")
    return target


def target_value_map(target: str) -> tuple[dict[str, str], str]:
    parsed = parse(target)
    values = {leaf["path"]: leaf["value"] for leaf in parsed["leaves"]}
    if len(values) != len(parsed["leaves"]):
        raise AppleStringsRewritePlanError("duplicate_path")
    return values, _text_hash(_skeleton(target, parsed["leaves"]))


def language_validation_text(target: str) -> str:
    parsed = parse(target)
    return "\n".join(leaf["value"] for leaf in parsed["leaves"])
