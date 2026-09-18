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
REWRITE_OPERATIONS = {"rewrite", "revise", "revision", "proofread", "proofreading",
                      "naturalize", "same-language-edit"}
REWRITE_CONTENT_TYPES = {"prose", "headline", "cta", "marketing", "ui",
                         "documentation", "seo", "legal"}


class RoutingBlocked(ValueError):
    """Raised when trusted job metadata is contradictory or incomplete."""


@dataclass(frozen=True)
class Route:
    task_kind: str
    language: str
    source_text: str
    content_type: str
    reason: str
    profile_id: str = ""
    request_id: str = ""
    session_id: str = ""
    session_epoch: str = ""


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
    if content_type not in ({"prose", "title", "meta_description", "ui"}
                            | REWRITE_CONTENT_TYPES):
        raise RoutingBlocked("invalid content_type")

    translation_evidence = bool(source.strip()) or operation in TRANSLATION_OPERATIONS
    if explicit:
        if explicit not in {"response", "translation", "rewrite"}:
            raise RoutingBlocked("invalid task_kind")
        task_kind = explicit
        reason = "explicit-host-task-kind"
    elif operation in REWRITE_OPERATIONS:
        task_kind = "rewrite"
        reason = "structured-rewrite-operation"
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
    if task_kind == "rewrite":
        if not source.strip():
            raise RoutingBlocked("rewrite route requires complete source_text")
        if operation in TRANSLATION_OPERATIONS or operation in RESPONSE_OPERATIONS:
            raise RoutingBlocked("host operation conflicts with rewrite task")
    elif operation in REWRITE_OPERATIONS:
        raise RoutingBlocked("rewrite operation conflicts with non-rewrite task")

    language_field = ("target_language" if task_kind == "translation"
                      else "language" if task_kind == "rewrite" else "response_language")
    language = _string(context, language_field).strip() or _string(context, "language").strip()
    if language.casefold() in {"auto", "all"} or not EXACT_LANGUAGE.fullmatch(language):
        raise RoutingBlocked(f"{language_field} must be an exact language or locale tag")
    profile_id = _string(context, "profile_id").strip()
    request_id = _string(context, "request_id").strip()
    session_id = _string(context, "session_id").strip()
    session_epoch = _string(context, "session_epoch").strip()
    if task_kind == "rewrite":
        if (not profile_id or not request_id or not session_id
                or re.fullmatch(r"[0-9a-f]{64}", session_epoch) is None):
            raise RoutingBlocked("rewrite route requires host profile_id, stable request_id, session_id and session_epoch")
        if content_type not in REWRITE_CONTENT_TYPES:
            raise RoutingBlocked("rewrite route has invalid content_type")
    elif profile_id or request_id or session_epoch:
        raise RoutingBlocked("rewrite-only fields conflict with non-rewrite task")
    return Route(task_kind, language, source if task_kind in {"translation", "rewrite"} else "",
                 content_type, reason, profile_id, request_id, session_id, session_epoch)


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
