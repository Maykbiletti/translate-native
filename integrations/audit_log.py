#!/usr/bin/env python3
"""Content-free audit records for language-guard release decisions."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:@/-]+")


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _directory_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _validate_directory(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"audit directory must be a real directory: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(f"audit directory must not be writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"audit directory owner is invalid: {path}")


def _existing_directory_anchor(path: Path) -> Path:
    candidate = path
    while True:
        try:
            candidate.lstat()
            return candidate
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise RuntimeError(f"audit directory has no existing anchor: {path}")
            candidate = parent


def _assert_directory_unchanged(
    path: Path, expected: tuple[int, int, int, int, int],
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise RuntimeError(f"audit directory cannot be rechecked: {path}") from error
    _validate_directory(path, current)
    if _directory_identity(current) != expected:
        raise RuntimeError(f"audit directory changed during operation: {path}")


@contextmanager
def _open_audit_directory(path: Path, *, create: bool) -> Iterator[int | None]:
    if os.name == "nt":
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        yield None
        return
    anchor = Path.home()
    try:
        relative = path.parent.relative_to(anchor)
    except ValueError:
        anchor = _existing_directory_anchor(path.parent)
        relative = path.parent.relative_to(anchor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    current_path = anchor
    try:
        try:
            descriptor = os.open(anchor, flags)
            _validate_directory(anchor, os.fstat(descriptor))
            for component in relative.parts:
                current_path = current_path / component
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=descriptor)
                try:
                    _validate_directory(current_path, os.fstat(child))
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            expected = _directory_identity(os.fstat(descriptor))
        except (FileNotFoundError, RuntimeError):
            raise
        except OSError as error:
            raise RuntimeError(
                f"audit directory cannot be opened safely: {current_path}"
            ) from error
        try:
            yield descriptor
        finally:
            _assert_directory_unchanged(path.parent, expected)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_file(path: Path, details: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError(f"{label} must be a regular file: {path}")
    if details.st_nlink != 1:
        raise RuntimeError(f"{label} must not have additional hard links: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise RuntimeError(f"{label} must not be writable outside its owner: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise RuntimeError(f"{label} owner is invalid: {path}")


def _audit_lstat(path: Path, directory: int | None) -> os.stat_result:
    if directory is None:
        return path.lstat()
    return os.stat(path.name, dir_fd=directory, follow_symlinks=False)


def _open_audit_file(path: Path, directory: int | None, flags: int, mode: int = 0o600) -> int:
    if directory is None:
        return os.open(path, flags, mode)
    return os.open(path.name, flags, mode, dir_fd=directory)


def _path_matches_handle(path: Path, handle, label: str, directory: int | None) -> None:
    try:
        opened = os.fstat(handle.fileno())
        current = _audit_lstat(path, directory)
    except OSError as error:
        raise RuntimeError(f"{label} is unreadable: {path}") from error
    _validate_file(path, opened, label)
    _validate_file(path, current, label)
    if _file_identity(opened) != _file_identity(current):
        raise RuntimeError(f"{label} changed while in use: {path}")


@contextmanager
def _protected_append(
    path: Path, label: str, encoding: str, directory: int | None,
) -> Iterator[Any]:
    """Open one append-only state file without following or replacing links."""
    descriptor = None
    for _attempt in range(3):
        try:
            before = _audit_lstat(path, directory)
        except FileNotFoundError:
            flags = (
                os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = _open_audit_file(path, directory, flags)
                before = None
                break
            except FileExistsError:
                continue
            except OSError as error:
                raise RuntimeError(f"{label} cannot be created: {path}") from error
        except OSError as error:
            raise RuntimeError(f"{label} is unreadable: {path}") from error
        _validate_file(path, before, label)
        flags = (
            os.O_RDWR | os.O_APPEND
            | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = _open_audit_file(path, directory, flags)
        except OSError as error:
            raise RuntimeError(f"{label} cannot be opened safely: {path}") from error
        try:
            opened = os.fstat(descriptor)
            _validate_file(path, opened, label)
            if _file_identity(opened) != _file_identity(before):
                raise RuntimeError(f"{label} changed while opening: {path}")
        except (OSError, RuntimeError):
            os.close(descriptor)
            descriptor = None
            raise
        break
    if descriptor is None:
        raise RuntimeError(f"{label} could not be reserved safely: {path}")
    try:
        with os.fdopen(descriptor, "a+", encoding=encoding, newline="\n") as handle:
            descriptor = None
            _path_matches_handle(path, handle, label, directory)
            try:
                yield handle
            finally:
                handle.flush()
                os.fsync(handle.fileno())
                _path_matches_handle(path, handle, label, directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def audit_paths_healthy(path: Path) -> bool:
    """Inspect existing audit state without creating or modifying it."""
    try:
        with _open_audit_directory(path, create=False) as directory:
            for candidate, label in (
                (path, "audit log"),
                (path.with_suffix(path.suffix + ".lock"), "audit lock"),
            ):
                try:
                    details = _audit_lstat(candidate, directory)
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
                try:
                    _validate_file(candidate, details, label)
                except RuntimeError:
                    return False
            return True
    except FileNotFoundError:
        return True
    except (OSError, RuntimeError):
        return False


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
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _open_audit_directory(path, create=True) as directory:
        with _protected_append(lock_path, "audit lock", "ascii", directory) as lock_handle:
            with _exclusive_lock(lock_handle):
                _path_matches_handle(lock_path, lock_handle, "audit lock", directory)
                with _protected_append(path, "audit log", "utf-8", directory) as handle:
                    handle.write(line)
