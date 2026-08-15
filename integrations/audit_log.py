#!/usr/bin/env python3
"""Content-free audit records for language-guard release decisions."""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:@/-]+")


def safe_label(value: Any, limit: int = 160) -> str:
    return SAFE_LABEL.sub("_", str(value or "").strip())[:limit]


def finding_codes(result: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for finding in result.get("findings", []):
        if isinstance(finding, dict) and finding.get("code"):
            output.append(safe_label(finding["code"], 80))
    reason = result.get("reason")
    if reason:
        output.append(safe_label(reason, 80))
    checks = result.get("checks")
    if isinstance(checks, dict):
        output.extend(
            f"failed-{safe_label(name, 70)}"
            for name, passed in checks.items()
            if passed is not True
        )
    return sorted(set(filter(None, output)))[:40]


@contextmanager
def _exclusive_lock(handle) -> Iterator[None]:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write("0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_audit(path: Path, record: dict[str, Any]) -> None:
    """Append a bounded record. Callers must never pass source or target text."""
    forbidden = {"source", "source_text", "target", "target_text", "release_token", "service_token"}
    if forbidden.intersection(record):
        raise ValueError("audit records cannot contain text or tokens")
    payload = {
        "ts": int(time.time()),
        "event": safe_label(record.get("event", "decision"), 40),
        "allowed": record.get("allowed") is True,
        "task_kind": safe_label(record.get("task_kind"), 24),
        "language": safe_label(record.get("language"), 40),
        "agent_id": safe_label(record.get("agent_id"), 120),
        "channel": safe_label(record.get("channel"), 80),
        "source_sha256": safe_label(record.get("source_sha256"), 64),
        "target_sha256": safe_label(record.get("target_sha256"), 64),
        "guard_version": safe_label(record.get("guard_version"), 32),
        "codes": [safe_label(value, 80) for value in record.get("codes", [])[:40]],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="ascii") as lock_handle:
        with _exclusive_lock(lock_handle):
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
