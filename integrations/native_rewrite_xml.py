"""Lossless long-XML planning for same-language native rewriting.

This deliberately supports a strict XML 1.0 subset. Only element text becomes
model-owned. Markup, attributes, namespace bindings, comments, processing
instructions, references and all other source bytes stay in the trusted
skeleton. DTDs, CDATA and mixed-content elements remain fail-closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ElementTree
from typing import Any, Callable


POLICY = "raw-xml-element-text-v1"
SELECTOR_PROFILE = "android-resources-v1"
NATIVE_REVIEW_PROJECTION = "android-xml-values-target-only-v1"
XML_SPACE = " \t\n\r"
MAX_DEPTH = 128
MAX_SPANS = 512
MAX_PATH_CHARS = 2048
MAX_ATTRIBUTE_CHARS = 16384
PACKING_OVERHEAD_CHARS = 640
UNIT_RESERVED_CHARS = 192
CONTEXT_CHARS = 256
XML_NAMESPACE_URI = "http://www.w3.org/XML/1998/namespace"
XMLNS_NAMESPACE_URI = "http://www.w3.org/2000/xmlns/"
NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9._-]*(?::[A-Za-z_][A-Za-z0-9._-]*)?"
ATTRIBUTE_PATTERN = re.compile(NAME_PATTERN)
END_TAG_PATTERN = re.compile(r"</(" + NAME_PATTERN + r")[ \t\n\r]*>")
XML_DECLARATION_PATTERN = re.compile(
    r"<\?xml[ \t\n\r]+version=(?P<q1>['\"])1\.0(?P=q1)"
    r"(?:[ \t\n\r]+encoding=(?P<q2>['\"])(?i:UTF-8)(?P=q2))?"
    r"(?:[ \t\n\r]+standalone=(?P<q3>['\"])(?:yes|no)(?P=q3))?"
    r"[ \t\n\r]*\?>",
)
REFERENCE_PATTERN = re.compile(
    r"&(?:amp|lt|gt|quot|apos|#(?:[0-9]+|x[0-9A-Fa-f]+));"
)
ANDROID_RESOURCE = (
    r"@(?:\+|\*)?(?:[A-Za-z_][A-Za-z0-9_.-]*:)?"
    r"[A-Za-z_][A-Za-z0-9_.-]*/[A-Za-z_][A-Za-z0-9_.-]*"
)
ANDROID_THEME = (
    r"\?(?:[A-Za-z_][A-Za-z0-9_.-]*:)?"
    r"(?:attr/)?[A-Za-z_][A-Za-z0-9_.-]*"
)
PROTECTED_PATTERN = re.compile(
    r"&(?:amp|lt|gt|quot|apos|#(?:[0-9]+|x[0-9A-Fa-f]+));"
    r"|https?://[^\s<>\"']+|mailto:[^\s<>\"']+"
    r"|(?<![^\s<>\"'@,;:(){}\[\]])"
    r"[^\s<>\"'@,;:(){}\[\]]{1,128}@"
    r"(?:[^\s<>\"'@,;:(){}\[\]/.]{1,63}\.)+"
    r"[^\s<>\"'@,;:(){}\[\]/.]{2,63}"
    r"|\{\{[^{}]+\}\}|\$\{[^{}]+\}|%\{[^{}]+\}|\{[^{}\n]{1,256}\}"
    r"|%(?:\d+\$)?[-+#0 ']*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlLjzt]*[diouxXfFeEgGaAcspn%@]"
    r"|\\(?:" + ANDROID_RESOURCE + "|" + ANDROID_THEME + r"|[@?][A-Za-z0-9_.:-]+)"
    r"|(?<![A-Za-z0-9_\\])(?:" + ANDROID_RESOURCE + "|" + ANDROID_THEME + r")"
    r"|\\(?:[ntrbf'\"\\]|u[0-9A-Fa-f]{4})"
)
CANDIDATE_FORBIDDEN = frozenset({"<", "&"})
TEMPLATE_MARKERS = ("{%", "{#", "<%")


class XmlRewritePlanError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = "long_xml_" + code


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
        "xml_space_codepoints": [ord(character) for character in XML_SPACE],
        "name_pattern": NAME_PATTERN,
        "name_flags": ATTRIBUTE_PATTERN.flags,
        "end_tag_pattern": END_TAG_PATTERN.pattern,
        "end_tag_flags": END_TAG_PATTERN.flags,
        "xml_declaration_pattern": XML_DECLARATION_PATTERN.pattern,
        "xml_declaration_flags": XML_DECLARATION_PATTERN.flags,
        "reference_pattern": REFERENCE_PATTERN.pattern,
        "reference_flags": REFERENCE_PATTERN.flags,
        "android_resource_pattern": ANDROID_RESOURCE,
        "android_theme_pattern": ANDROID_THEME,
        "protected_pattern": PROTECTED_PATTERN.pattern,
        "protected_flags": PROTECTED_PATTERN.flags,
        "candidate_forbidden": sorted(CANDIDATE_FORBIDDEN),
        "template_markers": list(TEMPLATE_MARKERS),
        "opaque_selector_attribute": {"translatable": "false"},
        "android_quote_policy": "outer-double-quotes-host-owned-v1",
        "reject_dtd": True,
        "reject_cdata": True,
        "reject_xinclude": True,
        "semantic_xml_preflight": "stdlib-elementtree-after-declaration-rejection",
        "decode_attribute_references": True,
        "reject_unclassified_android_sigil": True,
        "require_source_nfc_before_creator": True,
        "require_android_selector_attributes": True,
        "plural_quantities": ["zero", "one", "two", "few", "many", "other"],
        "reject_mixed_content": True,
        "translate_attributes": False,
        "selector_profile": SELECTOR_PROFILE,
        "selector_root": "resources",
        "selector_direct_elements": ["string"],
        "selector_collection_elements": ["plurals", "string-array"],
        "selector_collection_item": "item",
        "selector_translatable_false": True,
        "xml_namespace_uri": XML_NAMESPACE_URI,
        "xmlns_namespace_uri": XMLNS_NAMESPACE_URI,
        "require_declared_prefixes": True,
        "max_depth": MAX_DEPTH,
        "max_spans": MAX_SPANS,
        "max_path_chars": MAX_PATH_CHARS,
        "max_attribute_chars": MAX_ATTRIBUTE_CHARS,
        "packing_overhead_chars": PACKING_OVERHEAD_CHARS,
        "unit_reserved_chars": UNIT_RESERVED_CHARS,
        "context_chars": CONTEXT_CHARS,
    }


def _valid_xml_character(character: str) -> bool:
    value = ord(character)
    return (value in {0x9, 0xA, 0xD}
            or 0x20 <= value <= 0xD7FF
            or 0xE000 <= value <= 0xFFFD
            or 0x10000 <= value <= 0x10FFFF)


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
            raise XmlRewritePlanError("invalid")
        position += 1
    raise XmlRewritePlanError("invalid")


def _start_tag(source: str, start: int, end: int) -> tuple[str, list[dict], bool]:
    position = start + 1
    match = ATTRIBUTE_PATTERN.match(source, position)
    if match is None:
        raise XmlRewritePlanError("invalid")
    name = match.group(0)
    position = match.end()
    attributes: list[dict] = []
    raw_names: set[str] = set()
    self_closing = False
    while position < end - 1:
        before_space = position
        while position < end - 1 and source[position] in XML_SPACE:
            position += 1
        if position >= end - 1:
            break
        if source[position] == "/":
            if source[position + 1:end] != ">":
                raise XmlRewritePlanError("invalid")
            self_closing = True
            break
        if position == before_space:
            raise XmlRewritePlanError("invalid")
        attribute = ATTRIBUTE_PATTERN.match(source, position)
        if attribute is None:
            raise XmlRewritePlanError("invalid")
        raw_name = attribute.group(0)
        if raw_name in raw_names:
            raise XmlRewritePlanError("duplicate_attribute")
        raw_names.add(raw_name)
        position = attribute.end()
        while position < end - 1 and source[position] in XML_SPACE:
            position += 1
        if position >= end - 1 or source[position] != "=":
            raise XmlRewritePlanError("invalid")
        position += 1
        while position < end - 1 and source[position] in XML_SPACE:
            position += 1
        if position >= end - 1 or source[position] not in {'"', "'"}:
            raise XmlRewritePlanError("unquoted_attribute")
        quote = source[position]
        value_start = position + 1
        value_end = source.find(quote, value_start, end - 1)
        if value_end < 0:
            raise XmlRewritePlanError("invalid")
        value = source[value_start:value_end]
        if len(value) > MAX_ATTRIBUTE_CHARS or "<" in value:
            raise XmlRewritePlanError("attribute_invalid")
        _validate_references(value)
        attributes.append({"name": raw_name, "value": value,
                           "decoded_value": _decode_references(value)})
        position = value_end + 1
    return name, attributes, self_closing


def _validate_references(value: str) -> None:
    position = 0
    while True:
        position = value.find("&", position)
        if position < 0:
            return
        match = REFERENCE_PATTERN.match(value, position)
        if match is None:
            raise XmlRewritePlanError("unsupported_entity")
        token = match.group(0)
        if token.startswith("&#"):
            try:
                codepoint = int(token[3:-1], 16) if token.startswith("&#x") else int(token[2:-1])
                if not _valid_xml_character(chr(codepoint)):
                    raise ValueError
            except (ValueError, OverflowError):
                raise XmlRewritePlanError("invalid_reference") from None
        position = match.end()


def _decode_references(value: str) -> str:
    """Decode only XML's built-ins/numeric references; DTDs are forbidden."""
    _validate_references(value)
    predefined = {"&amp;": "&", "&lt;": "<", "&gt;": ">",
                  "&quot;": '"', "&apos;": "'"}

    def replace(match: re.Match) -> str:
        token = match.group(0)
        if token in predefined:
            return predefined[token]
        value = int(token[3:-1], 16) if token.startswith("&#x") else int(token[2:-1])
        return chr(value)

    return REFERENCE_PATTERN.sub(replace, value)


def _semantic_preflight(source: str) -> None:
    """Require a conforming XML tree without ever admitting a DTD/entity set."""
    position = 1 if source.startswith("\ufeff") else 0
    while True:
        while position < len(source) and source[position] in XML_SPACE:
            position += 1
        if source.startswith("<!--", position):
            end = source.find("-->", position + 4)
            if end < 0:
                raise XmlRewritePlanError("invalid")
            position = end + 3
            continue
        if source.startswith("<?", position):
            end = source.find("?>", position + 2)
            if end < 0:
                raise XmlRewritePlanError("invalid")
            position = end + 2
            continue
        break
    if source.startswith("<!DOCTYPE", position) or source.startswith("<!ENTITY", position):
        raise XmlRewritePlanError("unsupported_declaration")
    try:
        root = ElementTree.fromstring(source)
    except (ElementTree.ParseError, ValueError):
        raise XmlRewritePlanError("invalid") from None
    xinclude = "{http://www.w3.org/2001/XInclude}"
    if any(isinstance(element.tag, str) and element.tag.startswith(xinclude)
           for element in root.iter()):
        raise XmlRewritePlanError("xinclude_unsupported")


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
        raise XmlRewritePlanError("invalid")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError:
        raise XmlRewritePlanError("unicode_invalid") from None
    if any(not _valid_xml_character(character) for character in source):
        raise XmlRewritePlanError("unicode_invalid")
    if not unicodedata.is_normalized("NFC", source):
        raise XmlRewritePlanError("source_not_nfc")
    if "]]>" in source:
        raise XmlRewritePlanError("invalid")
    _semantic_preflight(source)
    leaves: list[dict] = []
    review_values: list[str] = []
    stack: list[dict] = []
    root_children: dict[str, int] = {}
    position = 1 if source.startswith("\ufeff") else 0
    root_seen = False
    root_closed = False
    declaration_seen = False
    selector_profile = None

    def current_namespaces() -> dict[str, str]:
        return dict(stack[-1]["namespaces"]) if stack else {"xml": XML_NAMESPACE_URI}

    def preserved() -> bool:
        return any(item["preserved"] for item in stack)

    def element_path(name: str) -> str:
        counts = stack[-1]["children"] if stack else root_children
        index = counts.get(name, 0)
        counts[name] = index + 1
        parent = stack[-1]["path"] if stack else "$xml"
        path = f"{parent}/{name}[{index}]"
        if len(path) > MAX_PATH_CHARS:
            raise XmlRewritePlanError("path_too_large")
        return path

    def add_leaf(path: str, start: int, end: int) -> int:
        raw = source[start:end]
        if start >= end or not raw.strip():
            return 0
        android_quoted = len(raw) >= 2 and raw.startswith('"') and raw.endswith('"')
        if android_quoted:
            raw = raw[1:-1]
            start += 1
            end -= 1
            if not raw.strip():
                return 0
        if any(marker in raw for marker in TEMPLATE_MARKERS):
            raise XmlRewritePlanError("unsupported_template")
        _validate_references(raw)
        protected = list(PROTECTED_PATTERN.finditer(raw))
        remainder = PROTECTED_PATTERN.sub("", raw)
        if "&" in remainder:
            raise XmlRewritePlanError("unsupported_entity")
        if "@" in remainder or "\\" in remainder:
            raise XmlRewritePlanError("unsupported_android_syntax")
        if '"' in remainder or ("'" in remainder and not android_quoted):
            raise XmlRewritePlanError("android_string_syntax")
        added = 0
        cursor = 0
        for match in protected + [None]:
            relative_end = match.start() if match is not None else len(raw)
            part = raw[cursor:relative_end]
            if part.strip():
                if len(leaves) >= MAX_SPANS:
                    raise XmlRewritePlanError("too_many_spans")
                part_start, part_end = start + cursor, start + relative_end
                leaves.append({
                    "path": f"{path}/part[{added}]", "kind": "text",
                    "start": part_start, "end": part_end, "source": part,
                    "source_sha256": _text_hash(part),
                    "android_quoted": android_quoted,
                })
                added += 1
            if match is not None:
                cursor = match.end()
        return added

    while position < len(source):
        if source[position] != "<":
            end = source.find("<", position)
            end = len(source) if end < 0 else end
            raw = source[position:end]
            if not stack:
                if raw.strip():
                    raise XmlRewritePlanError("text_outside_root")
            else:
                text_index = stack[-1]["text_count"]
                stack[-1]["text_count"] += 1
                if stack[-1]["eligible"] and not preserved():
                    added = add_leaf(
                        f"{stack[-1]['path']}/text()[{text_index}]", position, end)
                    if added:
                        stack[-1]["linguistic"] = True
                elif raw.strip() and not preserved():
                    raise XmlRewritePlanError("unclassified_text")
            position = end
            continue
        if source.startswith("<!--", position):
            if stack and stack[-1]["eligible"]:
                raise XmlRewritePlanError("selected_content_interrupted")
            end = source.find("-->", position + 4)
            comment = source[position + 4:end]
            if end < 0 or "--" in comment or comment.endswith("-"):
                raise XmlRewritePlanError("invalid_comment")
            position = end + 3
            continue
        if source.startswith("<![CDATA[", position):
            raise XmlRewritePlanError("unsupported_cdata")
        if source.startswith("<!", position):
            raise XmlRewritePlanError("unsupported_declaration")
        if (source.startswith("<?xml", position)
                and position + 5 < len(source)
                and source[position + 5] in XML_SPACE):
            end = source.find("?>", position + 5)
            raw_end = end + 2 if end >= 0 else -1
            if (position not in {0, 1} or declaration_seen or root_seen or raw_end < 0
                    or XML_DECLARATION_PATTERN.fullmatch(source[position:raw_end]) is None):
                raise XmlRewritePlanError("invalid_declaration")
            declaration_seen = True
            position = raw_end
            continue
        if source.startswith("<?", position):
            if stack and stack[-1]["eligible"]:
                raise XmlRewritePlanError("selected_content_interrupted")
            end = source.find("?>", position + 2)
            if end < 0:
                raise XmlRewritePlanError("invalid_processing_instruction")
            target = source[position + 2:end].split(None, 1)[0]
            if (ATTRIBUTE_PATTERN.fullmatch(target) is None
                    or target.casefold() == "xml" or "?>" in source[position + 2:end]):
                raise XmlRewritePlanError("invalid_processing_instruction")
            position = end + 2
            continue
        if source.startswith("</", position):
            end = source.find(">", position + 2)
            if end < 0:
                raise XmlRewritePlanError("invalid")
            match = END_TAG_PATTERN.fullmatch(source[position:end + 1])
            if match is None or not stack or stack[-1]["name"] != match.group(1):
                raise XmlRewritePlanError("unbalanced")
            closed = stack.pop()
            if closed["linguistic"] and closed["child_elements"]:
                raise XmlRewritePlanError("mixed_content")
            if closed["eligible"] and closed["linguistic"] and not closed["preserved"]:
                raw_value = source[closed["content_start"]:position]
                if (len(raw_value) >= 2 and raw_value.startswith('"')
                        and raw_value.endswith('"')):
                    raw_value = raw_value[1:-1]
                value = _decode_references(raw_value)
                if (not value or not value.strip()
                        or unicodedata.normalize("NFC", value) != value):
                    raise XmlRewritePlanError("review_projection_invalid")
                review_values.append(value)
            if not stack:
                root_closed = True
            position = end + 1
            continue
        if root_closed:
            raise XmlRewritePlanError("multiple_roots")
        end = _tag_end(source, position + 1)
        name, attributes, self_closing = _start_tag(source, position, end)
        if not stack:
            if root_seen:
                raise XmlRewritePlanError("multiple_roots")
            root_seen = True
        namespaces = current_namespaces()
        for item in attributes:
            semantic_value = item["decoded_value"]
            if item["name"] == "xmlns":
                if semantic_value in {XML_NAMESPACE_URI, XMLNS_NAMESPACE_URI}:
                    raise XmlRewritePlanError("namespace_invalid")
                if not semantic_value:
                    namespaces.pop("", None)
                else:
                    namespaces[""] = semantic_value
            elif item["name"].startswith("xmlns:"):
                prefix = item["name"].split(":", 1)[1]
                if (prefix in {"xml", "xmlns"} or not semantic_value
                        or semantic_value in {XML_NAMESPACE_URI,
                                              XMLNS_NAMESPACE_URI}):
                    raise XmlRewritePlanError("namespace_invalid")
                namespaces[prefix] = semantic_value
        prefix, _, local_name = name.partition(":")
        if local_name:
            if prefix not in namespaces:
                raise XmlRewritePlanError("undeclared_prefix")
        else:
            local_name = prefix
        if len(stack) == 0:
            if name != "resources" or namespaces.get("", ""):
                raise XmlRewritePlanError("unsupported_profile")
            selector_profile = SELECTOR_PROFILE
        expanded_attributes = set()
        for item in attributes:
            attr_name = item["name"]
            if attr_name == "xmlns" or attr_name.startswith("xmlns:"):
                continue
            attr_prefix, _, attr_local = attr_name.partition(":")
            if attr_local:
                if attr_prefix not in namespaces:
                    raise XmlRewritePlanError("undeclared_prefix")
                expanded = (namespaces[attr_prefix], attr_local)
            else:
                expanded = ("", attr_prefix)
            if expanded in expanded_attributes:
                raise XmlRewritePlanError("duplicate_attribute")
            expanded_attributes.add(expanded)
        semantic_attributes = {item["name"]: item["decoded_value"]
                               for item in attributes}
        xi_uri = "http://www.w3.org/2001/XInclude"
        if ((local_name == "include" and namespaces.get(prefix if name.count(":") else "") == xi_uri)
                or any(item["decoded_value"] == xi_uri for item in attributes
                       if item["name"] == "xmlns" or item["name"].startswith("xmlns:"))):
            raise XmlRewritePlanError("xinclude_unsupported")
        for item in attributes:
            semantic_value = item["decoded_value"]
            if item["name"] == "xml:space" and semantic_value == "preserve":
                raise XmlRewritePlanError("xml_space_unsupported")
        unnamespaced = ":" not in name and not namespaces.get("", "")
        eligible = False
        selector_element = False
        if len(stack) == 1 and name == "string" and unnamespaced:
            if not semantic_attributes.get("name", "").strip():
                raise XmlRewritePlanError("selector_attribute_invalid")
            eligible = True
            selector_element = True
        elif (len(stack) == 1 and name in {"plurals", "string-array"}
              and unnamespaced):
            if not semantic_attributes.get("name", "").strip():
                raise XmlRewritePlanError("selector_attribute_invalid")
            selector_element = True
        elif (len(stack) == 2 and name == "item" and unnamespaced
              and stack[-1]["name"] in {"plurals", "string-array"}
              and not stack[-1]["namespaces"].get("", "")):
            if stack[-1]["name"] == "plurals":
                if semantic_attributes.get("quantity") not in {
                        "zero", "one", "two", "few", "many", "other"}:
                    raise XmlRewritePlanError("selector_attribute_invalid")
            elif "quantity" in semantic_attributes:
                raise XmlRewritePlanError("selector_attribute_invalid")
            eligible = True
            selector_element = True
        preserve_flag = preserved()
        if (selector_element
                and semantic_attributes.get("translatable", "").casefold() == "false"):
            preserve_flag = True
        if (selector_element and any(
                item["name"].split(":")[-1] == "translate"
                and item["decoded_value"].casefold() == "no"
                for item in attributes)):
            preserve_flag = True
        if stack:
            stack[-1]["child_elements"] += 1
        path = element_path(name)
        if not self_closing:
            if len(stack) >= MAX_DEPTH:
                raise XmlRewritePlanError("too_deep")
            stack.append({
                "name": name, "path": path, "children": {}, "text_count": 0,
                "local_name": local_name, "eligible": eligible,
                "namespaces": namespaces, "preserved": preserve_flag,
                "linguistic": False, "child_elements": 0,
                "content_start": end,
            })
        elif not stack:
            root_closed = True
        position = end
    if stack or not root_seen or not root_closed:
        raise XmlRewritePlanError("unbalanced")
    if selector_profile != SELECTOR_PROFILE or not leaves:
        raise XmlRewritePlanError("no_linguistic_spans")
    skeleton = _skeleton(source, leaves)
    return {"leaves": leaves, "review_values": review_values,
            "skeleton_sha256": _text_hash(skeleton)}


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
                raise XmlRewritePlanError("span_too_large") from None
            raise
        unit_ids = []
        leaf_index = len(manifest_leaves)
        for index, (chunk, body) in enumerate(chunks):
            unit_id = "xml-span-" + _hash({
                "policy": POLICY, "path": leaf["path"], "kind": leaf["kind"],
                "index": index, "source_sha256": chunk["source_sha256"],
                "document_sha256": document_sha256,
            })
            unit_ids.append(unit_id)
            units.append({
                "value_id": unit_id, "source": body,
                "source_sha256": chunk["source_sha256"], "path": leaf["path"],
                "kind": leaf["kind"], "leaf_index": leaf_index,
                "android_quoted": leaf["android_quoted"],
                "part_index": index, "previous_context": "", "next_context": "",
            })
        leaf_units = units[-len(unit_ids):] if unit_ids else []
        for index, unit in enumerate(leaf_units):
            unit["previous_context"] = (
                chunks[index - 1][1][-CONTEXT_CHARS:] if index else "")
            unit["next_context"] = (
                chunks[index + 1][1][:CONTEXT_CHARS]
                if index + 1 < len(chunks) else "")
        manifest_leaves.append({
            "path": leaf["path"], "kind": leaf["kind"],
            "source_sha256": leaf["source_sha256"],
            "android_quoted": leaf["android_quoted"],
            "prefix": prefix, "suffix": suffix, "separators": separators,
            "unit_ids": unit_ids,
        })
    current: list[dict] = []
    current_cost = PACKING_OVERHEAD_CHARS
    for unit in units:
        cost = (len(unit["source"]) + len(unit["previous_context"])
                + len(unit["next_context"]) + UNIT_RESERVED_CHARS)
        if cost + PACKING_OVERHEAD_CHARS > chunk_chars:
            raise XmlRewritePlanError("span_too_large")
        if current and current_cost + cost > chunk_chars:
            groups.append({"units": current})
            current, current_cost = [], PACKING_OVERHEAD_CHARS
        current.append(unit)
        current_cost += cost
    if current:
        groups.append({"units": current})
    if not groups or len(groups) > max_groups:
        raise XmlRewritePlanError("too_large")
    for index, group in enumerate(groups):
        group["index"] = index
        group["chunk_id"] = "rewrite-xml-chunk-" + _hash({
            "policy": POLICY, "index": index,
            "values": [(item["value_id"], item["source_sha256"])
                       for item in group["units"]],
        })
    manifest = {
        "schema": "translate-native.long-xml-manifest.v1", "policy": POLICY,
        "selector_profile": SELECTOR_PROFILE,
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
                    or '"' in candidate
                    or ("'" in candidate and not leaf["android_quoted"])
                    or any(character in candidate for character in CANDIDATE_FORBIDDEN)
                    or "]]>" in candidate
                    or any(not _valid_xml_character(character) for character in candidate)):
                raise XmlRewritePlanError("candidate_invalid")
            try:
                candidate.encode("utf-8")
            except UnicodeEncodeError:
                raise XmlRewritePlanError("candidate_invalid") from None
            parts.extend((candidate, leaf["separators"][index]))
        parts.append(leaf["suffix"])
        replacements.append((raw_leaf["start"], raw_leaf["end"], "".join(parts)))
    if set(candidates) != expected:
        raise XmlRewritePlanError("candidate_invalid")
    output, position = [], 0
    for start, end, value in replacements:
        output.extend((source[position:start], value))
        position = end
    output.append(source[position:])
    target = "".join(output)
    target_parsed = parse(target)
    if target_parsed["skeleton_sha256"] != _text_hash(_skeleton(source, state["leaves"])):
        raise XmlRewritePlanError("skeleton_changed")
    return target


def target_value_map(target: str) -> tuple[dict[str, str], str]:
    parsed = parse(target)
    values = {}
    for leaf in parsed["leaves"]:
        if leaf["path"] in values:
            raise XmlRewritePlanError("path_collision")
        values[leaf["path"]] = leaf["source"]
    return values, parsed["skeleton_sha256"]


def native_review_text(target: str) -> str:
    """Return only ordered localized values, never XML resource metadata."""
    parsed = parse(target)
    values = parsed["review_values"]
    if not values or any(not isinstance(value, str) or not value.strip()
                         for value in values):
        raise XmlRewritePlanError("review_projection_invalid")
    projection = "\n\n".join(values)
    if not projection or unicodedata.normalize("NFC", projection) != projection:
        raise XmlRewritePlanError("review_projection_invalid")
    return projection


def language_validation_text(target: str) -> str:
    return native_review_text(target)
