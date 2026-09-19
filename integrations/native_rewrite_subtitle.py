"""Lossless long SRT/WebVTT planning for same-language native rewriting.

Only cue payloads become model-owned.  Headers, cue identifiers, timestamps,
settings, metadata blocks, blank lines and line-ending bytes stay host-owned.
Protected inline syntax is replaced with collision-resistant host markers before
model access and restored byte-for-byte after the candidate is validated.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


POLICY = "strict-srt-webvtt-cue-payloads-v1"
SELECTOR_PROFILE = "srt-webvtt-cue-text-v1"
PACKING_OVERHEAD_CHARS = 320
UNIT_RESERVED_CHARS = 768
CONTEXT_CHARS = 192
MAX_CUES = 4096
MAX_LINES_PER_CUE = 64
MARKER_PATTERN = re.compile(r"__TN_SUB_[0-9]{4}_[0-9a-f]{16}__")
SRT_TIMING = re.compile(
    r"[0-9]{2,}:[0-5][0-9]:[0-5][0-9],[0-9]{3} --> "
    r"[0-9]{2,}:[0-5][0-9]:[0-5][0-9],[0-9]{3}(?: [^\r\n]+)?"
)
VTT_TIMING = re.compile(
    r"(?:[0-9]{2,}:)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3} --> "
    r"(?:[0-9]{2,}:)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3}"
    r"(?: [A-Za-z][A-Za-z0-9-]*:[^ \t\r\n]+)*"
)
_PROTECTED = re.compile(
    r"(?:https?://|mailto:)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
    r"|(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
    r"|\{\{[^{}]+\}\}|\$\{[^{}]+\}|%\{[^{}]+\}"
    r"|%(?:\d+\$)?[-+#0 ']*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlLjzt]*[diouxXfFeEgGaAcspn%@]"
    r"|&(?:[A-Za-z][A-Za-z0-9]+|#\d+|#x[0-9A-Fa-f]+);"
    r"|<[^<>\r\n]+>"
    r"|\\(?:[nrtbfv\\\"']|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2})"
    r"|(?<!`)`[^`\r\n]+`(?!`)"
    r"|\{[A-Za-z_][\w.-]*(?:\s*,[^{}]*)?\}"
)


class SubtitleRewritePlanError(ValueError):
    def __init__(self, code: str):
        self.code = "long_subtitle_" + code
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
        "formats": ["srt", "webvtt"],
        "srt_timing_pattern": SRT_TIMING.pattern,
        "webvtt_timing_pattern": VTT_TIMING.pattern,
        "protected_pattern": _PROTECTED.pattern,
        "marker_pattern": MARKER_PATTERN.pattern,
        "metadata_blocks": ["NOTE", "STYLE", "REGION"],
        "line_count_policy": "preserve-per-cue",
        "line_ending_policy": "source-owned-per-gap",
        "require_source_nfc_before_creator": True,
        "max_cues": MAX_CUES,
        "max_lines_per_cue": MAX_LINES_PER_CUE,
        "packing_overhead_chars": PACKING_OVERHEAD_CHARS,
        "unit_reserved_chars": UNIT_RESERVED_CHARS,
        "context_chars": CONTEXT_CHARS,
    }


def _lines(source: str) -> list[dict]:
    if not isinstance(source, str) or not source:
        raise SubtitleRewritePlanError("invalid")
    if not unicodedata.is_normalized("NFC", source):
        raise SubtitleRewritePlanError("non_nfc")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise SubtitleRewritePlanError("unicode_invalid") from None
    result, position = [], 0
    for match in re.finditer(r"([^\r\n]*)(\r\n|\r|\n|\Z)", source):
        body, ending = match.group(1), match.group(2)
        if match.start() == len(source):
            break
        if body and not body.strip():
            raise SubtitleRewritePlanError("whitespace_line_ambiguous")
        result.append({"body": body, "start": match.start(1),
                       "end": match.end(1), "eol": ending})
        position = match.end()
        if not ending:
            break
    if position != len(source):
        raise SubtitleRewritePlanError("line_endings_invalid")
    return result


def _blocks(lines: list[dict]) -> list[list[dict]]:
    blocks, current = [], []
    for line in lines:
        if line["body"] == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _mask(value: str, cue_index: int) -> tuple[str, list[dict]]:
    if MARKER_PATTERN.search(value):
        raise SubtitleRewritePlanError("marker_collision")
    output, tokens, position = [], [], 0
    for token_index, match in enumerate(_PROTECTED.finditer(value)):
        token = match.group(0)
        marker = "__TN_SUB_{:04d}_{}__".format(
            token_index, _text_hash(f"{cue_index}:{token_index}:{token}")[:16])
        if marker in value:
            raise SubtitleRewritePlanError("marker_collision")
        output.extend((value[position:match.start()], marker))
        tokens.append({"marker": marker, "token": token})
        position = match.end()
    output.append(value[position:])
    return "".join(output), tokens


def _unmask(candidate: str, tokens: list[dict], line_count: int) -> str:
    if (not isinstance(candidate, str) or not candidate
            or candidate != candidate.strip() or "\r" in candidate
            or candidate.count("\n") + 1 != line_count
            or not unicodedata.is_normalized("NFC", candidate)):
        raise SubtitleRewritePlanError("candidate_invalid")
    if MARKER_PATTERN.findall(candidate) != [item["marker"] for item in tokens]:
        raise SubtitleRewritePlanError("protected_syntax_changed")
    restored = candidate
    for item in tokens:
        restored = restored.replace(item["marker"], item["token"], 1)
    if MARKER_PATTERN.search(restored):
        raise SubtitleRewritePlanError("protected_syntax_changed")
    return restored


def parse(source: str) -> dict:
    lines = _lines(source)
    blocks = _blocks(lines)
    if not blocks:
        raise SubtitleRewritePlanError("invalid")
    first = blocks[0][0]["body"]
    if first.startswith("\ufeff"):
        first = first[1:]
    webvtt = first == "WEBVTT" or first.startswith("WEBVTT ")
    if not webvtt and any(line["body"].startswith("Dialogue:") for line in lines):
        raise SubtitleRewritePlanError("ass_unsupported")
    format_name = "webvtt" if webvtt else "srt"
    start_index = 1 if webvtt else 0
    leaves = []
    for block_index, block in enumerate(blocks[start_index:], start=start_index):
        lead = block[0]["body"]
        if webvtt and (lead == "NOTE" or lead.startswith("NOTE ")
                       or lead in {"STYLE", "REGION"}):
            continue
        timing_index = 0
        pattern = VTT_TIMING if webvtt else SRT_TIMING
        if not pattern.fullmatch(lead):
            if len(block) < 2 or not pattern.fullmatch(block[1]["body"]):
                raise SubtitleRewritePlanError("cue_invalid")
            if not webvtt and not lead.isdigit():
                raise SubtitleRewritePlanError("cue_identifier_invalid")
            timing_index = 1
        payload = block[timing_index + 1:]
        if not payload or len(payload) > MAX_LINES_PER_CUE:
            raise SubtitleRewritePlanError("cue_payload_invalid")
        if any(line["body"] != line["body"].strip() for line in payload):
            raise SubtitleRewritePlanError("cue_payload_whitespace_ambiguous")
        value = "\n".join(line["body"] for line in payload)
        if not value.strip():
            raise SubtitleRewritePlanError("cue_payload_invalid")
        cue_index = len(leaves)
        masked, tokens = _mask(value, cue_index)
        leaves.append({
            "path": f"cue[{cue_index}]", "block_index": block_index,
            "start": payload[0]["start"], "end": payload[-1]["end"],
            "value": value, "source": masked, "tokens": tokens,
            "line_count": len(payload),
            "line_endings": [line["eol"] for line in payload[:-1]],
        })
        if len(leaves) > MAX_CUES:
            raise SubtitleRewritePlanError("too_many_cues")
    if not leaves:
        raise SubtitleRewritePlanError("no_rewritable_values")
    return {"format": format_name, "leaves": leaves}


def _skeleton(source: str, leaves: list[dict]) -> str:
    output, position = [], 0
    for leaf in leaves:
        output.extend((source[position:leaf["start"]], "\x00subtitle:",
                       leaf["path"], "\x00"))
        position = leaf["end"]
    output.append(source[position:])
    return "".join(output)


def build_plan(source: str, chunk_chars: int, max_groups: int, _split_text=None):
    if (type(chunk_chars) is not int or chunk_chars < 256
            or type(max_groups) is not int or max_groups < 1):
        raise SubtitleRewritePlanError("budget_invalid")
    parsed = parse(source)
    leaves, units = [], []
    for index, leaf in enumerate(parsed["leaves"]):
        unit = {
            "value_id": "subtitle-value-" + _hash({
                "policy": POLICY, "source_sha256": _text_hash(source),
                "path": leaf["path"], "value_sha256": _text_hash(leaf["source"]),
            }),
            "path": leaf["path"], "source": leaf["source"],
            "source_sha256": _text_hash(leaf["source"]),
            "previous_context": (parsed["leaves"][index - 1]["source"][-CONTEXT_CHARS:]
                                 if index else ""),
            "next_context": (parsed["leaves"][index + 1]["source"][:CONTEXT_CHARS]
                             if index + 1 < len(parsed["leaves"]) else ""),
        }
        cost = (len(unit["source"]) + len(unit["previous_context"])
                + len(unit["next_context"]) + PACKING_OVERHEAD_CHARS)
        if cost > chunk_chars:
            raise SubtitleRewritePlanError("cue_too_large")
        internal = dict(leaf, unit_ids=[unit["value_id"]])
        leaves.append(internal)
        units.append(unit)
    groups, current, cost = [], [], 0
    for unit in units:
        item_cost = (len(unit["source"]) + len(unit["previous_context"])
                     + len(unit["next_context"]) + PACKING_OVERHEAD_CHARS)
        if current and cost + item_cost > chunk_chars:
            groups.append(current)
            current, cost = [], 0
        current.append(unit)
        cost += item_cost
    if current:
        groups.append(current)
    if len(groups) > max_groups:
        raise SubtitleRewritePlanError("too_large")
    public_groups = []
    for index, group in enumerate(groups):
        summary = [{"value_id": item["value_id"], "path": item["path"],
                    "source_sha256": item["source_sha256"],
                    "source_chars": len(item["source"]),
                    "source_bytes": len(item["source"].encode("utf-8"))}
                   for item in group]
        public_groups.append({
            "index": index,
            "chunk_id": "rewrite-subtitle-chunk-" + _hash({
                "policy": POLICY, "source_sha256": _text_hash(source),
                "index": index, "values": summary}),
            "values": summary,
        })
    manifest = {
        "schema": "translate-native.long-subtitle-manifest.v1",
        "policy": POLICY, "selector_profile": SELECTOR_PROFILE,
        "format": parsed["format"], "source_sha256": _text_hash(source),
        "source_chars": len(source), "source_bytes": len(source.encode("utf-8")),
        "skeleton_sha256": _text_hash(_skeleton(source, leaves)),
        "chunk_chars": chunk_chars, "max_groups": max_groups,
        "values": [{"path": leaf["path"], "start": leaf["start"],
                    "end": leaf["end"], "source_sha256": _text_hash(leaf["source"]),
                    "line_count": leaf["line_count"], "unit_ids": leaf["unit_ids"]}
                   for leaf in leaves],
        "groups": public_groups,
    }
    return manifest, {"format": parsed["format"], "leaves": leaves,
                      "groups": [dict(public_groups[index], units=group)
                                 for index, group in enumerate(groups)]}


def assemble(source: str, state: dict, candidates: dict) -> str:
    replacements = []
    for leaf in state["leaves"]:
        value_id = leaf["unit_ids"][0]
        if value_id not in candidates:
            raise SubtitleRewritePlanError("candidate_missing")
        restored = _unmask(candidates[value_id], leaf["tokens"], leaf["line_count"])
        if restored == leaf["value"]:
            replacement = source[leaf["start"]:leaf["end"]]
        else:
            parts = restored.split("\n")
            replacement = parts[0]
            for ending, part in zip(leaf["line_endings"], parts[1:]):
                replacement += ending + part
        replacements.append((leaf["start"], leaf["end"], replacement))
    if set(candidates) != {leaf["unit_ids"][0] for leaf in state["leaves"]}:
        raise SubtitleRewritePlanError("candidate_extra")
    target = source
    for start, end, replacement in reversed(replacements):
        target = target[:start] + replacement + target[end:]
    mapped, skeleton = candidate_map(source, target)
    if skeleton != _text_hash(_skeleton(source, parse(source)["leaves"])):
        raise SubtitleRewritePlanError("skeleton_changed")
    if mapped != candidates:
        raise SubtitleRewritePlanError("candidate_roundtrip_invalid")
    return target


def candidate_map(source: str, target: str) -> tuple[dict, str]:
    left, right = parse(source), parse(target)
    if left["format"] != right["format"] or len(left["leaves"]) != len(right["leaves"]):
        raise SubtitleRewritePlanError("structure_changed")
    if _text_hash(_skeleton(source, left["leaves"])) != _text_hash(
            _skeleton(target, right["leaves"])):
        raise SubtitleRewritePlanError("skeleton_changed")
    result = {}
    for source_leaf, target_leaf in zip(left["leaves"], right["leaves"]):
        if source_leaf["path"] != target_leaf["path"]:
            raise SubtitleRewritePlanError("structure_changed")
        target_tokens = [match.group(0) for match in _PROTECTED.finditer(target_leaf["value"])]
        if target_tokens != [item["token"] for item in source_leaf["tokens"]]:
            raise SubtitleRewritePlanError("protected_syntax_changed")
        masked = target_leaf["value"]
        for item in source_leaf["tokens"]:
            masked = masked.replace(item["token"], item["marker"], 1)
        if masked.count("\n") + 1 != source_leaf["line_count"]:
            raise SubtitleRewritePlanError("line_count_changed")
        value_id = "subtitle-value-" + _hash({
            "policy": POLICY, "source_sha256": _text_hash(source),
            "path": source_leaf["path"],
            "value_sha256": _text_hash(source_leaf["source"]),
        })
        result[value_id] = masked
    return result, _text_hash(_skeleton(target, right["leaves"]))


def language_validation_text(target: str) -> str:
    """Return cue prose without host-protected technical syntax."""
    parsed = parse(target)
    return "\n".join(_PROTECTED.sub(" ", leaf["value"])
                     for leaf in parsed["leaves"])
