"""Lossless long-HTML planning for same-language native rewriting.

Only rendered text nodes and explicitly linguistic attribute values become
model-owned.  Every other source byte remains in the trusted skeleton.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any, Callable


POLICY = "raw-html-linguistic-spans-v1"
HTML_SPACE = " \t\n\f\r"
MAX_DEPTH = 128
MAX_SPANS = 512
MAX_PATH_CHARS = 2048
MAX_ATTRIBUTE_CHARS = 16384
PACKING_OVERHEAD_CHARS = 640
UNIT_RESERVED_CHARS = 192
CONTEXT_CHARS = 256

TRANSLATABLE_ATTRIBUTES = {
    "alt", "aria-description", "aria-label", "placeholder", "title",
}
TRANSLATABLE_META_NAMES = {
    "application-name", "description", "keywords", "twitter:description",
    "twitter:title",
}
TRANSLATABLE_META_PROPERTIES = {
    "og:description", "og:site_name", "og:title", "twitter:description",
    "twitter:title",
}
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
RAW_TEXT_ELEMENTS = {"script", "style"}
PRESERVED_ELEMENTS = {"code", "kbd", "pre", "samp", "var"}
UNSUPPORTED_ELEMENTS = {
    "iframe", "math", "noembed", "noframes", "noscript", "svg", "template",
    "plaintext", "textarea", "xmp",
}
NAME_PATTERN = r"[A-Za-z][A-Za-z0-9:._-]*"
END_TAG_PATTERN = r"</([A-Za-z][A-Za-z0-9:._-]*)[ \t\n\f\r]*>"
ATTRIBUTE_TOKEN_PATTERN = r"[^ \t\n\f\r=/>\"'`<]+"
ATTRIBUTE_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9._-]*"
DOCTYPE_PATTERN = r"<!doctype[ \t\n\f\r]+html[ \t\n\f\r]*>"
RAW_TEXT_CLOSE_PATTERN = r"</{tag}[ \t\n\f\r]*>"
SCRIPT_AMBIGUITY_PATTERN = r"<!--|-->|<script(?:[ \t\n\f\r/>])"
PROTECTED_PATTERN = (
    r"&(?:[A-Za-z][A-Za-z0-9]+|#\d+|#x[0-9A-Fa-f]+);"
    r"|https?://[^\s<>\"']+|mailto:[^\s<>\"']+"
    r"|\{\{[^{}]+\}\}|\$\{[^{}]+\}|%\{[^{}]+\}|\{[^{}\n]{1,256}\}"
    r"|%(?:\d+\$)?[-+#0 ']*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlLjzt]*[diouxXfFeEgGaAcspn%@]"
)
TEMPLATE_MARKERS = ("{%", "{#", "<%")
UNQUOTED_ATTRIBUTE_FORBIDDEN = frozenset({'"', "'", "`", "=", "<"})
CANDIDATE_FORBIDDEN = frozenset({"<", "&"})
_NAME = re.compile(NAME_PATTERN)
_END_TAG = re.compile(END_TAG_PATTERN)
_PROTECTED = re.compile(PROTECTED_PATTERN)
_ATTRIBUTE_NAME = re.compile(ATTRIBUTE_NAME_PATTERN)
_SCRIPT_AMBIGUITY = re.compile(SCRIPT_AMBIGUITY_PATTERN, re.IGNORECASE)


class HtmlRewritePlanError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = "long_html_" + code


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def effective_policy() -> dict:
    """Return every parser choice that can change model-owned HTML bytes."""
    return {
        "policy": POLICY,
        "html_space_codepoints": [ord(character) for character in HTML_SPACE],
        "translatable_attributes": sorted(TRANSLATABLE_ATTRIBUTES),
        "translatable_meta_names": sorted(TRANSLATABLE_META_NAMES),
        "translatable_meta_properties": sorted(TRANSLATABLE_META_PROPERTIES),
        "void_elements": sorted(VOID_ELEMENTS),
        "raw_text_elements": sorted(RAW_TEXT_ELEMENTS),
        "preserved_elements": sorted(PRESERVED_ELEMENTS),
        "unsupported_elements": sorted(UNSUPPORTED_ELEMENTS),
        "name_pattern": _NAME.pattern,
        "name_flags": _NAME.flags,
        "end_tag_pattern": _END_TAG.pattern,
        "end_tag_flags": _END_TAG.flags,
        "attribute_token_pattern": ATTRIBUTE_TOKEN_PATTERN,
        "attribute_name_pattern": _ATTRIBUTE_NAME.pattern,
        "attribute_name_flags": _ATTRIBUTE_NAME.flags,
        "doctype_pattern": DOCTYPE_PATTERN,
        "raw_text_close_pattern": RAW_TEXT_CLOSE_PATTERN,
        "script_ambiguity_pattern": _SCRIPT_AMBIGUITY.pattern,
        "script_ambiguity_flags": _SCRIPT_AMBIGUITY.flags,
        "protected_pattern": _PROTECTED.pattern,
        "protected_flags": _PROTECTED.flags,
        "template_markers": list(TEMPLATE_MARKERS),
        "unquoted_attribute_forbidden": sorted(UNQUOTED_ATTRIBUTE_FORBIDDEN),
        "candidate_forbidden": sorted(CANDIDATE_FORBIDDEN),
        "linguistic_attribute_quotes": ["\"", "'"],
        "attribute_name_grammar": "ascii-name-v1",
        "reject_invalid_unquoted_attribute_values": True,
        "reject_non_ascii_html_whitespace": True,
        "reject_script_escaped_or_nested_states": True,
        "reject_nonvoid_self_closing": True,
        "reject_mixed_content": True,
        "max_depth": MAX_DEPTH,
        "max_spans": MAX_SPANS,
        "max_path_chars": MAX_PATH_CHARS,
        "max_attribute_chars": MAX_ATTRIBUTE_CHARS,
        "packing_overhead_chars": PACKING_OVERHEAD_CHARS,
        "unit_reserved_chars": UNIT_RESERVED_CHARS,
        "context_chars": CONTEXT_CHARS,
    }


def _tag_end(source: str, start: int) -> int:
    quote = ""
    position = start
    while position < len(source):
        character = source[position]
        if quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            return position + 1
        elif character == "<":
            raise HtmlRewritePlanError("invalid")
        position += 1
    raise HtmlRewritePlanError("invalid")


def _start_tag(source: str, start: int, end: int) -> tuple[str, list[dict], bool]:
    if any(character.isspace() and character not in HTML_SPACE
           for character in source[start:end]):
        raise HtmlRewritePlanError("invalid")
    position = start + 1
    match = _NAME.match(source, position)
    if match is None:
        raise HtmlRewritePlanError("invalid")
    tag = match.group(0).casefold()
    position = match.end()
    attributes: list[dict] = []
    names: set[str] = set()
    self_closing = False
    while position < end - 1:
        while position < end - 1 and source[position] in HTML_SPACE:
            position += 1
        if position >= end - 1:
            break
        if source[position] == "/":
            if source[position + 1:end] != ">":
                raise HtmlRewritePlanError("invalid")
            self_closing = True
            break
        name_match = re.match(ATTRIBUTE_TOKEN_PATTERN, source[position:end - 1])
        if name_match is None:
            raise HtmlRewritePlanError("invalid")
        raw_name = name_match.group(0)
        name = raw_name.casefold()
        if (_ATTRIBUTE_NAME.fullmatch(raw_name) is None
                or ":" in name or name == "xmlns"
                or name.startswith(("v-", "@", "[", "(", "*", "#"))):
            raise HtmlRewritePlanError("unsupported_attribute")
        if name in names:
            raise HtmlRewritePlanError("duplicate_attribute")
        names.add(name)
        position += len(raw_name)
        while position < end - 1 and source[position] in HTML_SPACE:
            position += 1
        value = None
        value_start = value_end = None
        quoted = False
        if position < end - 1 and source[position] == "=":
            position += 1
            while position < end - 1 and source[position] in HTML_SPACE:
                position += 1
            if position >= end - 1:
                raise HtmlRewritePlanError("invalid")
            if source[position] in {'"', "'"}:
                quoted = True
                quote = source[position]
                value_start = position + 1
                value_end = source.find(quote, value_start, end - 1)
                if value_end < 0:
                    raise HtmlRewritePlanError("invalid")
                value = source[value_start:value_end]
                position = value_end + 1
            else:
                value_start = position
                while (position < end - 1 and source[position] not in HTML_SPACE
                       and source[position] not in ">"):
                    if source[position] in UNQUOTED_ATTRIBUTE_FORBIDDEN:
                        raise HtmlRewritePlanError("invalid")
                    position += 1
                value_end = position
                value = source[value_start:value_end]
                if not value:
                    raise HtmlRewritePlanError("invalid")
        if value is not None and len(value) > MAX_ATTRIBUTE_CHARS:
            raise HtmlRewritePlanError("attribute_too_large")
        attributes.append({"name": name, "value": value, "start": value_start,
                           "end": value_end, "quoted": quoted})
    return tag, attributes, self_closing


def _skeleton(source: str, leaves: list[dict]) -> str:
    output, position = [], 0
    for leaf in leaves:
        output.append(source[position:leaf["start"]])
        output.append("\x00" + leaf["kind"] + ":" + leaf["path"] + "\x00")
        position = leaf["end"]
    output.append(source[position:])
    return "".join(output)


def parse(source: str) -> dict:
    if not isinstance(source, str) or not source:
        raise HtmlRewritePlanError("invalid")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise HtmlRewritePlanError("unicode_invalid") from None
    leaves: list[dict] = []
    stack: list[dict] = []
    root_children: dict[str, int] = {}
    root_text_count = 0
    position = 0

    def preserved() -> bool:
        return any(item["preserved"] for item in stack)

    def element_path(tag: str) -> str:
        counts = stack[-1]["children"] if stack else root_children
        index = counts.get(tag, 0)
        counts[tag] = index + 1
        parent = stack[-1]["path"] if stack else "$html"
        path = f"{parent}/{tag}[{index}]"
        if len(path) > MAX_PATH_CHARS:
            raise HtmlRewritePlanError("path_too_large")
        return path

    def add_leaf(path: str, kind: str, start: int, end: int,
                 quote: str = "") -> int:
        raw = source[start:end]
        if start >= end or not raw.strip():
            return 0
        # Do not let generic brace placeholders hide executable template
        # delimiters from the explicit unsupported-syntax check below.
        if any(marker in raw for marker in TEMPLATE_MARKERS):
            raise HtmlRewritePlanError("unsupported_template_or_entity")
        protected = list(_PROTECTED.finditer(raw))
        remainder = _PROTECTED.sub("", raw)
        if "&" in remainder:
            raise HtmlRewritePlanError("unsupported_template_or_entity")
        added = 0
        cursor = 0
        for match in protected + [None]:
            relative_end = match.start() if match is not None else len(raw)
            part = raw[cursor:relative_end]
            if part.strip():
                if len(leaves) >= MAX_SPANS:
                    raise HtmlRewritePlanError("too_many_spans")
                part_start, part_end = start + cursor, start + relative_end
                part_path = f"{path}/part[{added}]"
                leaves.append({"path": part_path, "kind": kind,
                               "start": part_start, "end": part_end,
                               "source": part, "quote": quote,
                               "source_sha256": _text_hash(part)})
                added += 1
            if match is not None:
                cursor = match.end()
        return added

    while position < len(source):
        if stack and stack[-1]["tag"] in RAW_TEXT_ELEMENTS:
            tag = re.escape(stack[-1]["tag"])
            closing = re.search(RAW_TEXT_CLOSE_PATTERN.format(tag=tag),
                                source[position:], re.IGNORECASE)
            if closing is None:
                raise HtmlRewritePlanError("unclosed_raw_text")
            raw_body = source[position:position + closing.start()]
            if (stack[-1]["tag"] == "script"
                    and _SCRIPT_AMBIGUITY.search(raw_body)):
                # HTML's script escaped/double-escaped tokenizer states can
                # make an apparent closing tag non-closing. This bounded
                # policy rejects the ambiguity instead of approximating it.
                raise HtmlRewritePlanError("ambiguous_script_raw_text")
            position += closing.start()
        if source[position] != "<":
            end = source.find("<", position)
            end = len(source) if end < 0 else end
            if stack:
                text_index = stack[-1]["text_count"]
                stack[-1]["text_count"] += 1
                path = f"{stack[-1]['path']}/text()[{text_index}]"
            else:
                path = f"$html/text()[{root_text_count}]"
                root_text_count += 1
            if not preserved():
                added = add_leaf(path, "text", position, end)
                if added and stack:
                    stack[-1]["linguistic"] = True
            position = end
            continue
        if source.startswith("<!--", position):
            end = source.find("-->", position + 4)
            if end < 0 or "--" in source[position + 4:end]:
                raise HtmlRewritePlanError("invalid")
            position = end + 3
            continue
        if source.startswith("<![CDATA[", position):
            raise HtmlRewritePlanError("unsupported_declaration")
        if source.startswith("<?", position):
            raise HtmlRewritePlanError("unsupported_declaration")
        if source.startswith("<!", position):
            end = _tag_end(source, position + 2)
            if re.fullmatch(DOCTYPE_PATTERN, source[position:end],
                            re.IGNORECASE) is None:
                raise HtmlRewritePlanError("unsupported_declaration")
            position = end
            continue
        if source.startswith("</", position):
            end = source.find(">", position + 2)
            if end < 0:
                raise HtmlRewritePlanError("invalid")
            raw = source[position:end + 1]
            match = _END_TAG.fullmatch(raw)
            if match is None or not stack or stack[-1]["tag"] != match.group(1).casefold():
                raise HtmlRewritePlanError("unbalanced")
            closed = stack.pop()
            if closed["linguistic"] and closed["child_elements"]:
                raise HtmlRewritePlanError("mixed_content")
            position = end + 1
            continue
        end = _tag_end(source, position + 1)
        tag, attributes, self_closing = _start_tag(source, position, end)
        if ":" in tag or tag in UNSUPPORTED_ELEMENTS:
            raise HtmlRewritePlanError("unsupported_element")
        # In HTML (unlike XML), a trailing slash does not close ordinary
        # elements. Accepting <div/> would make our stack disagree with the
        # browser tree and could move later text across a protection boundary.
        if self_closing and tag not in VOID_ELEMENTS:
            raise HtmlRewritePlanError("unsupported_self_closing_element")
        if stack:
            stack[-1]["child_elements"] += 1
        path = element_path(tag)
        attribute_map = {item["name"]: item["value"] or "" for item in attributes}
        protected = preserved() or tag in PRESERVED_ELEMENTS or tag in RAW_TEXT_ELEMENTS
        if not protected:
            meta_name = html.unescape(attribute_map.get("name", "")).casefold()
            meta_property = html.unescape(attribute_map.get("property", "")).casefold()
            for item in attributes:
                linguistic_meta = item["name"] == "content" and tag == "meta" and (
                    meta_name in TRANSLATABLE_META_NAMES
                    or meta_property in TRANSLATABLE_META_PROPERTIES)
                if item["value"] and (item["name"] in TRANSLATABLE_ATTRIBUTES
                                      or linguistic_meta):
                    quote = source[item["start"] - 1] if item["quoted"] else ""
                    if quote not in {'"', "'"}:
                        raise HtmlRewritePlanError("unquoted_linguistic_attribute")
                    add_leaf(path + "/@" + item["name"], "attribute",
                             item["start"], item["end"], quote)
        if tag not in VOID_ELEMENTS and not self_closing:
            if len(stack) >= MAX_DEPTH:
                raise HtmlRewritePlanError("too_deep")
            stack.append({"tag": tag, "path": path, "children": {},
                          "text_count": 0, "preserved": protected,
                          "linguistic": False, "child_elements": 0})
        position = end
    if stack:
        raise HtmlRewritePlanError("unbalanced")
    if not leaves:
        raise HtmlRewritePlanError("no_linguistic_spans")
    skeleton = _skeleton(source, leaves)
    return {"leaves": leaves, "skeleton_sha256": _text_hash(skeleton)}


def build_plan(source: str, chunk_chars: int, max_groups: int,
               document_plan: Callable) -> tuple[dict, dict]:
    parsed = parse(source)
    # The shared Unicode segmenter has a 256-character minimum. Small budgets
    # may still carry short values; the exact packed-cost check below rejects
    # any value that does not actually fit.
    owned_chunk_chars = max(
        256, chunk_chars - PACKING_OVERHEAD_CHARS
        - UNIT_RESERVED_CHARS - (2 * CONTEXT_CHARS))
    groups: list[dict] = []
    units: list[dict] = []
    manifest_leaves = []
    for leaf in parsed["leaves"]:
        try:
            _manifest, chunks, separators, prefix, suffix = document_plan(
                leaf["source"], owned_chunk_chars, max_groups)
        except Exception as error:
            if getattr(error, "code", None) in {
                    "rewrite.long_document_too_large",
                    "rewrite.long_document_safe_boundary_unavailable"}:
                raise HtmlRewritePlanError("span_too_large") from None
            raise
        unit_ids = []
        for index, (chunk, body) in enumerate(chunks):
            unit_id = "html-span-" + _hash({
                "policy": POLICY, "path": leaf["path"], "kind": leaf["kind"],
                "index": index, "source_sha256": chunk["source_sha256"],
            })
            unit_ids.append(unit_id)
            units.append({"value_id": unit_id, "source": body,
                          "source_sha256": chunk["source_sha256"],
                          "path": leaf["path"], "kind": leaf["kind"],
                          "leaf_index": len(manifest_leaves), "part_index": index,
                          "previous_context": body[:0], "next_context": body[:0]})
        for index, unit_id in enumerate(unit_ids):
            unit = next(item for item in units if item["value_id"] == unit_id)
            unit["previous_context"] = (chunks[index - 1][1][-CONTEXT_CHARS:]
                                        if index else "")
            unit["next_context"] = (chunks[index + 1][1][:CONTEXT_CHARS]
                                    if index + 1 < len(chunks) else "")
        manifest_leaves.append({
            "path": leaf["path"], "kind": leaf["kind"],
            "source_sha256": leaf["source_sha256"],
            "prefix": prefix, "suffix": suffix, "separators": separators,
            "unit_ids": unit_ids,
        })
    current: list[dict] = []
    current_cost = PACKING_OVERHEAD_CHARS
    for unit in units:
        cost = (len(unit["source"]) + len(unit["previous_context"])
                + len(unit["next_context"]) + UNIT_RESERVED_CHARS)
        if cost + PACKING_OVERHEAD_CHARS > chunk_chars:
            raise HtmlRewritePlanError("span_too_large")
        if current and current_cost + cost > chunk_chars:
            groups.append({"units": current})
            current, current_cost = [], PACKING_OVERHEAD_CHARS
        current.append(unit)
        current_cost += cost
    if current:
        groups.append({"units": current})
    if not groups or len(groups) > max_groups:
        raise HtmlRewritePlanError("too_large")
    for index, group in enumerate(groups):
        group["index"] = index
        group["chunk_id"] = "rewrite-html-chunk-" + _hash({
            "policy": POLICY, "index": index,
            "values": [(item["value_id"], item["source_sha256"])
                       for item in group["units"]],
        })
    manifest = {
        "schema": "translate-native.long-html-manifest.v1", "policy": POLICY,
        "source_sha256": _text_hash(source), "source_chars": len(source),
        "source_bytes": len(source.encode("utf-8")),
        "skeleton_sha256": parsed["skeleton_sha256"],
        "leaves": manifest_leaves,
        "groups": [{"index": item["index"], "chunk_id": item["chunk_id"],
                    "value_ids": [unit["value_id"] for unit in item["units"]]}
                   for item in groups],
    }
    return manifest, {"leaves": parsed["leaves"], "manifest_leaves": manifest_leaves,
                      "groups": groups}


def assemble(source: str, state: dict, candidates: dict[str, str]) -> str:
    replacements = []
    expected = set()
    for raw_leaf, leaf in zip(state["leaves"], state["manifest_leaves"]):
        parts = [leaf["prefix"]]
        for index, value_id in enumerate(leaf["unit_ids"]):
            expected.add(value_id)
            candidate = candidates.get(value_id)
            if (not isinstance(candidate, str) or not candidate
                    or candidate != candidate.strip()
                    or any(character in candidate for character in CANDIDATE_FORBIDDEN)
                    or (raw_leaf["quote"] and raw_leaf["quote"] in candidate)):
                raise HtmlRewritePlanError("candidate_invalid")
            try:
                candidate.encode("utf-8")
            except UnicodeEncodeError:
                raise HtmlRewritePlanError("candidate_invalid") from None
            parts.extend((candidate, leaf["separators"][index]))
        parts.append(leaf["suffix"])
        replacements.append((raw_leaf["start"], raw_leaf["end"], "".join(parts)))
    if set(candidates) != expected:
        raise HtmlRewritePlanError("candidate_invalid")
    output, position = [], 0
    for start, end, value in replacements:
        output.extend((source[position:start], value))
        position = end
    output.append(source[position:])
    target = "".join(output)
    target_parsed = parse(target)
    if (target_parsed["skeleton_sha256"]
            != _text_hash(_skeleton(source, state["leaves"]))):
        raise HtmlRewritePlanError("skeleton_changed")
    return target


def target_value_map(target: str) -> tuple[dict[str, str], str]:
    parsed = parse(target)
    values = {}
    for leaf in parsed["leaves"]:
        if leaf["path"] in values:
            raise HtmlRewritePlanError("path_collision")
        values[leaf["path"]] = leaf["source"]
    return values, parsed["skeleton_sha256"]
