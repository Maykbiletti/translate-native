#!/usr/bin/env python3
"""Host-owned, fail-closed delivery boundary for agent output.

The untrusted agent must return one JSON envelope on stdout:

    {"target_text": "...", "release_token": "blg6...."}

The trusted host supplies task kind, locale, source text, and signing-key
location as command-line policy. Only the exact text covered by the receipt is
written to stdout. Diagnostics and rejected candidates are never written to
stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Sequence, TypeVar


ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATH = ROOT / "translate-native" / "scripts" / "language_quality.py"
CLIENT_PATH = ROOT / "translate-native" / "scripts" / "guard_service_client.py"
SPEC = importlib.util.spec_from_file_location("blun_delivery_quality", QUALITY_PATH)
assert SPEC and SPEC.loader
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)

CLIENT_SPEC = importlib.util.spec_from_file_location("blun_delivery_service_client", CLIENT_PATH)
assert CLIENT_SPEC and CLIENT_SPEC.loader
SERVICE_CLIENT = importlib.util.module_from_spec(CLIENT_SPEC)
CLIENT_SPEC.loader.exec_module(SERVICE_CLIENT)

DEFAULT_KEY_PATH = Path.home() / ".config" / "blun-language-guard" / "signing.key"
DEFAULT_POLICY_PATH = Path.home() / ".config" / "blun-language-guard" / "delivery-policy.json"
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
MAX_POLICY_BYTES = 64 * 1024
FORBIDDEN_AGENT_FIELDS = {
    "task_kind", "language", "source_text", "content_type",
    "short_text_reviewed", "key_path", "delivery_channel",
}


@dataclass(frozen=True)
class HostPolicy:
    task_kind: str
    language: str
    source_text: str = ""
    content_type: str = "prose"
    short_text_reviewed: bool = False


class DeliveryBlocked(ValueError):
    """Raised when an untrusted candidate is not eligible for delivery."""


def _exact_language(language: str) -> bool:
    return (
        isinstance(language, str)
        and language.casefold() not in {"auto", "all"}
        and re.fullmatch(r"(?:[A-Za-z]{2,8}|x)(?:-[A-Za-z0-9]{1,8})*", language)
        is not None
    )


def validate_policy(policy: HostPolicy) -> None:
    if policy.task_kind not in {"response", "translation"}:
        raise DeliveryBlocked("host policy has an invalid task kind")
    if not _exact_language(policy.language):
        raise DeliveryBlocked("host policy requires an exact language or locale tag")
    if policy.task_kind == "translation" and not policy.source_text.strip():
        raise DeliveryBlocked("translation policy requires the complete source text")
    if policy.task_kind == "response" and policy.source_text:
        raise DeliveryBlocked("response policy cannot contain translation source text")


def parse_envelope(raw: str, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > max_bytes:
        raise DeliveryBlocked("agent envelope exceeds the configured size limit")
    try:
        envelope = json.loads(raw.lstrip("\ufeff"))
    except json.JSONDecodeError as error:
        raise DeliveryBlocked("agent output is not one valid JSON release envelope") from error
    if not isinstance(envelope, dict):
        raise DeliveryBlocked("agent release envelope must be a JSON object")
    forbidden = sorted(FORBIDDEN_AGENT_FIELDS.intersection(envelope))
    if forbidden:
        raise DeliveryBlocked(
            "agent envelope attempts to override host-owned fields: " + ", ".join(forbidden)
        )
    if set(envelope) - {"target_text", "release_token"}:
        raise DeliveryBlocked("agent release envelope contains unsupported fields")
    if not isinstance(envelope.get("target_text"), str) or not envelope["target_text"].strip():
        raise DeliveryBlocked("agent release envelope has no non-empty target_text")
    if not isinstance(envelope.get("release_token"), str) or not envelope["release_token"].strip():
        raise DeliveryBlocked("agent release envelope has no release_token")
    return envelope


def verify_envelope(envelope: dict[str, Any], policy: HostPolicy, key: bytes) -> str:
    validate_policy(policy)
    target = envelope["target_text"]
    verification = QUALITY.verify_receipt(
        envelope["release_token"],
        policy.source_text,
        target,
        policy.language,
        key,
        policy.content_type,
        policy.short_text_reviewed,
        purpose=policy.task_kind,
    )
    if not verification.get("valid"):
        failed = [name for name, passed in verification.get("checks", {}).items() if not passed]
        detail = ", ".join(failed) if failed else "invalid receipt"
        raise DeliveryBlocked("release receipt rejected: " + detail)
    return target


def verify_envelope_with_service(
    envelope: dict[str, Any],
    policy: HostPolicy,
    endpoint: str,
    *,
    service_token: str = "",
    timeout: float = 10.0,
) -> str:
    validate_policy(policy)
    target = envelope["target_text"]
    result = SERVICE_CLIENT.call_guard_service(
        endpoint,
        {
            "operation": "verify",
            "task_kind": policy.task_kind,
            "source_text": policy.source_text,
            "target_text": target,
            "language": policy.language,
            "release_token": envelope["release_token"],
            "content_type": policy.content_type,
            "short_text_reviewed": policy.short_text_reviewed,
        },
        auth_token=service_token,
        timeout=timeout,
    )
    if not result.get("valid"):
        failed = [name for name, passed in result.get("checks", {}).items() if not passed]
        detail = ", ".join(failed) if failed else "invalid receipt"
        raise DeliveryBlocked("isolated guard rejected the receipt: " + detail)
    return target


def load_verification_key(path: Path) -> bytes:
    """Load an existing verifier key without silently creating a new trust root."""
    environment_key = os.environ.get("BLUN_LANGUAGE_GUARD_KEY")
    if environment_key:
        return hashlib.sha256(environment_key.encode("utf-8")).digest()
    try:
        return QUALITY.load_existing_key(path)
    except FileNotFoundError as error:
        raise DeliveryBlocked("signing key is missing; delivery fails closed") from error
    except OSError as error:
        raise DeliveryBlocked("signing key cannot be read") from error
    except ValueError as error:
        if "permissions" in str(error):
            raise DeliveryBlocked("signing key permissions are broader than owner-only") from error
        raise DeliveryBlocked("signing key is invalid") from error


def _policy_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_ctime_ns,
        details.st_mtime_ns,
    )


def _policy_directory_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _validate_policy_directory(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise DeliveryBlocked(f"installed delivery policy directory is invalid: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise DeliveryBlocked(
            f"installed delivery policy directory is writable outside its owner: {path}"
        )
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise DeliveryBlocked(
            f"installed delivery policy directory has the wrong owner: {path}"
        )


def _existing_policy_directory_anchor(path: Path) -> Path:
    candidate = path
    while True:
        try:
            candidate.lstat()
            return candidate
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise DeliveryBlocked(
                    f"installed delivery policy directory has no existing anchor: {path}"
                )
            candidate = parent


def _assert_policy_directory_unchanged(
    path: Path,
    expected: tuple[int, int, int, int, int],
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise DeliveryBlocked(
            f"installed delivery policy directory cannot be rechecked: {path}"
        ) from error
    _validate_policy_directory(path, current)
    if _policy_directory_identity(current) != expected:
        raise DeliveryBlocked(
            f"installed delivery policy directory changed while reading: {path}"
        )


@contextlib.contextmanager
def _open_policy_directory(path: Path) -> Iterator[int | None]:
    if os.name == "nt":
        yield None
        return
    anchor = Path.home()
    try:
        relative = path.parent.relative_to(anchor)
    except ValueError:
        anchor = _existing_policy_directory_anchor(path.parent)
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
            _validate_policy_directory(anchor, os.fstat(descriptor))
            for component in relative.parts:
                current_path = current_path / component
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    yield None
                    return
                try:
                    _validate_policy_directory(current_path, os.fstat(child))
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            expected = _policy_directory_identity(os.fstat(descriptor))
        except DeliveryBlocked:
            raise
        except OSError as error:
            raise DeliveryBlocked(
                f"installed delivery policy directory cannot be opened safely: {current_path}"
            ) from error
        try:
            yield descriptor
        finally:
            _assert_policy_directory_unchanged(path.parent, expected)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _policy_lstat(path: Path, directory: int | None) -> os.stat_result:
    if directory is None:
        return path.lstat()
    return os.stat(path.name, dir_fd=directory, follow_symlinks=False)


def _open_policy_file(path: Path, directory: int | None, flags: int) -> int:
    if directory is None:
        return os.open(path, flags)
    return os.open(path.name, flags, dir_fd=directory)


def _policy_fstat(descriptor: int) -> os.stat_result:
    return os.fstat(descriptor)


def _read_protected_policy_at(
    path: Path,
    directory: int | None,
) -> dict[str, Any] | None:
    try:
        before = _policy_lstat(path, directory)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DeliveryBlocked("installed delivery policy is unreadable") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise DeliveryBlocked("installed delivery policy must be a regular file")
    if before.st_nlink != 1:
        raise DeliveryBlocked("installed delivery policy must not have additional hard links")
    if before.st_size > MAX_POLICY_BYTES:
        raise DeliveryBlocked("installed delivery policy exceeds the size limit")
    if os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077:
        raise DeliveryBlocked("installed delivery policy permissions are broader than owner-only")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise DeliveryBlocked("installed delivery policy owner is invalid")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = _open_policy_file(path, directory, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = _policy_fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _policy_identity(opened) != _policy_identity(before):
                raise DeliveryBlocked("installed delivery policy changed while opening")
            raw = handle.read(MAX_POLICY_BYTES + 1)
            after_read = _policy_fstat(handle.fileno())
        after_path = _policy_lstat(path, directory)
    except DeliveryBlocked:
        raise
    except OSError as error:
        raise DeliveryBlocked("installed delivery policy is unreadable") from error
    if len(raw) > MAX_POLICY_BYTES:
        raise DeliveryBlocked("installed delivery policy exceeds the size limit")
    if (
        _policy_identity(after_read) != _policy_identity(opened)
        or _policy_identity(after_path) != _policy_identity(opened)
    ):
        raise DeliveryBlocked("installed delivery policy changed while reading")
    try:
        policy = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DeliveryBlocked("installed delivery policy is unreadable") from error
    if not isinstance(policy, dict):
        raise DeliveryBlocked("installed delivery policy is invalid")
    return policy


def _read_protected_policy(path: Path) -> dict[str, Any] | None:
    with _open_policy_directory(path) as directory:
        if os.name != "nt" and directory is None:
            return None
        return _read_protected_policy_at(path, directory)


def load_installed_service_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    policy = _read_protected_policy(path)
    if policy is None:
        return {}
    isolated = policy.get("isolated_service")
    if (
        policy.get("mandatory") is not True
        or not isinstance(isolated, dict)
        or isolated.get("required") is not True
        or not isinstance(isolated.get("endpoint"), str)
        or not isolated["endpoint"].strip()
        or len(isolated["endpoint"]) > 4096
        or not isinstance(isolated.get("token_file"), str)
        or not isolated["token_file"].strip()
        or len(isolated["token_file"]) > 4096
        or ("fail_closed" in policy and policy["fail_closed"] is not True)
        or ("direct_delivery_allowed" in policy and policy["direct_delivery_allowed"] is not False)
        or ("raw_streaming_allowed" in policy and policy["raw_streaming_allowed"] is not False)
        or ("on_guard_error" in policy and policy["on_guard_error"] != "block")
    ):
        raise DeliveryBlocked("installed delivery policy is invalid")
    return isolated


ResultT = TypeVar("ResultT")


def guarded_send(
    raw_envelope: str,
    policy: HostPolicy,
    key: bytes,
    send: Callable[[str], ResultT],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ResultT:
    """Verify first, then invoke a synchronous delivery function exactly once."""
    target = verify_envelope(parse_envelope(raw_envelope, max_bytes), policy, key)
    return send(target)


async def guarded_send_async(
    raw_envelope: str,
    policy: HostPolicy,
    key: bytes,
    send: Callable[[str], Awaitable[ResultT]],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ResultT:
    """Verify first, then invoke an asynchronous API/Telegram sender exactly once."""
    target = verify_envelope(parse_envelope(raw_envelope, max_bytes), policy, key)
    return await send(target)


def _read_source(path: Path | None, task_kind: str) -> str:
    if path is None:
        return ""
    if task_kind != "translation":
        raise DeliveryBlocked("--source-file is valid only for translation tasks")
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise DeliveryBlocked("cannot read the trusted translation source") from error


def _untrusted_environment(policy: HostPolicy) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("BLUN_LANGUAGE_GUARD_KEY", None)
    environment.pop("BLUN_LANGUAGE_GUARD_KEY_FILE", None)
    environment.pop("BLUN_LANGUAGE_GUARD_SERVICE_TOKEN", None)
    environment.pop("BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE", None)
    environment["BLUN_LANGUAGE_GUARD_MANDATORY"] = "1"
    environment["BLUN_LANGUAGE_GUARD_TASK_KIND"] = policy.task_kind
    environment["BLUN_LANGUAGE_GUARD_LANGUAGE"] = policy.language
    environment["BLUN_LANGUAGE_GUARD_CONTENT_TYPE"] = policy.content_type
    return environment


def _run_agent(command: Sequence[str], timeout: float, max_bytes: int, policy: HostPolicy) -> str:
    if not command:
        return sys.stdin.read(max_bytes + 1)
    actual = list(command)
    if actual and actual[0] == "--":
        actual = actual[1:]
    if not actual:
        raise DeliveryBlocked("missing agent command after --")
    try:
        completed = subprocess.run(
            actual,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
            timeout=timeout,
            env=_untrusted_environment(policy),
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise DeliveryBlocked("agent process failed before guarded delivery") from error
    if completed.returncode != 0:
        raise DeliveryBlocked(f"agent process exited with status {completed.returncode}")
    if len(completed.stdout.encode("utf-8")) > max_bytes:
        raise DeliveryBlocked("agent envelope exceeds the configured size limit")
    return completed.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver only language-guarded agent output")
    parser.add_argument("--task-kind", required=True, choices=("response", "translation"))
    parser.add_argument("--language", required=True, help="Trusted exact BCP-47 language tag")
    parser.add_argument("--source-file", type=Path, help="Trusted complete source for translations")
    parser.add_argument("--content-type", default="prose")
    parser.add_argument("--short-text-reviewed", action="store_true")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_PATH)
    parser.add_argument("--service-endpoint", default=os.environ.get("BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT", ""))
    parser.add_argument("--service-token-file", type=Path)
    parser.add_argument("--require-service", action="store_true", help="Disable same-user key verification")
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.timeout <= 0 or args.max_bytes <= 0:
            raise DeliveryBlocked("timeout and max-bytes must be positive")
        source = _read_source(args.source_file, args.task_kind)
        policy = HostPolicy(
            task_kind=args.task_kind,
            language=args.language,
            source_text=source,
            content_type=args.content_type,
            short_text_reviewed=args.short_text_reviewed,
        )
        validate_policy(policy)
        raw = _run_agent(args.command, args.timeout, args.max_bytes, policy)
        envelope = parse_envelope(raw, args.max_bytes)
        installed_service = load_installed_service_policy(args.policy_file)
        service_endpoint = args.service_endpoint or str(installed_service.get("endpoint", ""))
        token_file = args.service_token_file
        if token_file is None and installed_service.get("token_file"):
            token_file = Path(str(installed_service["token_file"]))
        require_service = args.require_service or installed_service.get("required") is True
        if service_endpoint:
            service_token = ""
            if token_file:
                service_token = SERVICE_CLIENT.load_service_token(token_file)
            target = verify_envelope_with_service(
                envelope,
                policy,
                service_endpoint,
                service_token=service_token,
                timeout=min(args.timeout, 30.0),
            )
        else:
            if require_service:
                raise DeliveryBlocked("isolated guard service is required")
            key = load_verification_key(args.key_file)
            target = verify_envelope(envelope, policy, key)
    except (DeliveryBlocked, SERVICE_CLIENT.GuardServiceError, OSError) as error:
        print(f"BLOCK: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
