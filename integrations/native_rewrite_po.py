"""Lossless long-GNU-PO planning for same-language native rewriting.

Only non-empty ``msgstr`` values outside the metadata header become model-owned.
Comments, flags, contexts, msgids, plural selectors, keywords, whitespace, line
endings and every unselected string token stay exact host-owned bytes.  The
accepted grammar is deliberately conservative and uses JSON-compatible quoted
string escapes, a safe subset of GNU PO string syntax.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Callable


POLICY = "raw-po-msgstr-spans-v1"
SELECTOR_PROFILE = "gnu-po-nonempty-msgstr-v1"
NATIVE_REVIEW_PROJECTION = "po-msgstr-target-only-v1"
PACKING_OVERHEAD_CHARS = 320
UNIT_RESERVED_CHARS = 768
CONTEXT_CHARS = 192
MAX_ENTRIES = 4096
MAX_VALUES = 4096
MAX_TOKENS_PER_VALUE = 256
MAX_TOKEN_CHARS = 65536
MAX_PLURAL_INDEX = 1000
_QUOTED = r'"(?:\\.|[^"\\\r\n])*"'
_DIRECTIVE = re.compile(
    r"^(?P<kind>msgctxt|msgid|msgid_plural|msgstr(?:\[(?P<plural>0|[1-9][0-9]*)\])?)"
    r"[ \t]+(?P<token>" + _QUOTED + r")$"
)
_CONTINUATION = re.compile(r"^[ \t]*(?P<token>" + _QUOTED + r")$")


class PoRewritePlanError(ValueError):
    def __init__(self, code: str):
        self.code = "long_po_" + code
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
        "quoted_string_grammar": "json-compatible-gnu-po-subset-v1",
        "directive_pattern": _DIRECTIVE.pattern,
        "directive_flags": _DIRECTIVE.flags,
        "continuation_pattern": _CONTINUATION.pattern,
        "continuation_flags": _CONTINUATION.flags,
        "header_policy": "msgid-empty-entry-host-owned",
        "selected_fields": ["msgstr", "msgstr[n]"],
        "empty_msgstr_policy": "host-owned",
        "comments_and_obsolete_entries": "host-owned",
        "require_source_nfc_before_creator": True,
        "require_candidate_nfc": True,
        "max_entries": MAX_ENTRIES,
        "max_values": MAX_VALUES,
        "max_tokens_per_value": MAX_TOKENS_PER_VALUE,
        "max_token_chars": MAX_TOKEN_CHARS,
        "max_plural_index": MAX_PLURAL_INDEX,
        "packing_overhead_chars": PACKING_OVERHEAD_CHARS,
        "unit_reserved_chars": UNIT_RESERVED_CHARS,
        "context_chars": CONTEXT_CHARS,
    }


def _decode(token: str) -> str:
    if len(token) > MAX_TOKEN_CHARS:
        raise PoRewritePlanError("token_too_large")
    try:
        value = json.loads(token)
        value.encode("utf-8")
    except (json.JSONDecodeError, UnicodeEncodeError, ValueError):
        raise PoRewritePlanError("unsupported_escape") from None
    if not isinstance(value, str) or "\x00" in value:
        raise PoRewritePlanError("value_invalid")
    return value


def _lines(source: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    position = 0
    for line in source.splitlines(keepends=True):
        result.append((position, line))
        position += len(line)
    if position < len(source) or not result:
        result.append((position, source[position:]))
    return result


def _line_body(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\r", "\n")):
        return line[:-1]
    return line


def parse(source: str) -> dict:
    if not isinstance(source, str) or not source:
        raise PoRewritePlanError("invalid")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise PoRewritePlanError("unicode_invalid") from None
    if unicodedata.normalize("NFC", source) != source:
        raise PoRewritePlanError("non_nfc")
    entries: list[dict] = []
    current: dict[str, Any] = {"fields": {}, "order": []}
    active: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current, active
        if not current["order"]:
            current, active = {"fields": {}, "order": []}, None
            return
        if "msgid" not in current["fields"]:
            raise PoRewritePlanError("entry_invalid")
        order = current["order"]
        prefix = (["msgctxt"] if order and order[0] == "msgctxt" else []) + ["msgid"]
        if order[:len(prefix)] != prefix:
            raise PoRewritePlanError("field_order_invalid")
        tail = order[len(prefix):]
        plural = bool(tail and tail[0] == "msgid_plural")
        if plural:
            translations = tail[1:]
            indexes = [int(kind[7:-1]) for kind in translations
                       if re.fullmatch(r"msgstr\[(?:0|[1-9][0-9]*)\]", kind)]
            if (len(indexes) != len(translations) or not indexes
                    or indexes != list(range(len(indexes)))):
                raise PoRewritePlanError("plural_fields_invalid")
        elif tail != ["msgstr"]:
            raise PoRewritePlanError("singular_fields_invalid")
        entries.append(current)
        if len(entries) > MAX_ENTRIES:
            raise PoRewritePlanError("too_many_entries")
        current, active = {"fields": {}, "order": []}, None

    for line_index, (offset, line) in enumerate(_lines(source)):
        body = _line_body(line)
        if line_index == 0 and body.startswith("\ufeff"):
            body, offset = body[1:], offset + 1
        if not body.strip(" \t"):
            finish()
            continue
        if body.lstrip(" \t").startswith("#"):
            active = None
            continue
        match = _DIRECTIVE.fullmatch(body)
        if match is not None:
            kind = match.group("kind")
            plural = match.group("plural")
            if plural is not None and int(plural) > MAX_PLURAL_INDEX:
                raise PoRewritePlanError("plural_index_invalid")
            if kind in current["fields"]:
                raise PoRewritePlanError("duplicate_field")
            token_start = offset + match.start("token")
            token_end = offset + match.end("token")
            active = {"kind": kind, "tokens": [(token_start, token_end)],
                      "value": _decode(match.group("token"))}
            current["fields"][kind] = active
            current["order"].append(kind)
            continue
        continuation = _CONTINUATION.fullmatch(body)
        if continuation is not None:
            if active is None:
                raise PoRewritePlanError("orphan_continuation")
            if len(active["tokens"]) >= MAX_TOKENS_PER_VALUE:
                raise PoRewritePlanError("too_many_tokens")
            token_start = offset + continuation.start("token")
            token_end = offset + continuation.end("token")
            active["tokens"].append((token_start, token_end))
            active["value"] += _decode(continuation.group("token"))
            continue
        raise PoRewritePlanError("unsupported_syntax")
    finish()
    if not entries:
        raise PoRewritePlanError("invalid")
    leaves: list[dict] = []
    for entry_index, entry in enumerate(entries):
        if entry["fields"]["msgid"]["value"] == "":
            if entry_index != 0:
                raise PoRewritePlanError("header_position_invalid")
            continue
        for kind in entry["order"]:
            field = entry["fields"][kind]
            if not kind.startswith("msgstr") or not field["value"]:
                continue
            path = f"entry[{entry_index}]/{kind}"
            leaves.append({"path": path, "entry_index": entry_index,
                           "kind": kind, "value": field["value"],
                           "tokens": list(field["tokens"])})
            if len(leaves) > MAX_VALUES:
                raise PoRewritePlanError("too_many_values")
    if not leaves:
        raise PoRewritePlanError("no_rewritable_values")
    return {"entries": entries, "leaves": leaves}


def _skeleton(source: str, leaves: list[dict]) -> str:
    spans = []
    for leaf in leaves:
        for token_index, (start, end) in enumerate(leaf["tokens"]):
            spans.append((start, end, leaf["path"], token_index))
    spans.sort()
    output, position = [], 0
    for start, end, path, token_index in spans:
        if start < position:
            raise PoRewritePlanError("invalid")
        output.extend((source[position:start], '"\x00po:', path, ":",
                       str(token_index), '\x00"'))
        position = end
    output.append(source[position:])
    return "".join(output)


def build_plan(source: str, chunk_chars: int, max_groups: int,
               split_text: Callable) -> tuple[dict, dict]:
    if (type(chunk_chars) is not int or chunk_chars < 256
            or type(max_groups) is not int or max_groups < 1):
        raise PoRewritePlanError("budget_invalid")
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
                raise PoRewritePlanError("value_too_large") from None
            raise
        internal.update(prefix=prefix, suffix=suffix, separators=separators)
        for part_index, (part, body) in enumerate(parts):
            value_id = "po-value-" + _hash({
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
            raise PoRewritePlanError("value_too_large")
        if current and cost + unit_cost > chunk_chars:
            groups.append(current)
            current, cost = [], 0
        current.append(unit)
        cost += unit_cost
    if current:
        groups.append(current)
    if not groups or len(groups) > max_groups:
        raise PoRewritePlanError("too_large")
    public_groups = []
    for index, group in enumerate(groups):
        summary = [{"value_id": item["value_id"], "path": item["path"],
                    "source_sha256": item["source_sha256"],
                    "source_chars": len(item["source"]),
                    "source_bytes": len(item["source"].encode("utf-8"))}
                   for item in group]
        chunk_id = "rewrite-po-chunk-" + _hash({
            "policy": POLICY, "source_sha256": _text_hash(source),
            "index": index, "values": summary})
        public_groups.append({"index": index, "chunk_id": chunk_id,
                              "values": summary})
    manifest = {
        "schema": "translate-native.long-po-manifest.v1", "policy": POLICY,
        "selector_profile": SELECTOR_PROFILE,
        "source_sha256": _text_hash(source), "source_chars": len(source),
        "source_bytes": len(source.encode("utf-8")),
        "skeleton_sha256": _text_hash(_skeleton(source, parsed["leaves"])),
        "chunk_chars": chunk_chars, "max_groups": max_groups,
        "values": [{"path": leaf["path"], "entry_index": leaf["entry_index"],
                    "kind": leaf["kind"], "source_sha256": _text_hash(leaf["value"]),
                    "token_count": len(leaf["tokens"]),
                    "unit_ids": list(leaves[index]["unit_ids"])}
                   for index, leaf in enumerate(parsed["leaves"])],
        "groups": public_groups,
    }
    state = {"manifest_leaves": leaves,
             "groups": [dict(public_groups[index], units=group)
                        for index, group in enumerate(groups)]}
    return manifest, state


def assemble(source: str, state: dict, candidates: dict[str, str]) -> str:
    replacements: list[tuple[int, int, str]] = []
    for leaf in state["manifest_leaves"]:
        pieces = [leaf["prefix"]]
        for index, value_id in enumerate(leaf["unit_ids"]):
            candidate = candidates.get(value_id)
            if (not isinstance(candidate, str) or not candidate
                    or candidate != candidate.strip() or "\x00" in candidate
                    or unicodedata.normalize("NFC", candidate) != candidate):
                raise PoRewritePlanError("candidate_invalid")
            pieces.extend((candidate, leaf["separators"][index]))
        pieces.append(leaf["suffix"])
        revised = "".join(pieces)
        if revised == leaf["value"]:
            continue
        tokens = leaf["tokens"]
        replacements.append((tokens[0][0], tokens[0][1],
                             json.dumps(revised, ensure_ascii=False)))
        for start, end in tokens[1:]:
            replacements.append((start, end, '""'))
    output, position = [], 0
    for start, end, value in sorted(replacements):
        if start < position:
            raise PoRewritePlanError("invalid")
        output.extend((source[position:start], value))
        position = end
    output.append(source[position:])
    target = "".join(output)
    values, skeleton = target_value_map(target)
    if skeleton != _text_hash(_skeleton(source, parse(source)["leaves"])):
        raise PoRewritePlanError("structure_changed")
    expected = {leaf["path"]: ("".join(
        [leaf["prefix"]] + [piece for pair in zip(
            [candidates[value_id] for value_id in leaf["unit_ids"]],
            leaf["separators"]) for piece in pair] + [leaf["suffix"]]))
        for leaf in state["manifest_leaves"]}
    if values != expected:
        raise PoRewritePlanError("assembly_mismatch")
    return target


def target_value_map(target: str) -> tuple[dict[str, str], str]:
    parsed = parse(target)
    values = {leaf["path"]: leaf["value"] for leaf in parsed["leaves"]}
    if len(values) != len(parsed["leaves"]):
        raise PoRewritePlanError("duplicate_path")
    return values, _text_hash(_skeleton(target, parsed["leaves"]))


def native_review_text(target: str) -> str:
    """Return only decoded translated values, never msgids or catalog metadata."""
    parsed = parse(target)
    values = [leaf["value"] for leaf in parsed["leaves"]]
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise PoRewritePlanError("review_projection_invalid")
    projection = "\n\n".join(values)
    if not projection or unicodedata.normalize("NFC", projection) != projection:
        raise PoRewritePlanError("review_projection_invalid")
    return projection
