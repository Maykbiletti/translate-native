#!/usr/bin/env python3
"""Host-owned routing from structured job metadata to the correct release gate."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


EXACT_LANGUAGE = re.compile(r"^(?:[A-Za-z]{2,8}|x)(?:-[A-Za-z0-9]{1,8})*$")
TRANSLATION_OPERATIONS = {
    "translate", "translation", "localize", "localization", "transcreate",
    "translation-review", "translation-proofread", "i18n", "l10n",
}
RESPONSE_OPERATIONS = {"respond", "response", "chat", "answer", "compose"}


class RoutingBlocked(ValueError):
    """Raised when trusted job metadata is contradictory or incomplete."""


@dataclass(frozen=True)
class Route:
    task_kind: str
    language: str
    source_text: str
    content_type: str
    reason: str


def _string(context: dict[str, Any], name: str) -> str:
    value = context.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RoutingBlocked(f"{name} must be a string")
    return value


def route_host_context(context: dict[str, Any]) -> Route:
    if not isinstance(context, dict):
        raise RoutingBlocked("host context must be an object")
    explicit = _string(context, "task_kind").strip().casefold()
    operation = _string(context, "operation").strip().casefold()
    source = _string(context, "source_text")
    content_type = _string(context, "content_type").strip() or "prose"
    if content_type not in {"prose", "title", "meta_description", "ui"}:
        raise RoutingBlocked("invalid content_type")

    translation_evidence = bool(source.strip()) or operation in TRANSLATION_OPERATIONS
    if explicit:
        if explicit not in {"response", "translation"}:
            raise RoutingBlocked("invalid task_kind")
        task_kind = explicit
        reason = "explicit-host-task-kind"
    elif translation_evidence:
        task_kind = "translation"
        reason = "structured-translation-evidence"
    elif not operation or operation in RESPONSE_OPERATIONS:
        task_kind = "response"
        reason = "structured-response-route"
    else:
        raise RoutingBlocked("unknown host operation")

    if task_kind == "translation" and not source.strip():
        raise RoutingBlocked("translation route requires complete source_text")
    if task_kind == "response" and source.strip():
        raise RoutingBlocked("source_text cannot be downgraded to response")
    if task_kind == "response" and operation in TRANSLATION_OPERATIONS:
        raise RoutingBlocked("translation operation cannot be downgraded to response")
    if task_kind == "translation" and operation in RESPONSE_OPERATIONS:
        raise RoutingBlocked("response operation conflicts with translation source")

    language_field = "target_language" if task_kind == "translation" else "response_language"
    language = _string(context, language_field).strip() or _string(context, "language").strip()
    if language.casefold() in {"auto", "all"} or not EXACT_LANGUAGE.fullmatch(language):
        raise RoutingBlocked(f"{language_field} must be an exact language or locale tag")
    return Route(task_kind, language, source if task_kind == "translation" else "", content_type, reason)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve trusted host metadata to a language-guard route")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    try:
        raw = args.input.read_text(encoding="utf-8-sig") if args.input else sys.stdin.read().lstrip("\ufeff")
        route = route_host_context(json.loads(raw))
    except (OSError, json.JSONDecodeError, RoutingBlocked) as error:
        print(json.dumps({"status": "BLOCK", "error": str(error)}))
        return 1
    print(json.dumps({"status": "PASS", **asdict(route)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
