#!/usr/bin/env python3
"""Protected deployment factory for the provider-neutral HTTPS review host.

This module contains no model-provider integration.  It turns one protected,
versioned deployment configuration into the existing source-blind response
reviewer and pins both HTTP authentication and host-result attestation.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import base64
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
HTTP_PATH = ROOT / "integrations" / "website_localization_subagent_http.py"
RESPONSE_PATH = ROOT / "integrations" / "response_subagent_review.py"
CONFIG_SCHEMA = "translate-native.response-review-https-runtime.v1"
MAX_CONFIG_BYTES = 64 * 1024
MAX_SECRET_BYTES = 64 * 1024
MAX_REVIEW_SECONDS = 60
MAX_TRANSPORT_MESSAGE_BYTES = 8 * 1024 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
BEARER = re.compile(r"^[A-Za-z0-9._~+/=-]{32,2048}$")
HMAC_SECRET = re.compile(r"^[A-Za-z0-9+/=_-]{43,4096}$")
HMAC_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")


class ResponseReviewRuntimeError(RuntimeError):
    """Configuration failure that prevents the Guard from starting."""


class _CapacityUnavailable(RuntimeError):
    host_subagent_failure = True
    code = "host_capacity"
    retryable = True


class DeadlineURLTransport:
    """Run one HTTPS exchange in a killable process with a wall deadline."""

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        """Stop the worker and its pipe-inheriting descendants without hanging."""
        if process.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except (OSError, ValueError):
                    process.kill()
            else:
                process.kill()
        try:
            process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            # A broken or unkillable worker must not turn cleanup into a new
            # unbounded console wait. Closing our pipe endpoints is safe after
            # the request has already failed closed.
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            try:
                process.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float):
        try:
            request = json.dumps({
                "url": url,
                "headers": dict(headers),
                "body": base64.b64encode(body).decode("ascii"),
                "timeout": timeout,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise HTTP.HTTPReviewHostFailed("http.transport_invalid", retryable=False) from None
        if len(request) > MAX_TRANSPORT_MESSAGE_BYTES:
            raise HTTP.HTTPReviewHostFailed("http.request_invalid", retryable=False)
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--transport-worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={}, start_new_session=os.name != "nt",
            )
            output, _error = process.communicate(request, timeout=float(timeout))
        except subprocess.TimeoutExpired:
            if process is not None:
                self._terminate(process)
            raise HTTP.HTTPReviewHostFailed("http.timeout", retryable=True) from None
        except (OSError, subprocess.SubprocessError):
            if process is not None and process.poll() is None:
                self._terminate(process)
            raise HTTP.HTTPReviewHostFailed("http.network", retryable=True) from None
        if (process.returncode != 0 or not output
                or len(output) > MAX_TRANSPORT_MESSAGE_BYTES):
            raise HTTP.HTTPReviewHostFailed("http.transport_invalid", retryable=True)
        try:
            reply = json.loads(output.decode("utf-8"), object_pairs_hook=_pairs,
                               parse_constant=_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise HTTP.HTTPReviewHostFailed("http.transport_invalid", retryable=True) from None
        if (not isinstance(reply, dict) or set(reply) not in ({"result"}, {"error"})
                or ("error" in reply and (not isinstance(reply["error"], dict)
                    or set(reply["error"]) != {"code", "retryable"}))):
            raise HTTP.HTTPReviewHostFailed("http.transport_invalid", retryable=True)
        if "error" in reply:
            error = reply["error"]
            raise HTTP.HTTPReviewHostFailed(
                error.get("code"), retryable=error.get("retryable"),
            )
        result = reply["result"]
        if (not isinstance(result, dict)
                or set(result) != {"status", "headers", "body"}
                or type(result["status"]) is not int
                or not isinstance(result["headers"], list)
                or any(not isinstance(pair, list) or len(pair) != 2
                       or not all(isinstance(item, str) for item in pair)
                       for pair in result["headers"])
                or not isinstance(result["body"], str)):
            raise HTTP.HTTPReviewHostFailed("http.transport_invalid", retryable=True)
        try:
            response_body = base64.b64decode(result["body"], validate=True)
        except (ValueError, TypeError):
            raise HTTP.HTTPReviewHostFailed("http.transport_invalid", retryable=True) from None
        return HTTP.HTTPResult(
            result["status"], tuple(tuple(pair) for pair in result["headers"]), response_body,
        )


def _load(name: str, path: Path):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ResponseReviewRuntimeError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HTTP = _load("blun_website_localization_subagent_http", HTTP_PATH)


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev, details.st_ino, details.st_nlink, details.st_size,
        details.st_ctime_ns, details.st_mtime_ns,
    )


def _directory_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev, details.st_ino, details.st_mode,
        details.st_uid, details.st_gid,
    )


def _validate_directory(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ResponseReviewRuntimeError(f"review configuration directory is invalid: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise ResponseReviewRuntimeError(
            f"review configuration directory is writable outside its owner: {path}"
        )
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ResponseReviewRuntimeError(
            f"review configuration directory has the wrong owner: {path}"
        )


@contextmanager
def _open_directory(path: Path) -> Iterator[int | None]:
    if os.name == "nt":
        yield None
        return
    anchor = Path(path.anchor)
    relative = path.parent.relative_to(anchor)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    descriptor = None
    current = anchor
    try:
        try:
            descriptor = os.open(anchor, flags)
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ResponseReviewRuntimeError(
                    f"review configuration directory is invalid: {anchor}"
                )
            for component in relative.parts:
                current = current / component
                child = os.open(component, flags, dir_fd=descriptor)
                try:
                    if not stat.S_ISDIR(os.fstat(child).st_mode):
                        raise ResponseReviewRuntimeError(
                            f"review configuration directory is invalid: {current}"
                        )
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            _validate_directory(path.parent, os.fstat(descriptor))
            expected = _directory_identity(os.fstat(descriptor))
        except (FileNotFoundError, ResponseReviewRuntimeError):
            raise
        except OSError as error:
            raise ResponseReviewRuntimeError(
                f"review configuration directory cannot be opened safely: {current}"
            ) from error
        try:
            yield descriptor
        finally:
            try:
                after = path.parent.lstat()
            except OSError as error:
                raise ResponseReviewRuntimeError(
                    f"review configuration directory cannot be rechecked: {path.parent}"
                ) from error
            _validate_directory(path.parent, after)
            if _directory_identity(after) != expected:
                raise ResponseReviewRuntimeError(
                    f"review configuration directory changed while reading: {path.parent}"
                )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _lstat(path: Path, directory: int | None) -> os.stat_result:
    return os.lstat(path) if directory is None else os.stat(
        path.name, dir_fd=directory, follow_symlinks=False,
    )


def _open_file(path: Path, directory: int | None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags) if directory is None else os.open(
        path.name, flags, dir_fd=directory,
    )


def _validate_file(path: Path, details: os.stat_result, maximum: int) -> None:
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ResponseReviewRuntimeError(f"review configuration is not a regular file: {path}")
    if details.st_nlink != 1:
        raise ResponseReviewRuntimeError(f"review configuration has additional hard links: {path}")
    if not 1 <= details.st_size <= maximum:
        raise ResponseReviewRuntimeError(f"review configuration has an invalid size: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
        raise ResponseReviewRuntimeError(f"review configuration must be owner-only: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ResponseReviewRuntimeError(f"review configuration has the wrong owner: {path}")


def _read_protected(path: Path, maximum: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ResponseReviewRuntimeError("review configuration paths must be absolute")
    with _open_directory(path) as directory:
        try:
            before = _lstat(path, directory)
            _validate_file(path, before, maximum)
            descriptor = _open_file(path, directory)
        except ResponseReviewRuntimeError:
            raise
        except OSError as error:
            raise ResponseReviewRuntimeError(
                f"review configuration cannot be opened safely: {path}"
            ) from error
        try:
            opened = os.fstat(descriptor)
            _validate_file(path, opened, maximum)
            if _file_identity(opened) != _file_identity(before):
                raise ResponseReviewRuntimeError(
                    f"review configuration changed while opening: {path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(maximum + 1)
            after = os.fstat(descriptor)
            after_path = _lstat(path, directory)
            if (_file_identity(after) != _file_identity(opened)
                    or _file_identity(after_path) != _file_identity(opened)):
                raise ResponseReviewRuntimeError(
                    f"review configuration changed while reading: {path}"
                )
        finally:
            os.close(descriptor)
    if not 1 <= len(raw) <= maximum:
        raise ResponseReviewRuntimeError(f"review configuration has an invalid size: {path}")
    return raw


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ResponseReviewRuntimeError(f"{name} is invalid")
    return value


def _protected_text(path_value: Any, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(path_value, str) or len(path_value) > 4096:
        raise ResponseReviewRuntimeError(f"{name} path is invalid")
    try:
        text = _read_protected(Path(path_value), MAX_SECRET_BYTES).decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ResponseReviewRuntimeError(f"{name} is not ASCII") from error
    if pattern.fullmatch(text) is None:
        raise ResponseReviewRuntimeError(f"{name} is invalid")
    return text


def _native_brief(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict)
            or set(value) != {"audience", "tone_profile", "target_terms"}
            or any(not isinstance(value[key], str) or not value[key].strip()
                   or len(value[key]) > 2000
                   or unicodedata.normalize("NFC", value[key]) != value[key]
                   for key in ("audience", "tone_profile"))
            or not isinstance(value["target_terms"], list)
            or len(value["target_terms"]) > 100
            or any(not isinstance(term, str) or not term.strip() or len(term) > 256
                   or unicodedata.normalize("NFC", term) != term
                   for term in value["target_terms"])):
        raise ResponseReviewRuntimeError("native_brief is invalid")
    return json.loads(json.dumps(value, ensure_ascii=False))


def load_runtime_config(path: Path) -> dict[str, Any]:
    try:
        raw = _read_protected(path, MAX_CONFIG_BYTES)
        text = raw.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("BOM is not allowed")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except ResponseReviewRuntimeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ResponseReviewRuntimeError("response review configuration is invalid JSON") from error
    expected = {
        "schema", "endpoint", "host_id", "allow_loopback_http",
        "authentication", "attestation", "review",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("schema") != CONFIG_SCHEMA:
        raise ResponseReviewRuntimeError("response review configuration fields are invalid")
    if type(value["allow_loopback_http"]) is not bool:
        raise ResponseReviewRuntimeError("allow_loopback_http must be boolean")
    _identifier(value["host_id"], "host_id")
    authentication = value["authentication"]
    if (not isinstance(authentication, dict)
            or set(authentication) != {"scheme", "token_file"}
            or authentication.get("scheme") != "bearer"):
        raise ResponseReviewRuntimeError("authentication configuration is invalid")
    attestation = value["attestation"]
    if (not isinstance(attestation, dict)
            or set(attestation) != {"algorithm", "key_id", "secret_file"}
            or attestation.get("algorithm") != "hmac-sha256"):
        raise ResponseReviewRuntimeError("attestation configuration is invalid")
    _identifier(attestation.get("key_id"), "attestation key_id")
    review = value["review"]
    review_fields = {
        "model_id", "model_version", "host_policy_version",
        "quality_profile_version", "prompt_version", "software_version",
        "timeout_seconds", "max_output_tokens", "max_concurrent_reviews",
        "native_brief",
    }
    if not isinstance(review, dict) or set(review) != review_fields:
        raise ResponseReviewRuntimeError("review configuration is invalid")
    for field in (
        "model_id", "model_version", "host_policy_version",
        "quality_profile_version", "prompt_version", "software_version",
    ):
        _identifier(review.get(field), field)
    if (type(review.get("timeout_seconds")) is not int
            or not 1 <= review["timeout_seconds"] <= MAX_REVIEW_SECONDS
            or type(review.get("max_output_tokens")) is not int
            or not 128 <= review["max_output_tokens"] <= 32768
            or type(review.get("max_concurrent_reviews")) is not int
            or not 1 <= review["max_concurrent_reviews"] <= 32):
        raise ResponseReviewRuntimeError("review budgets are invalid")
    review["native_brief"] = _native_brief(review["native_brief"])
    return value


class PinnedHMACAttestationVerifier:
    """Verify one pinned host key without exposing a signing API."""

    def __init__(self, secret: str, key_id: str):
        if HMAC_SECRET.fullmatch(secret) is None:
            raise ResponseReviewRuntimeError("attestation secret is invalid")
        self.__secret = secret.encode("ascii")
        self.key_id = _identifier(key_id, "attestation key_id")

    def verify(self, payload: bytes, attestation: dict[str, str]) -> bool:
        if (not isinstance(payload, bytes) or not isinstance(attestation, dict)
                or set(attestation) != {"schema", "algorithm", "key_id", "signature"}
                or attestation.get("schema") != HTTP.ATTESTATION_SCHEMA
                or attestation.get("algorithm") != "hmac-sha256"
                or attestation.get("key_id") != self.key_id
                or not isinstance(attestation.get("signature"), str)
                or HMAC_SIGNATURE.fullmatch(attestation["signature"]) is None):
            return False
        expected = hmac.new(self.__secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(attestation["signature"], expected)

    def __repr__(self) -> str:
        return f"PinnedHMACAttestationVerifier(key_id={self.key_id!r}, secret=<redacted>)"


class _LimitedReviewHost:
    def __init__(self, host: Any, limit: int):
        self._host = host
        self._capacity = threading.BoundedSemaphore(limit)

    def run_isolated(self, task: dict, *, control: dict):
        if not self._capacity.acquire(blocking=False):
            raise _CapacityUnavailable("response review host capacity is exhausted")
        try:
            return self._host.run_isolated(task, control=control)
        finally:
            self._capacity.release()

    def verify_execution(self, receipt: dict, *, control: dict) -> bool:
        return self._host.verify_execution(receipt, control=control)

    def verified_execution_evidence(self, receipt: dict, *, control: dict) -> dict:
        return self._host.verified_execution_evidence(receipt, control=control)


def build_response_reviewer_from_config(
    path: Path, *, transport: Any | None = None, response_module: Any | None = None,
):
    """Build the exact configured reviewer; tests may inject transport only."""
    config = load_runtime_config(path)
    authentication = config["authentication"]
    attestation = config["attestation"]
    review = config["review"]
    bearer = _protected_text(authentication["token_file"], BEARER, "authentication token")
    secret = _protected_text(attestation["secret_file"], HMAC_SECRET, "attestation secret")
    verifier = PinnedHMACAttestationVerifier(secret, attestation["key_id"])
    host = HTTP.HTTPSReviewHost(
        config["endpoint"],
        lambda: {"Authorization": "Bearer " + bearer},
        verifier,
        host_id=config["host_id"],
        transport=transport if transport is not None else DeadlineURLTransport(),
        timeout=review["timeout_seconds"],
        allow_loopback_http=config["allow_loopback_http"],
    )
    limited_host = _LimitedReviewHost(host, review["max_concurrent_reviews"])
    response = response_module or _load("blun_response_subagent_review", RESPONSE_PATH)
    return response.ResponseSubagentReviewer(
        limited_host,
        model_id=review["model_id"],
        model_version=review["model_version"],
        host_policy_version=review["host_policy_version"],
        quality_profile_version=review["quality_profile_version"],
        prompt_version=review["prompt_version"],
        software_version=review["software_version"],
        native_brief=review["native_brief"],
        timeout_seconds=review["timeout_seconds"],
        max_output_tokens=review["max_output_tokens"],
    )


def _transport_worker() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_TRANSPORT_MESSAGE_BYTES + 1)
        if not raw or len(raw) > MAX_TRANSPORT_MESSAGE_BYTES:
            return 2
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=_constant)
        if (not isinstance(request, dict)
                or set(request) != {"url", "headers", "body", "timeout"}
                or not isinstance(request["url"], str)
                or not isinstance(request["headers"], dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in request["headers"].items())
                or not isinstance(request["body"], str)
                or isinstance(request["timeout"], bool)
                or not isinstance(request["timeout"], (int, float))
                or not 0 < request["timeout"] <= MAX_REVIEW_SECONDS):
            return 2
        body = base64.b64decode(request["body"], validate=True)
        if len(body) > HTTP.MAX_REQUEST_BYTES:
            return 2
        try:
            result = HTTP.URLTransport().post(
                request["url"], request["headers"], body,
                timeout=float(request["timeout"]),
            )
            reply = {"result": {
                "status": result.status,
                "headers": [list(pair) for pair in result.headers],
                "body": base64.b64encode(result.body).decode("ascii"),
            }}
        except HTTP.HTTPReviewHostFailed as error:
            reply = {"error": {"code": error.code, "retryable": error.retryable}}
        encoded = json.dumps(reply, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_TRANSPORT_MESSAGE_BYTES:
            return 2
        sys.stdout.buffer.write(encoded)
        return 0
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
        return 2


if __name__ == "__main__":
    if sys.argv[1:] != ["--transport-worker"]:
        raise SystemExit(2)
    raise SystemExit(_transport_worker())
