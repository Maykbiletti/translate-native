"""Lossless long-Markdown planning for same-language native rewriting.

The first policy intentionally supports a narrow CommonMark-compatible subset.
Only plain prose spans become model-owned. Markdown syntax, code, quotations,
links, destinations, escapes, placeholders and all other source bytes remain in
the trusted skeleton. Ambiguous extensions remain fail-closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Callable


POLICY = "raw-markdown-prose-spans-v1"
PROFILE = "commonmark-conservative-v1"
NATIVE_REVIEW_PROJECTION = "markdown-rendered-prose-target-only-v1"
MAX_SPANS = 1024
MAX_PATH_CHARS = 2048
PACKING_OVERHEAD_CHARS = 640
UNIT_RESERVED_CHARS = 192
CONTEXT_CHARS = 256
FENCE_OPEN = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)(?P<eol>\r\n|\n|\r)?$")
ATX_HEADING = re.compile(r"^(?P<prefix> {0,3}#{1,6}[ \t]+)(?P<body>.*?)(?P<closing>[ \t]+#+[ \t]*)?$")
LIST_MARKER = re.compile(r"^(?P<prefix> {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]+)(?P<body>.*)$")
TASK_MARKER = re.compile(r"^(?P<prefix>\[[ xX]\][ \t]+)(?P<body>.*)$")
BLOCKQUOTE = re.compile(r"^ {0,3}>")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]\r\n]+\]:")
THEMATIC_BREAK = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
DIRECTIVE = re.compile(r"^ {0,3}(?::{3,}|!{3}|\?{3}|\{[%#]|<%)")
MDX_STATEMENT = re.compile(r"^ {0,3}(?:import|export)(?:[ \t]|$)")
INLINE_LINK = re.compile(
    r"!?\[[^\[\]\r\n]+\]\((?:<[^<>\r\n]+>|[^()\s\r\n]+)"
    r"(?:[ \t]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'))?\)"
)
REFERENCE_LINK = re.compile(r"!?\[[^\[\]\r\n]+\]\[[^\[\]\r\n]*\]")
BRACKET_LABEL = re.compile(r"\[[^\[\]\r\n]{1,256}\]")
AUTOLINK = re.compile(
    r"<(?:https?://[^<>\s]+|mailto:[^<>\s]+|"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)>"
)
PROTECTED = re.compile(
    r"&(?:amp|lt|gt|quot|apos|#(?:[0-9]+|x[0-9A-Fa-f]+));"
    r"|https?://[^\s<>\"'`]+|mailto:[^\s<>\"'`]+"
    r"|[\w.!#$%&'*+/=?^_`{|}~-]+@"
    r"\w(?:[\w.-]{0,251}\w)?"
    r"|\{\{[^{}\r\n]+\}\}|\$\{[^{}\r\n]+\}|%\{[^{}\r\n]+\}"
    r"|\{[A-Za-z_][A-Za-z0-9_.:-]{0,255}\}"
    r"|%(?:\d+\$)?[-+#0 ']*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlLjzt]*[diouxXfFeEgGaAcspn%@]"
)
EMAIL_TOKEN = re.compile(r"[^\s<>\"'`]+@[^\s<>\"'`]+")
SPECIAL = frozenset("`\\[]<>*_~#|{}")
CANDIDATE_FORBIDDEN = frozenset("`\\[]<>*_~#|{}&")
BLOCK_INJECTION = re.compile(r"^(?:#{1,6}|[-+*>]|[0-9]{1,9}[.)])[ \t]+")
MARKDOWN_BLOCK_INTENT_PATTERN = re.compile(
    r"^(?: {0,3}(?:#{1,6}[ \t]+|>|```|~~~|[-+*][ \t]+|"
    r"[0-9]{1,9}[.)][ \t]+|\[[^\]\r\n]+\]:|:::{1,}|!{3}(?:[ \t]|$)|"
    r"\?{3}(?:[ \t]|$)|(?:import|export)(?:[ \t]|$))|"
    r"(?: {4}| {0,3}\t)\S| {0,3}(?:=+|-{3,})[ \t]*$|"
    r"[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*:?-{3,}:?[ \t]*\|?[ \t]*$)",
    re.MULTILINE,
)
MARKDOWN_INTENT_PATTERN = re.compile(
    r"^(?: {0,3}(?:#{1,6}[ \t]+|>|```|~~~|[-+*][ \t]+|"
    r"[0-9]{1,9}[.)][ \t]+|\[[^\]\r\n]+\]:|:::{1,})|"
    r" {0,3}(?:!{3}|\?{3})(?:[ \t]|$)|"
    r"(?: {4}| {0,3}\t)\S| {0,3}(?:=+|-{3,})[ \t]*$|"
    r" {0,3}(?:import|export)(?:[ \t]|$)|"
    r"[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*:?-{3,}:?[ \t]*\|?[ \t]*$)|"
    r"`|\\|[*_~]|\{%|\{#|<%|&(?:[A-Za-z][A-Za-z0-9]+|#[xX]?[0-9A-Fa-f]+);|"
    r"[ \t]{2}(?:\r?\n|\r)|!?\[[^\]\r\n]+\](?:\(|\[)|"
    r"(?<!\*)\*[^*\r\n]+\*|(?<!_)_[^_\r\n]+_|~~[^~\r\n]+~~",
    re.MULTILINE,
)
MULTILINE_LINK_INTENT = re.compile(r"!?\[[^\]]*\](?:\(|\[)")
MDX_EXPRESSION_INTENT = re.compile(
    r"\{(?=[^{}\r\n]{1,256}\})(?=[^{}\r\n]*(?:[()=+\-*/%!?&|<>]|[ \t]))"
    r"[^{}\r\n]+\}"
)
SIMPLE_PLACEHOLDER = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,255}")
MUSTACHE_PLACEHOLDER = re.compile(r"[ \t]*[A-Za-z_][A-Za-z0-9_.:-]{0,255}[ \t]*")


class MarkdownRewritePlanError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = "long_markdown_" + code


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
        "profile": PROFILE,
        "native_review_projection": NATIVE_REVIEW_PROJECTION,
        "fence_open_pattern": FENCE_OPEN.pattern,
        "fence_open_flags": FENCE_OPEN.flags,
        "atx_heading_pattern": ATX_HEADING.pattern,
        "atx_heading_flags": ATX_HEADING.flags,
        "list_marker_pattern": LIST_MARKER.pattern,
        "list_marker_flags": LIST_MARKER.flags,
        "task_marker_pattern": TASK_MARKER.pattern,
        "task_marker_flags": TASK_MARKER.flags,
        "blockquote_pattern": BLOCKQUOTE.pattern,
        "blockquote_flags": BLOCKQUOTE.flags,
        "reference_definition_pattern": REFERENCE_DEFINITION.pattern,
        "reference_definition_flags": REFERENCE_DEFINITION.flags,
        "thematic_break_pattern": THEMATIC_BREAK.pattern,
        "thematic_break_flags": THEMATIC_BREAK.flags,
        "setext_underline_pattern": SETEXT_UNDERLINE.pattern,
        "setext_underline_flags": SETEXT_UNDERLINE.flags,
        "directive_pattern": DIRECTIVE.pattern,
        "directive_flags": DIRECTIVE.flags,
        "mdx_statement_pattern": MDX_STATEMENT.pattern,
        "mdx_statement_flags": MDX_STATEMENT.flags,
        "intent_pattern": MARKDOWN_INTENT_PATTERN.pattern,
        "intent_pattern_flags": MARKDOWN_INTENT_PATTERN.flags,
        "block_intent_pattern": MARKDOWN_BLOCK_INTENT_PATTERN.pattern,
        "block_intent_pattern_flags": MARKDOWN_BLOCK_INTENT_PATTERN.flags,
        "multiline_link_intent_pattern": MULTILINE_LINK_INTENT.pattern,
        "multiline_link_intent_flags": MULTILINE_LINK_INTENT.flags,
        "mdx_expression_intent_pattern": MDX_EXPRESSION_INTENT.pattern,
        "mdx_expression_intent_flags": MDX_EXPRESSION_INTENT.flags,
        "simple_placeholder_pattern": SIMPLE_PLACEHOLDER.pattern,
        "simple_placeholder_flags": SIMPLE_PLACEHOLDER.flags,
        "mustache_placeholder_pattern": MUSTACHE_PLACEHOLDER.pattern,
        "mustache_placeholder_flags": MUSTACHE_PLACEHOLDER.flags,
        "mdx_brace_scanner": "balanced-across-lines-except-exact-placeholder-v1",
        "inline_link_pattern": INLINE_LINK.pattern,
        "inline_link_flags": INLINE_LINK.flags,
        "reference_link_pattern": REFERENCE_LINK.pattern,
        "reference_link_flags": REFERENCE_LINK.flags,
        "bracket_label_pattern": BRACKET_LABEL.pattern,
        "bracket_label_flags": BRACKET_LABEL.flags,
        "autolink_pattern": AUTOLINK.pattern,
        "autolink_flags": AUTOLINK.flags,
        "protected_pattern": PROTECTED.pattern,
        "protected_flags": PROTECTED.flags,
        "email_token_pattern": EMAIL_TOKEN.pattern,
        "email_token_flags": EMAIL_TOKEN.flags,
        "unmatched_at_sign_rejected": True,
        "escaped_bracket_constructs_rejected": True,
        "reference_continuations_host_owned": True,
        "emphasis_capable_lines_host_owned": True,
        "unmatched_braces_rejected": True,
        "special_characters": sorted(SPECIAL),
        "candidate_forbidden": sorted(CANDIDATE_FORBIDDEN),
        "block_injection_pattern": BLOCK_INJECTION.pattern,
        "block_injection_flags": BLOCK_INJECTION.flags,
        "block_injection_rejected_by_target_reparse": True,
        "source_nfc_required": True,
        "intent_line_endings": "crlf-and-cr-normalized-to-lf-view-v1",
        "initial_bom_host_owned": True,
        "fenced_code_host_owned": True,
        "indented_code_host_owned": True,
        "blockquotes_host_owned": True,
        "front_matter_host_owned": True,
        "links_host_owned": True,
        "raw_html_rejected": True,
        "tables_rejected": True,
        "directives_rejected": True,
        "max_spans": MAX_SPANS,
        "max_path_chars": MAX_PATH_CHARS,
        "packing_overhead_chars": PACKING_OVERHEAD_CHARS,
        "unit_reserved_chars": UNIT_RESERVED_CHARS,
        "context_chars": CONTEXT_CHARS,
    }


def looks_like_markdown(source: str) -> bool:
    if not isinstance(source, str):
        return False
    view = source[1:] if source.startswith("\ufeff") else source
    view = view.replace("\r\n", "\n").replace("\r", "\n")
    return (MARKDOWN_INTENT_PATTERN.search(view) is not None
            or MULTILINE_LINK_INTENT.search(view) is not None
            or MDX_EXPRESSION_INTENT.search(view) is not None
            or AUTOLINK.search(view) is not None
            or _has_mdx_expression_intent(view))


def has_block_markdown_intent(source: str) -> bool:
    if not isinstance(source, str):
        return False
    view = source[1:] if source.startswith("\ufeff") else source
    view = view.replace("\r\n", "\n").replace("\r", "\n")
    return MARKDOWN_BLOCK_INTENT_PATTERN.search(view) is not None


def _has_mdx_expression_intent(source: str) -> bool:
    """Find balanced JSX/MDX braces while retaining simple placeholders."""
    index = 0
    while index < len(source):
        if source[index] != "{":
            index += 1
            continue
        if index and source[index - 1] in "$%":
            index += 1
            continue
        if source.startswith("{{", index):
            close = source.find("}}", index + 2)
            if close < 0:
                return True
            if MUSTACHE_PLACEHOLDER.fullmatch(
                    source[index + 2:close]) is None:
                return True
            index = close + 2
            continue
        depth = 1
        position = index + 1
        quote = ""
        escaped = False
        while position < len(source) and depth:
            character = source[position]
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
            elif character in {'"', "'"}:
                quote = character
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            position += 1
        if depth:
            return True
        inner = source[index + 1:position - 1]
        if SIMPLE_PLACEHOLDER.fullmatch(inner) is None:
            return True
        index = position
    return False


def _leading_columns(line: str) -> int:
    columns = 0
    for character in line:
        if character == " ":
            columns += 1
        elif character == "\t":
            columns += 4 - (columns % 4)
        else:
            break
    return columns


def _line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _skeleton(source: str, leaves: list[dict]) -> str:
    output, position = [], 0
    for leaf in leaves:
        output.append(source[position:leaf["start"]])
        output.append("\x00" + leaf["kind"] + ":" + leaf["path"] + "\x00")
        position = leaf["end"]
    output.append(source[position:])
    return "".join(output)


def _fence_close(line: str, marker: str, minimum: int) -> bool:
    body, _eol = _line_ending(line)
    match = re.fullmatch(r" {0,3}(?P<fence>" + re.escape(marker) + r"{" +
                         str(minimum) + r",})[ \t]*", body)
    return match is not None


def _inline_prose_ranges(text: str, absolute_start: int) -> list[tuple[int, int]]:
    """Return raw prose ranges; every Markdown-capable token stays outside."""
    if re.search(r"!?\[[^\r\n]*\\[^\r\n]*\]", text):
        raise MarkdownRewritePlanError("escaped_bracket_unsupported")
    if any(marker in text for marker in "*_~"):
        return []
    if any(marker in text for marker in ("{%", "{#", "%}", "#}")):
        raise MarkdownRewritePlanError("template_unsupported")
    ranges: list[tuple[int, int]] = []
    position = 0

    def add_plain(start: int, end: int) -> None:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end and any(character.isalnum() for character in text[start:end]):
            ranges.append((absolute_start + start, absolute_start + end))

    plain_start = 0
    while position < len(text):
        protected_end = None
        character = text[position]
        if character == "`":
            run = 1
            while position + run < len(text) and text[position + run] == "`":
                run += 1
            close = text.find("`" * run, position + run)
            if close < 0:
                raise MarkdownRewritePlanError("unclosed_inline_code")
            protected_end = close + run
        elif character == "\\":
            if position + 1 >= len(text):
                raise MarkdownRewritePlanError("dangling_escape")
            protected_end = position + 2
        elif text.startswith("![", position) or character == "[":
            for pattern in (INLINE_LINK, REFERENCE_LINK):
                match = pattern.match(text, position)
                if match is not None:
                    protected_end = match.end()
                    break
            if protected_end is None:
                label = BRACKET_LABEL.match(text, position)
                if label is None:
                    raise MarkdownRewritePlanError("unsupported_link")
                if label.end() < len(text) and text[label.end()] in "([":
                    raise MarkdownRewritePlanError("unsupported_link")
                protected_end = label.end()
        elif character == "<":
            match = AUTOLINK.match(text, position)
            if match is None:
                raise MarkdownRewritePlanError("raw_html_unsupported")
            protected_end = match.end()
        elif character == ">":
            raise MarkdownRewritePlanError("raw_html_unsupported")
        elif character == "&":
            match = PROTECTED.match(text, position)
            if match is None:
                raise MarkdownRewritePlanError("entity_unsupported")
            protected_end = match.end()
        elif character == "@":
            raise MarkdownRewritePlanError("email_unsupported")
        else:
            match = EMAIL_TOKEN.match(text, position) or PROTECTED.match(text, position)
            if match is not None:
                protected_end = match.end()
            elif character == "|":
                raise MarkdownRewritePlanError("table_unsupported")
            elif character in "{}":
                raise MarkdownRewritePlanError("template_unsupported")
            elif character in SPECIAL:
                protected_end = position + 1
        if protected_end is None:
            position += 1
            continue
        add_plain(plain_start, position)
        position = protected_end
        plain_start = position
    add_plain(plain_start, len(text))
    return ranges


def parse(source: str) -> dict:
    if not isinstance(source, str) or not source:
        raise MarkdownRewritePlanError("invalid")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise MarkdownRewritePlanError("unicode_invalid") from None
    if "\x00" in source or not unicodedata.is_normalized("NFC", source):
        raise MarkdownRewritePlanError("source_not_nfc")
    lines = source.splitlines(keepends=True)
    if not lines:
        raise MarkdownRewritePlanError("invalid")
    leaves: list[dict] = []
    offsets, cursor = [], 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    if cursor < len(source):
        lines.append(source[cursor:])
        offsets.append(cursor)

    index = 0
    if lines:
        first, _ = _line_ending(lines[0])
        first = first[1:] if first.startswith("\ufeff") else first
        if first == "---":
            index = 1
            while index < len(lines):
                body, _ = _line_ending(lines[index])
                index += 1
                if body in {"---", "..."}:
                    break
            else:
                raise MarkdownRewritePlanError("front_matter_unclosed")
        elif first.startswith("---"):
            raise MarkdownRewritePlanError("front_matter_ambiguous")

    quote_mode = False
    while index < len(lines):
        line = lines[index]
        body, _eol = _line_ending(line)
        start = offsets[index]
        stripped = body.strip(" \t")
        if quote_mode:
            if stripped:
                index += 1
                continue
            quote_mode = False
            index += 1
            continue
        if BLOCKQUOTE.match(body):
            quote_mode = True
            index += 1
            continue
        fence = FENCE_OPEN.fullmatch(line)
        if fence is not None:
            marker = fence.group("fence")[0]
            if marker == "`" and "`" in fence.group("info"):
                raise MarkdownRewritePlanError("fence_invalid")
            minimum = len(fence.group("fence"))
            index += 1
            while index < len(lines) and not _fence_close(lines[index], marker, minimum):
                index += 1
            if index >= len(lines):
                raise MarkdownRewritePlanError("fence_unclosed")
            index += 1
            continue
        if REFERENCE_DEFINITION.match(body):
            index += 1
            first_continuation = True
            while index < len(lines):
                continuation, _ = _line_ending(lines[index])
                if not continuation.strip(" \t"):
                    break
                if (not continuation.startswith((" ", "\t"))
                        and not (first_continuation
                                 and continuation.lstrip(" \t").startswith(
                                     ('"', "'", "(")))):
                    break
                index += 1
                first_continuation = False
            continue
        if _leading_columns(body) >= 4:
            index += 1
            continue
        if DIRECTIVE.match(body):
            raise MarkdownRewritePlanError("directive_unsupported")
        if MDX_STATEMENT.match(body):
            raise MarkdownRewritePlanError("mdx_unsupported")
        if not stripped or THEMATIC_BREAK.fullmatch(body) or SETEXT_UNDERLINE.fullmatch(body):
            index += 1
            continue

        content_start, content_end = 0, len(body)
        block_injection_sensitive = True
        heading = ATX_HEADING.fullmatch(body)
        if heading is not None:
            content_start = heading.start("body")
            content_end = heading.end("body")
            block_injection_sensitive = False
        else:
            item = LIST_MARKER.fullmatch(body)
            if item is not None:
                content_start = item.start("body")
                task = TASK_MARKER.fullmatch(item.group("body"))
                if task is not None:
                    content_start += task.start("body")
        content = body[content_start:content_end]
        ranges = _inline_prose_ranges(content, start + content_start)
        for part, (part_start, part_end) in enumerate(ranges):
            if len(leaves) >= MAX_SPANS:
                raise MarkdownRewritePlanError("too_many_spans")
            path = f"$markdown/line[{index}]/part[{part}]"
            if len(path) > MAX_PATH_CHARS:
                raise MarkdownRewritePlanError("path_too_large")
            raw = source[part_start:part_end]
            leaves.append({
                "path": path, "kind": "prose", "start": part_start,
                "end": part_end, "source": raw, "source_sha256": _text_hash(raw),
                "block_injection_sensitive": (
                    block_injection_sensitive and part == 0
                    and part_start == start + content_start),
            })
        index += 1

    if not leaves:
        raise MarkdownRewritePlanError("no_linguistic_content")
    skeleton = _skeleton(source, leaves)
    return {"leaves": leaves, "skeleton_sha256": _text_hash(skeleton)}


def build_plan(source: str, chunk_chars: int, max_groups: int,
               document_plan: Callable) -> tuple[dict, dict]:
    parsed = parse(source)
    document_sha256 = _text_hash(source)
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
                raise MarkdownRewritePlanError("span_too_large") from None
            raise
        unit_ids = []
        leaf_index = len(manifest_leaves)
        for part_index, (chunk, body) in enumerate(chunks):
            value_id = "markdown-span-" + _hash({
                "policy": POLICY, "path": leaf["path"], "index": part_index,
                "source_sha256": chunk["source_sha256"],
                "document_sha256": document_sha256,
            })
            unit_ids.append(value_id)
            units.append({
                "value_id": value_id, "source": body,
                "source_sha256": chunk["source_sha256"], "path": leaf["path"],
                "kind": leaf["kind"], "leaf_index": leaf_index,
                "part_index": part_index, "previous_context": "", "next_context": "",
            })
        leaf_units = units[-len(unit_ids):] if unit_ids else []
        for part_index, unit in enumerate(leaf_units):
            unit["previous_context"] = (
                chunks[part_index - 1][1][-CONTEXT_CHARS:] if part_index else "")
            unit["next_context"] = (
                chunks[part_index + 1][1][:CONTEXT_CHARS]
                if part_index + 1 < len(chunks) else "")
        manifest_leaves.append({
            "path": leaf["path"], "kind": leaf["kind"],
            "source_sha256": leaf["source_sha256"], "prefix": prefix,
            "suffix": suffix, "separators": separators, "unit_ids": unit_ids,
        })
    current: list[dict] = []
    current_cost = PACKING_OVERHEAD_CHARS
    for unit in units:
        cost = (len(unit["source"]) + len(unit["previous_context"])
                + len(unit["next_context"]) + UNIT_RESERVED_CHARS)
        if cost + PACKING_OVERHEAD_CHARS > chunk_chars:
            raise MarkdownRewritePlanError("span_too_large")
        if current and current_cost + cost > chunk_chars:
            groups.append({"units": current})
            current, current_cost = [], PACKING_OVERHEAD_CHARS
        current.append(unit)
        current_cost += cost
    if current:
        groups.append({"units": current})
    if not groups or len(groups) > max_groups:
        raise MarkdownRewritePlanError("too_large")
    for group_index, group in enumerate(groups):
        group["index"] = group_index
        group["chunk_id"] = "rewrite-markdown-chunk-" + _hash({
            "policy": POLICY, "index": group_index,
            "values": [(item["value_id"], item["source_sha256"])
                       for item in group["units"]],
        })
    manifest = {
        "schema": "translate-native.long-markdown-manifest.v1",
        "policy": POLICY, "profile": PROFILE,
        "source_sha256": document_sha256, "source_chars": len(source),
        "source_bytes": len(source.encode("utf-8")),
        "skeleton_sha256": parsed["skeleton_sha256"],
        "leaves": manifest_leaves,
        "groups": [{"index": item["index"], "chunk_id": item["chunk_id"],
                    "value_ids": [unit["value_id"] for unit in item["units"]]}
                   for item in groups],
    }
    return manifest, {
        "leaves": parsed["leaves"], "manifest_leaves": manifest_leaves,
        "groups": groups,
    }


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
                    or not unicodedata.is_normalized("NFC", candidate)
                    or any(character in candidate for character in CANDIDATE_FORBIDDEN)
                    or (index == 0 and raw_leaf["block_injection_sensitive"]
                        and BLOCK_INJECTION.match(candidate) is not None)
                    or "\r" in candidate or "\n" in candidate or "\x00" in candidate):
                raise MarkdownRewritePlanError("candidate_invalid")
            try:
                candidate.encode("utf-8")
            except UnicodeEncodeError:
                raise MarkdownRewritePlanError("candidate_invalid") from None
            parts.extend((candidate, leaf["separators"][index]))
        parts.append(leaf["suffix"])
        replacements.append((raw_leaf["start"], raw_leaf["end"], "".join(parts)))
    if set(candidates) != expected:
        raise MarkdownRewritePlanError("candidate_invalid")
    output, position = [], 0
    for start, end, value in replacements:
        output.extend((source[position:start], value))
        position = end
    output.append(source[position:])
    target = "".join(output)
    target_parsed = parse(target)
    if target_parsed["skeleton_sha256"] != _text_hash(_skeleton(source, state["leaves"])):
        raise MarkdownRewritePlanError("skeleton_changed")
    return target


def target_value_map(target: str) -> tuple[dict[str, str], str]:
    parsed = parse(target)
    values = {}
    for leaf in parsed["leaves"]:
        if leaf["path"] in values:
            raise MarkdownRewritePlanError("path_collision")
        values[leaf["path"]] = leaf["source"]
    return values, parsed["skeleton_sha256"]


def native_review_text(target: str) -> str:
    """Return ordered rendered prose without Markdown syntax or opaque data."""
    parsed = parse(target)
    values = [leaf["source"] for leaf in parsed["leaves"]
              if leaf["source"].strip()]
    if (not values
            or any(not unicodedata.is_normalized("NFC", value)
                   for value in values)):
        raise MarkdownRewritePlanError("review_projection_invalid")
    projection = "\n\n".join(values)
    if not projection or not unicodedata.is_normalized("NFC", projection):
        raise MarkdownRewritePlanError("review_projection_invalid")
    return projection


def language_validation_text(target: str) -> str:
    return native_review_text(target)
