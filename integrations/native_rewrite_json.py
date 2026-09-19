"""Trusted long-JSON planning for same-language rewriting.

Only decoded string values become model-owned units. JSON keys, delimiters,
whitespace, object/array structure and non-string scalars remain exact host bytes.
"""
from __future__ import annotations

import hashlib
import json


POLICY = "raw-json-value-spans-v1"
PACKING_OVERHEAD_CHARS = 192
UNIT_RESERVED_CHARS = 768
CONTEXT_CHARS = 128
MAX_DEPTH = 128
MAX_STRING_VALUES = 2048
MAX_PATH_CHARS = 4096
MAX_KEY_CHARS = 1024


class JsonRewritePlanError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _NumberToken:
    __slots__ = ("lexeme",)

    def __init__(self, lexeme):
        self.lexeme = lexeme


def _text_hash(value):
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise JsonRewritePlanError("json_unicode_invalid") from None


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError):
        raise JsonRewritePlanError("json_plan_invalid") from None


def _hash(value):
    return _text_hash(_canonical(value))


def _pointer(parent, kind, value):
    if kind == "key":
        segment = "k:" + value.replace("~", "~0").replace("/", "~1")
    else:
        segment = "i:" + str(value)
    path = parent + "/" + segment
    if len(path) > MAX_PATH_CHARS:
        raise JsonRewritePlanError("json_path_too_large")
    return path


class _Parser:
    def __init__(self, source):
        self.source = source
        self.decoder = json.JSONDecoder(
            parse_int=_NumberToken,
            parse_float=_NumberToken,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                JsonRewritePlanError("json_nonfinite")))
        self.leaves = []

    def _space(self, position):
        while position < len(self.source) and self.source[position] in " \t\r\n":
            position += 1
        return position

    def _decoded(self, position):
        try:
            return self.decoder.raw_decode(self.source, position)
        except JsonRewritePlanError:
            raise
        except (json.JSONDecodeError, RecursionError, ValueError):
            raise JsonRewritePlanError("json_invalid") from None

    @staticmethod
    def _scalar_unicode(value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise JsonRewritePlanError("json_unicode_invalid") from None

    def value(self, position, path, depth):
        if depth > MAX_DEPTH:
            raise JsonRewritePlanError("json_too_deep")
        position = self._space(position)
        if position >= len(self.source):
            raise JsonRewritePlanError("json_invalid")
        marker = self.source[position]
        if marker == "{":
            position = self._space(position + 1)
            keys = set()
            if position < len(self.source) and self.source[position] == "}":
                return position + 1
            while True:
                if position >= len(self.source) or self.source[position] != '"':
                    raise JsonRewritePlanError("json_invalid")
                key, end = self._decoded(position)
                if not isinstance(key, str) or len(key) > MAX_KEY_CHARS:
                    raise JsonRewritePlanError("json_key_invalid")
                self._scalar_unicode(key)
                if key in keys:
                    raise JsonRewritePlanError("json_duplicate_key")
                keys.add(key)
                position = self._space(end)
                if position >= len(self.source) or self.source[position] != ":":
                    raise JsonRewritePlanError("json_invalid")
                position = self.value(position + 1, _pointer(path, "key", key), depth + 1)
                position = self._space(position)
                if position < len(self.source) and self.source[position] == ",":
                    position = self._space(position + 1)
                    continue
                if position < len(self.source) and self.source[position] == "}":
                    return position + 1
                raise JsonRewritePlanError("json_invalid")
        if marker == "[":
            position = self._space(position + 1)
            index = 0
            if position < len(self.source) and self.source[position] == "]":
                return position + 1
            while True:
                position = self.value(position, _pointer(path, "index", index), depth + 1)
                index += 1
                position = self._space(position)
                if position < len(self.source) and self.source[position] == ",":
                    position = self._space(position + 1)
                    continue
                if position < len(self.source) and self.source[position] == "]":
                    return position + 1
                raise JsonRewritePlanError("json_invalid")
        value, end = self._decoded(position)
        if isinstance(value, (dict, list)):
            raise JsonRewritePlanError("json_invalid")
        self._scalar_unicode(value)
        if isinstance(value, str):
            self.leaves.append({"path": path, "start": position, "end": end,
                                "value": value,
                                "raw_sha256": _text_hash(self.source[position:end])})
            if len(self.leaves) > MAX_STRING_VALUES:
                raise JsonRewritePlanError("json_too_many_values")
        return end

    def parse(self):
        if not isinstance(self.source, str):
            raise JsonRewritePlanError("json_invalid")
        start = 1 if self.source.startswith("\ufeff") else 0
        end = self.value(start, "#", 0)
        if self._space(end) != len(self.source):
            raise JsonRewritePlanError("json_invalid")
        return self.leaves


def parse_leaves(source):
    return _Parser(source).parse()


def _skeleton(source, leaves):
    pieces, position = [], 0
    for leaf in leaves:
        pieces.extend((source[position:leaf["start"]], '"<value:', leaf["path"], '>"'))
        position = leaf["end"]
    pieces.append(source[position:])
    return "".join(pieces)


def build_plan(source, chunk_chars, max_groups, split_text):
    if (type(chunk_chars) is not int or chunk_chars < 256
            or type(max_groups) is not int or max_groups < 1):
        raise JsonRewritePlanError("json_budget_invalid")
    leaves = parse_leaves(source)
    unit_budget = max(256, chunk_chars - UNIT_RESERVED_CHARS)
    internal_leaves, units = [], []
    for leaf_index, leaf in enumerate(leaves):
        value = leaf["value"]
        internal = dict(leaf, prefix=value, suffix="", separators=[], unit_ids=[])
        if value.strip():
            try:
                _manifest, parts, separators, prefix, suffix = split_text(
                    value, unit_budget, max_groups)
            except Exception as error:
                if getattr(error, "code", "").endswith("long_document_too_large"):
                    raise JsonRewritePlanError("json_value_too_large") from None
                raise
            internal.update(prefix=prefix, suffix=suffix, separators=separators)
            for part_index, (part, body) in enumerate(parts):
                value_id = "json-value-" + _hash({
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
        internal_leaves.append(internal)
    groups, current, current_cost = [], [], 0
    for unit in units:
        cost = (len(unit["source"]) + len(unit["previous_context"])
                + len(unit["next_context"]) + PACKING_OVERHEAD_CHARS)
        if cost > chunk_chars:
            raise JsonRewritePlanError("json_value_too_large")
        if current and current_cost + cost > chunk_chars:
            groups.append(current)
            current, current_cost = [], 0
        current.append(unit)
        current_cost += cost
    if current:
        groups.append(current)
    if len(groups) > max_groups:
        raise JsonRewritePlanError("json_too_large")
    public_groups = []
    for index, group in enumerate(groups):
        summary = [{"value_id": item["value_id"], "path": item["path"],
                    "source_sha256": item["source_sha256"],
                    "source_chars": len(item["source"]),
                    "source_bytes": len(item["source"].encode("utf-8"))}
                   for item in group]
        chunk_id = "rewrite-json-chunk-" + _hash({
            "policy": POLICY, "source_sha256": _text_hash(source),
            "index": index, "values": summary})
        public_groups.append({"index": index, "chunk_id": chunk_id,
                              "values": summary})
    manifest = {
        "schema": "translate-native.long-json-manifest.v1", "policy": POLICY,
        "source_sha256": _text_hash(source), "source_chars": len(source),
        "source_bytes": len(source.encode("utf-8")),
        "skeleton_sha256": _text_hash(_skeleton(source, leaves)),
        "chunk_chars": chunk_chars, "max_groups": max_groups,
        "values": [{"path": leaf["path"], "start": leaf["start"],
                    "end": leaf["end"], "source_sha256": _text_hash(leaf["value"]),
                    "raw_sha256": leaf["raw_sha256"],
                    "unit_ids": list(internal_leaves[index]["unit_ids"])}
                   for index, leaf in enumerate(leaves)],
        "groups": public_groups,
    }
    state = {"leaves": internal_leaves,
             "groups": [dict(public_groups[index], units=group)
                        for index, group in enumerate(groups)]}
    return manifest, state


def assemble(source, state, candidates):
    replacements = []
    for leaf in state["leaves"]:
        if not leaf["unit_ids"]:
            continue
        pieces = [leaf["prefix"]]
        for index, value_id in enumerate(leaf["unit_ids"]):
            candidate = candidates.get(value_id)
            if not isinstance(candidate, str) or not candidate or candidate != candidate.strip():
                raise JsonRewritePlanError("json_candidate_invalid")
            pieces.extend((candidate, leaf["separators"][index]))
        pieces.append(leaf["suffix"])
        revised = "".join(pieces)
        if revised == leaf["value"]:
            # A no-op must retain the exact original escape spelling/casing.
            token = source[leaf["start"]:leaf["end"]]
        else:
            try:
                token = json.dumps(revised, ensure_ascii=False, allow_nan=False)
                token.encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError):
                raise JsonRewritePlanError("json_candidate_invalid") from None
        replacements.append((leaf["start"], leaf["end"], token))
    target = source
    for start, end, token in reversed(replacements):
        target = target[:start] + token + target[end:]
    target_leaves = parse_leaves(target)
    if _text_hash(_skeleton(target, target_leaves)) != _text_hash(
            _skeleton(source, parse_leaves(source))):
        raise JsonRewritePlanError("json_skeleton_changed")
    return target


def target_value_map(target):
    leaves = parse_leaves(target)
    result = {}
    for leaf in leaves:
        if leaf["path"] in result:
            raise JsonRewritePlanError("json_path_collision")
        result[leaf["path"]] = leaf["value"]
    return result, _text_hash(_skeleton(target, leaves))
