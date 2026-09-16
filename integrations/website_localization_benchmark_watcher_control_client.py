#!/usr/bin/env python3
"""Provider-neutral, fail-closed client for benchmark watcher recovery.

The client sends one exact, idempotent failed-generation rearm request. It
never invents a new request identity after an uncertain transport outcome and
accepts only a receipt bound to the complete canonical request.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


AUTH_CONTEXT_SCHEMA = (
    "blun.website-localization-benchmark-watcher-control-client-auth.v1"
)
MAX_ORIGIN_LENGTH = 2048
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
RESERVED_REQUEST_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host",
    "idempotency-key", "transfer-encoding",
}
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
REMOTE_ERRORS = {
    "benchmark_watcher.control.environment_invalid": (400, False),
    "benchmark_watcher.control.https_required": (400, False),
    "benchmark_watcher.control.query_invalid": (400, False),
    "benchmark_watcher.control.route_not_found": (404, False),
    "benchmark_watcher.control.transfer_encoding": (400, False),
    "benchmark_watcher.control.content_type": (415, False),
    "benchmark_watcher.control.body_invalid": (400, False),
    "benchmark_watcher.control.body_too_large": (413, False),
    "benchmark_watcher.control.headers_invalid": (400, False),
    "benchmark_watcher.control.authentication_failed": (401, False),
    "benchmark_watcher.control.authentication_unavailable": (503, True),
    "benchmark_watcher.control.request_invalid": (400, False),
    "benchmark_watcher.control.idempotency_key_invalid": (400, False),
    "benchmark_watcher.control.schema_invalid": (503, False),
    "benchmark_watcher.control.evidence_invalid": (503, False),
    "benchmark_watcher.control.idempotency_conflict": (409, False),
    "benchmark_watcher.control.state_invalid": (503, False),
    "benchmark_watcher.control.lease_active": (409, False),
    "benchmark_watcher.control.result_final": (409, False),
    "benchmark_watcher.control.rearm_not_required": (409, False),
    "benchmark_watcher.control.state_conflict": (409, False),
    "benchmark_watcher.control.clock_invalid": (503, True),
    "benchmark_watcher.control.internal": (503, True),
}


def _load_control():
    path = Path(__file__).resolve().with_name(
        "website_localization_benchmark_watcher_control.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_benchmark_watcher_control_client_contract",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark watcher control contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CONTROL = _load_control()
MAX_RESPONSE_BYTES = _CONTROL.MAX_BODY_BYTES


class BenchmarkWatcherControlClientFailed(RuntimeError):
    """Stable, content-free failure with an explicit retry decision."""

    benchmark_watcher_control_client_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if (
            not isinstance(code, str)
            or _CONTROL.ERROR_CODE.fullmatch(code) is None
            or not isinstance(retryable, bool)
        ):
            raise ValueError("benchmark watcher control client failure is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _fail(code: str, *, retryable: bool = False) -> None:
    raise BenchmarkWatcherControlClientFailed(code, retryable=retryable)


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self, method: str, url: str, headers: Mapping[str, str],
        body: bytes | None, *, timeout: float,
    ) -> HTTPResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class URLTransport:
    """Perform one bounded request without following redirects."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, url, headers, body, *, timeout):
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            _fail("benchmark_watcher.control_client.network", retryable=True)
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), raw,
            )
        except (TimeoutError, socket.timeout, OSError):
            _fail("benchmark_watcher.control_client.network", retryable=True)
        finally:
            response.close()


def _finite(value: Any, *, minimum: float, maximum: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is invalid")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} is invalid")
    return result


def _origin(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str) or value != value.strip() or not value
        or not value.isascii() or len(value) > MAX_ORIGIN_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("benchmark watcher control origin is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError("benchmark watcher control origin is invalid") from None
    loopback = hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        not hostname or not hostname.isascii()
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and not (allow_loopback_http and loopback))
    ):
        raise ValueError("benchmark watcher control origin is invalid")
    return value.rstrip("/")


def _canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _fail("benchmark_watcher.control_client.request_invalid")
    if not raw or len(raw) > _CONTROL.MAX_BODY_BYTES:
        _fail("benchmark_watcher.control_client.request_invalid")
    return raw


def _request(
    request_id: Any, expected_attempts: Any, expected_failed_at: Any,
    expected_error_code: Any,
) -> dict[str, Any]:
    try:
        if (
            not isinstance(request_id, str)
            or _CONTROL.IDENTIFIER.fullmatch(request_id) is None
            or isinstance(expected_attempts, bool)
            or not isinstance(expected_attempts, int)
            or not 1 <= expected_attempts <= _CONTROL._WATCHER.MAX_ATTEMPTS
            or not isinstance(expected_error_code, str)
            or _CONTROL.ERROR_CODE.fullmatch(expected_error_code) is None
        ):
            raise ValueError
        failed_at = _CONTROL._timestamp(expected_failed_at)
    except (TypeError, ValueError):
        _fail("benchmark_watcher.control_client.request_invalid")
    return {
        "schema": _CONTROL.REQUEST_SCHEMA,
        "request_id": request_id,
        "expected_attempts": expected_attempts,
        "expected_failed_at": failed_at,
        "expected_error_code": expected_error_code,
    }


def _credential_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > MAX_HEADERS - 5:
        _fail("benchmark_watcher.control_client.authentication_invalid")
    result: dict[str, str] = {}
    observed: set[str] = set()
    for name, item in value.items():
        if (
            not isinstance(name, str) or HEADER_NAME.fullmatch(name) is None
            or not isinstance(item, str) or not item
            or len(item) > MAX_HEADER_VALUE or "\r" in item or "\n" in item
        ):
            _fail("benchmark_watcher.control_client.authentication_invalid")
        lower = name.lower()
        if lower in observed or lower in RESERVED_REQUEST_HEADERS:
            _fail("benchmark_watcher.control_client.authentication_invalid")
        observed.add(lower)
        result[name] = item
    return result


def _response_headers(value: Any, body: bytes) -> None:
    if not isinstance(value, tuple) or len(value) > MAX_HEADERS:
        _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
    result: dict[str, str] = {}
    for pair in value:
        if (
            not isinstance(pair, tuple) or len(pair) != 2
            or not isinstance(pair[0], str)
            or HEADER_NAME.fullmatch(pair[0]) is None
            or not isinstance(pair[1], str) or len(pair[1]) > MAX_HEADER_VALUE
            or "\r" in pair[1] or "\n" in pair[1]
        ):
            _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
        name = pair[0].lower()
        if name in result:
            _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
        result[name] = pair[1]
    expected = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }
    if (
        any(result.get(name) != item for name, item in expected.items())
        or "transfer-encoding" in result or "content-encoding" in result
    ):
        _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
    length = result.get("content-length")
    if (
        length is None or not length.isascii() or not length.isdecimal()
        or int(length) != len(body)
    ):
        _fail("benchmark_watcher.control_client.response_invalid", retryable=True)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _payload(body: Any) -> dict[str, Any]:
    if not isinstance(body, bytes) or not body or len(body) > MAX_RESPONSE_BYTES:
        _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
    if not isinstance(value, dict):
        _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
    return value


@dataclass(frozen=True)
class BenchmarkWatcherRearmSnapshot:
    request_sha256: str
    receipt: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return deepcopy(self.receipt)


@dataclass(frozen=True)
class BenchmarkWatcherRecoveryStatus:
    checked_at: float
    state: str
    rearmable: bool
    generation: dict[str, Any] | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": _CONTROL.STATUS_SCHEMA,
            "checked_at": self.checked_at,
            "state": self.state,
            "rearmable": self.rearmable,
            "generation": deepcopy(self.generation),
        }


class WebsiteLocalizationBenchmarkWatcherControlClient:
    """Inspect and rearm one exact watcher generation through strict HTTPS."""

    def __init__(
        self, origin: str,
        credential_headers: Callable[[dict[str, Any]], Mapping[str, str]],
        *, transport: HTTPTransport | None = None,
        clock: Callable[[], float], timeout: float = 10.0,
        max_future_skew: float = 300.0, allow_loopback_http: bool = False,
    ):
        self.origin = _origin(origin, allow_loopback_http)
        if not callable(credential_headers):
            raise ValueError("benchmark watcher control credentials are invalid")
        if transport is not None and not callable(getattr(transport, "request", None)):
            raise ValueError("benchmark watcher control transport is invalid")
        if not callable(clock):
            raise ValueError("benchmark watcher control clock is invalid")
        self.credential_headers = credential_headers
        self.transport = transport or URLTransport()
        self.clock = clock
        self.timeout = _finite(timeout, minimum=0.1, maximum=120, name="timeout")
        self.max_future_skew = _finite(
            max_future_skew, minimum=0, maximum=3600, name="max_future_skew",
        )

    def _now(self) -> float:
        try:
            return _CONTROL._timestamp(self.clock())
        except Exception:
            _fail("benchmark_watcher.control_client.clock_invalid")

    def _authorization_headers(
        self, *, method: str, path: str, body: bytes,
        idempotency_key: str | None,
    ) -> dict[str, str]:
        auth_context = {
            "schema": AUTH_CONTEXT_SCHEMA,
            "method": method,
            "path": path,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "idempotency_key": idempotency_key,
        }
        try:
            headers = _credential_headers(
                self.credential_headers(deepcopy(auth_context))
            )
        except BenchmarkWatcherControlClientFailed:
            raise
        except Exception:
            _fail(
                "benchmark_watcher.control_client.authentication_unavailable",
                retryable=True,
            )
        headers.update({
            "Accept": "application/json",
            "Connection": "close",
        })
        return headers

    def _response(
        self, *, method: str, path: str, headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            result = self.transport.request(
                method, self.origin + path, headers, body,
                timeout=self.timeout,
            )
        except BenchmarkWatcherControlClientFailed:
            raise
        except Exception:
            _fail("benchmark_watcher.control_client.network", retryable=True)
        if (
            not isinstance(result, HTTPResult) or isinstance(result.status, bool)
            or not isinstance(result.status, int)
            or not isinstance(result.body, bytes)
        ):
            _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
        _response_headers(result.headers, result.body)
        value = _payload(result.body)
        if value.get("schema") == _CONTROL.ERROR_SCHEMA:
            if set(value) != {"schema", "error_code", "retryable"}:
                _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
            code, retryable = value.get("error_code"), value.get("retryable")
            if (
                not isinstance(code, str)
                or _CONTROL.ERROR_CODE.fullmatch(code) is None
                or not isinstance(retryable, bool)
            ):
                _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
            if REMOTE_ERRORS.get(code) != (result.status, retryable):
                _fail("benchmark_watcher.control_client.contract_mismatch")
            _fail(code, retryable=retryable)
        return result.status, value

    def status(self) -> BenchmarkWatcherRecoveryStatus:
        """Read the exact content-free generation that may be rearmed."""
        headers = self._authorization_headers(
            method="GET", path=_CONTROL.STATUS_PATH, body=b"",
            idempotency_key=None,
        )
        result_status, value = self._response(
            method="GET", path=_CONTROL.STATUS_PATH, headers=headers, body=None,
        )
        if (
            result_status != 200
            or set(value) != {"schema", "status"}
            or value.get("schema") != _CONTROL.STATUS_RESPONSE_SCHEMA
        ):
            _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
        status = value.get("status")
        if not isinstance(status, dict) or set(status) != {
            "schema", "checked_at", "state", "rearmable", "generation",
        }:
            _fail("benchmark_watcher.control_client.status_mismatch")
        try:
            checked_at = _CONTROL._timestamp(status["checked_at"])
        except (KeyError, TypeError, ValueError):
            _fail("benchmark_watcher.control_client.status_mismatch")
        states = {"pending", "leased", "retry_wait", "succeeded", "failed"}
        if (
            status.get("schema") != _CONTROL.STATUS_SCHEMA
            or status.get("state") not in states
            or type(status.get("rearmable")) is not bool
            or status["rearmable"] != (status["state"] == "failed")
            or checked_at > self._now() + self.max_future_skew
        ):
            _fail("benchmark_watcher.control_client.status_mismatch")
        generation = status.get("generation")
        if status["rearmable"]:
            if not isinstance(generation, dict) or set(generation) != {
                "attempts", "failed_at", "error_code",
            }:
                _fail("benchmark_watcher.control_client.status_mismatch")
            try:
                failed_at = _CONTROL._timestamp(generation["failed_at"])
            except (KeyError, TypeError, ValueError):
                _fail("benchmark_watcher.control_client.status_mismatch")
            if (
                isinstance(generation.get("attempts"), bool)
                or not isinstance(generation.get("attempts"), int)
                or not 1 <= generation["attempts"] <= _CONTROL._WATCHER.MAX_ATTEMPTS
                or not isinstance(generation.get("error_code"), str)
                or _CONTROL.ERROR_CODE.fullmatch(generation["error_code"]) is None
                or failed_at > checked_at
            ):
                _fail("benchmark_watcher.control_client.status_mismatch")
            generation = {
                "attempts": generation["attempts"],
                "failed_at": failed_at,
                "error_code": generation["error_code"],
            }
        elif generation is not None:
            _fail("benchmark_watcher.control_client.status_mismatch")
        return BenchmarkWatcherRecoveryStatus(
            checked_at, status["state"], status["rearmable"],
            deepcopy(generation),
        )

    def rearm_failed(self, *, request_id: str) -> BenchmarkWatcherRearmSnapshot:
        """Read and rearm the current failed generation without inventing an ID."""
        if (
            not isinstance(request_id, str)
            or _CONTROL.IDENTIFIER.fullmatch(request_id) is None
        ):
            _fail("benchmark_watcher.control_client.request_invalid")
        status = self.status()
        if not status.rearmable or status.generation is None:
            _fail("benchmark_watcher.control_client.not_rearmable")
        return self.rearm(
            request_id=request_id,
            expected_attempts=status.generation["attempts"],
            expected_failed_at=status.generation["failed_at"],
            expected_error_code=status.generation["error_code"],
        )

    def rearm(
        self, *, request_id: str, expected_attempts: int,
        expected_failed_at: float | int, expected_error_code: str,
    ) -> BenchmarkWatcherRearmSnapshot:
        request = _request(
            request_id, expected_attempts, expected_failed_at,
            expected_error_code,
        )
        body = _canonical(request)
        request_sha256 = hashlib.sha256(body).hexdigest()
        headers = self._authorization_headers(
            method="POST", path=_CONTROL.REARM_PATH, body=body,
            idempotency_key=request_id,
        )
        headers.update({
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Idempotency-Key": request_id,
        })
        result_status, value = self._response(
            method="POST", path=_CONTROL.REARM_PATH, headers=headers, body=body,
        )
        if result_status != 200 or set(value) != {"schema", "receipt"} or value.get(
            "schema"
        ) != _CONTROL.RESPONSE_SCHEMA:
            _fail("benchmark_watcher.control_client.response_invalid", retryable=True)
        receipt = value.get("receipt")
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema", "request_sha256", "previous_state", "previous_attempts",
            "previous_error_code", "failed_at", "state", "rearmed_at",
        }:
            _fail("benchmark_watcher.control_client.receipt_mismatch")
        try:
            failed_at = _CONTROL._timestamp(receipt["failed_at"])
            rearmed_at = _CONTROL._timestamp(receipt["rearmed_at"])
        except (KeyError, TypeError, ValueError):
            _fail("benchmark_watcher.control_client.receipt_mismatch")
        if (
            receipt.get("schema") != _CONTROL.RECEIPT_SCHEMA
            or receipt.get("request_sha256") != request_sha256
            or receipt.get("previous_state") != "failed"
            or receipt.get("previous_attempts") != request["expected_attempts"]
            or isinstance(receipt.get("previous_attempts"), bool)
            or receipt.get("previous_error_code")
            != request["expected_error_code"]
            or failed_at != request["expected_failed_at"]
            or receipt.get("state") != "pending"
            or rearmed_at < failed_at
            or rearmed_at > self._now() + self.max_future_skew
        ):
            _fail("benchmark_watcher.control_client.receipt_mismatch")
        return BenchmarkWatcherRearmSnapshot(
            request_sha256, deepcopy(receipt),
        )
