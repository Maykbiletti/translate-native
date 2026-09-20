"""Lossless planning for a conservative YAML localization subset.

Only decoded, non-empty scalar values become model-owned. Mapping keys,
comments, indentation, line endings and scalar quoting remain host-owned. YAML
features whose semantics cannot be proved by this line-oriented profile block
before model access instead of falling back to prose.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Callable


POLICY = "raw-yaml-mapping-value-spans-v1"
SELECTOR_PROFILE = "yaml-nested-mapping-string-values-v1"
NATIVE_REVIEW_PROJECTION = "yaml-string-values-target-only-v1"
PACKING_OVERHEAD_CHARS = 320
UNIT_RESERVED_CHARS = 768
CONTEXT_CHARS = 192
MAX_ENTRIES = 4096
MAX_DEPTH = 32
MAX_TOKEN_CHARS = 65536
KEY = re.compile(r"[A-Za-z0-9_.-]+")
IMPLICIT_NON_STRING = re.compile(
    r"(?ix)(?:null|~|true|false|yes|no|on|off|"
    r"[-+]?(?:0|[1-9][0-9_]*)(?:\.[0-9_]*)?(?:e[-+]?[0-9]+)?|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[tT ][^ ]+)?|\.nan|[-+]?\.inf)"
)
HOST_NON_STRING = re.compile(
    r"(?x)(?:null|true|false|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)"
)


class YamlRewritePlanError(ValueError):
    def __init__(self, code: str):
        self.code = "long_yaml_" + code
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
        "grammar": "nested-plain-key-mappings-and-single-double-plain-scalars-v1",
        "selected_fields": ["nonempty_string_scalar"],
        "host_owned_scalars": ["JSON-compatible null", "boolean", "number"],
        "keys_comments_indentation_and_layout": "host-owned-exact-bytes",
        "unsupported": ["aliases", "anchors", "block-scalars", "directives",
                        "document-markers", "flow-collections", "merge-keys",
                        "sequences", "tags", "tabs", "multiline-quotes"],
        "require_unique_paths": True,
        "require_source_nfc_before_creator": True,
        "require_candidate_nfc": True,
        "indent_width": 2,
        "max_entries": MAX_ENTRIES,
        "max_depth": MAX_DEPTH,
        "max_token_chars": MAX_TOKEN_CHARS,
        "packing_overhead_chars": PACKING_OVERHEAD_CHARS,
        "unit_reserved_chars": UNIT_RESERVED_CHARS,
        "context_chars": CONTEXT_CHARS,
    }


def looks_like_yaml(source: str) -> bool:
    if not isinstance(source, str):
        return False
    lines = source.lstrip("\ufeff").splitlines()
    # A paired leading ``---`` block followed by prose is the established
    # Markdown front-matter profile, not a standalone YAML resource.
    if lines and lines[0].strip() == "---":
        closing = next((index for index, line in enumerate(lines[1:], 1)
                        if line.strip() == "---"), None)
        if closing is not None and any(line.strip() for line in lines[closing + 1:]):
            return False
    mappings = 0
    last_mapping_indent = -1
    for line in lines:
        if not line.strip() or line.lstrip(" ").startswith("#"):
            continue
        match = re.match(r"^([ ]*)[A-Za-z0-9_.-]+[ ]*:", line)
        if match:
            mappings += 1
            last_mapping_indent = len(match.group(1))
            continue
        if line.strip() in {"---", "..."} or line.lstrip().startswith("%YAML"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if last_mapping_indent >= 0 and indent > last_mapping_indent:
            # Preserve intent for unsupported block scalars and sequences so
            # they fail in the strict parser instead of falling back to prose.
            continue
        return False
    marker = any(line.strip() in {"---", "..."} or line.lstrip().startswith("%YAML")
                 for line in lines[:3])
    return mappings >= 2 or (marker and mappings >= 1)


def _plain_value(raw: str) -> str:
    if (not raw or raw != raw.strip() or len(raw) > MAX_TOKEN_CHARS
            or raw[0] in "-?:,[]{}#&*!|>'\"%@`"
            or any(character in raw for character in "[]{}")
            or ": " in raw or " #" in raw
            or IMPLICIT_NON_STRING.fullmatch(raw)):
        raise YamlRewritePlanError("plain_scalar_unsafe")
    if any(ord(character) < 0x20 for character in raw):
        raise YamlRewritePlanError("control_character")
    return raw


def _quoted_value(raw: str, quote: str) -> str:
    if len(raw) > MAX_TOKEN_CHARS or len(raw) < 2 or raw[-1] != quote:
        raise YamlRewritePlanError("quoted_scalar_invalid")
    if quote == '"':
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            raise YamlRewritePlanError("double_quoted_scalar_invalid") from None
        if not isinstance(value, str):
            raise YamlRewritePlanError("double_quoted_scalar_invalid")
        return value
    inner = raw[1:-1]
    position = 0
    while position < len(inner):
        if inner[position] == "'":
            if position + 1 >= len(inner) or inner[position + 1] != "'":
                raise YamlRewritePlanError("single_quoted_scalar_invalid")
            position += 2
        else:
            if ord(inner[position]) < 0x20:
                raise YamlRewritePlanError("control_character")
            position += 1
    return inner.replace("''", "'")


def _scalar(line: str, start: int) -> tuple[int, str, str]:
    while start < len(line) and line[start] == " ":
        start += 1
    if start == len(line) or line[start] == "#":
        raise YamlRewritePlanError("scalar_expected")
    quote = line[start] if line[start] in "'\"" else ""
    if quote:
        position = start + 1
        if quote == '"':
            escaped = False
            while position < len(line):
                character = line[position]
                if character == '"' and not escaped:
                    break
                if character in "\r\n":
                    raise YamlRewritePlanError("multiline_scalar_unsupported")
                if character == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
                position += 1
        else:
            while position < len(line):
                if line[position] == "'":
                    if position + 1 < len(line) and line[position + 1] == "'":
                        position += 2
                        continue
                    break
                position += 1
        if position >= len(line):
            raise YamlRewritePlanError("quoted_scalar_invalid")
        end = position + 1
        if line[end:].strip() and not line[end:].lstrip().startswith("#"):
            raise YamlRewritePlanError("trailing_syntax_unsupported")
        raw = line[start:end]
        return end, _quoted_value(raw, quote), "double" if quote == '"' else "single"
    comment = line.find(" #", start)
    end = len(line) if comment < 0 else comment
    while end > start and line[end - 1] == " ":
        end -= 1
    raw = line[start:end]
    if HOST_NON_STRING.fullmatch(raw):
        return end, raw, "nonstring"
    return end, _plain_value(raw), "plain"


def parse(source: str) -> dict:
    if not isinstance(source, str) or not source:
        raise YamlRewritePlanError("invalid")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise YamlRewritePlanError("unicode_invalid") from None
    if unicodedata.normalize("NFC", source) != source:
        raise YamlRewritePlanError("non_nfc")
    if "\x00" in source:
        raise YamlRewritePlanError("control_character")
    entries, leaves, paths = [], [], set()
    stack: list[tuple[int, str]] = []
    pending_container: tuple[int, str] | None = None
    offset = 1 if source.startswith("\ufeff") else 0
    body = source[offset:]
    for line_match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", body):
        if line_match.start() == len(body):
            break
        physical = line_match.group(0)
        line = physical.rstrip("\r\n")
        line_start = offset + line_match.start()
        if not line.strip() or line.lstrip(" ").startswith("#"):
            continue
        if "\t" in line[:len(line) - len(line.lstrip(" \t"))]:
            raise YamlRewritePlanError("tabs_unsupported")
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent % 2 or indent // 2 > MAX_DEPTH:
            raise YamlRewritePlanError("indentation_invalid")
        if stripped.startswith(("-", "?", ":", "!", "&", "*", "%", "[", "{")):
            raise YamlRewritePlanError("feature_unsupported")
        if stripped in {"---", "..."}:
            raise YamlRewritePlanError("document_marker_unsupported")
        match = re.match(r"([A-Za-z0-9_.-]+)[ ]*:", stripped)
        if not match or not KEY.fullmatch(match.group(1)):
            raise YamlRewritePlanError("mapping_expected")
        depth, key = indent // 2, match.group(1)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if depth and (not stack or stack[-1][0] != depth - 1):
            raise YamlRewritePlanError("indentation_invalid")
        if pending_container is not None and depth <= pending_container[0]:
            raise YamlRewritePlanError("empty_mapping_unsupported")
        pending_container = None
        path = "/".join([item[1] for item in stack] + [key])
        if path in paths:
            raise YamlRewritePlanError("duplicate_path")
        paths.add(path)
        colon_end = len(stripped[:match.end()]) + indent
        remainder = line[colon_end:]
        if not remainder.strip() or remainder.lstrip(" ").startswith("#"):
            stack.append((depth, key))
            pending_container = (depth, path)
            entries.append({"path": path, "kind": "mapping"})
        else:
            end, value, style = _scalar(line, colon_end)
            start = colon_end
            while start < len(line) and line[start] == " ":
                start += 1
            entry = {"path": path, "kind": "scalar", "style": style,
                     "value": value, "value_span": (line_start + start,
                                                        line_start + end)}
            entries.append(entry)
            if value and style != "nonstring":
                leaves.append(entry)
        if len(entries) > MAX_ENTRIES:
            raise YamlRewritePlanError("too_many_entries")
    if pending_container is not None:
        raise YamlRewritePlanError("empty_mapping_unsupported")
    if not entries or not leaves:
        raise YamlRewritePlanError("no_rewritable_values")
    return {"entries": entries, "leaves": leaves}


def _skeleton(source: str, leaves: list[dict]) -> str:
    output, position = [], 0
    for leaf in leaves:
        start, end = leaf["value_span"]
        if start < position:
            raise YamlRewritePlanError("invalid")
        output.extend((source[position:start], "\x00yaml:", leaf["path"], "\x00"))
        position = end
    output.append(source[position:])
    return "".join(output)


def build_plan(source: str, chunk_chars: int, max_groups: int,
               split_text: Callable) -> tuple[dict, dict]:
    if (type(chunk_chars) is not int or chunk_chars < 256
            or type(max_groups) is not int or max_groups < 1):
        raise YamlRewritePlanError("budget_invalid")
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
                raise YamlRewritePlanError("value_too_large") from None
            raise
        internal.update(prefix=prefix, suffix=suffix, separators=separators)
        for part_index, (part, body) in enumerate(parts):
            value_id = "yaml-value-" + _hash({
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
            raise YamlRewritePlanError("value_too_large")
        if current and cost + unit_cost > chunk_chars:
            groups.append(current)
            current, cost = [], 0
        current.append(unit)
        cost += unit_cost
    if current:
        groups.append(current)
    if not groups or len(groups) > max_groups:
        raise YamlRewritePlanError("too_large")
    public_groups = []
    for index, group in enumerate(groups):
        summary = [{"value_id": item["value_id"], "path": item["path"],
                    "source_sha256": item["source_sha256"],
                    "source_chars": len(item["source"]),
                    "source_bytes": len(item["source"].encode("utf-8"))}
                   for item in group]
        chunk_id = "rewrite-yaml-chunk-" + _hash({
            "policy": POLICY, "source_sha256": _text_hash(source),
            "index": index, "values": summary})
        public_groups.append({"index": index, "chunk_id": chunk_id,
                              "values": summary})
    manifest = {
        "schema": "translate-native.long-yaml-manifest.v1",
        "policy": POLICY, "selector_profile": SELECTOR_PROFILE,
        "source_sha256": _text_hash(source), "source_chars": len(source),
        "source_bytes": len(source.encode("utf-8")),
        "skeleton_sha256": _text_hash(_skeleton(source, parsed["leaves"])),
        "chunk_chars": chunk_chars, "max_groups": max_groups,
        "values": [{"path": leaf["path"], "style": leaf["style"],
                    "source_sha256": _text_hash(leaf["value"]),
                    "unit_ids": list(leaves[index]["unit_ids"])}
                   for index, leaf in enumerate(parsed["leaves"])],
        "groups": public_groups,
    }
    return manifest, {"manifest_leaves": leaves,
                      "groups": [dict(public_groups[index], units=group)
                                 for index, group in enumerate(groups)]}


def _encoded(value: str, style: str) -> str:
    if style == "double":
        return json.dumps(value, ensure_ascii=False)
    if style == "single":
        if any(ord(character) < 0x20 for character in value):
            raise YamlRewritePlanError("candidate_invalid")
        return "'" + value.replace("'", "''") + "'"
    _plain_value(value)
    return value


def assemble(source: str, state: dict, candidates: dict[str, str]) -> str:
    replacements, expected = [], {}
    for leaf in state["manifest_leaves"]:
        pieces = [leaf["prefix"]]
        for index, value_id in enumerate(leaf["unit_ids"]):
            candidate = candidates.get(value_id)
            if (not isinstance(candidate, str) or not candidate
                    or candidate != candidate.strip() or "\x00" in candidate
                    or unicodedata.normalize("NFC", candidate) != candidate):
                raise YamlRewritePlanError("candidate_invalid")
            pieces.extend((candidate, leaf["separators"][index]))
        pieces.append(leaf["suffix"])
        revised = "".join(pieces)
        expected[leaf["path"]] = revised
        if revised != leaf["value"]:
            start, end = leaf["value_span"]
            replacements.append((start, end, _encoded(revised, leaf["style"])))
    output, position = [], 0
    for start, end, value in replacements:
        if start < position:
            raise YamlRewritePlanError("invalid")
        output.extend((source[position:start], value))
        position = end
    output.append(source[position:])
    target = "".join(output)
    values, skeleton = target_value_map(target)
    if (skeleton != _text_hash(_skeleton(source, parse(source)["leaves"]))
            or values != expected):
        raise YamlRewritePlanError("assembly_mismatch")
    return target


def target_value_map(target: str) -> tuple[dict[str, str], str]:
    parsed = parse(target)
    values = {leaf["path"]: leaf["value"] for leaf in parsed["leaves"]}
    if len(values) != len(parsed["leaves"]):
        raise YamlRewritePlanError("duplicate_path")
    return values, _text_hash(_skeleton(target, parsed["leaves"]))


def native_review_text(target: str) -> str:
    parsed = parse(target)
    values = [leaf["value"] for leaf in parsed["leaves"]]
    projection = "\n\n".join(values)
    if not projection or unicodedata.normalize("NFC", projection) != projection:
        raise YamlRewritePlanError("review_projection_invalid")
    return projection


def language_validation_text(target: str) -> str:
    return native_review_text(target)
