#!/usr/bin/env python3
"""Provider-neutral, fail-closed client for operator health reports.

The client performs one bounded request, preserves a valid blocked report for
diagnosis, and converts every transport or contract failure into a stable,
content-free error. Retry scheduling remains a host responsibility.
"""

from __future__ import annotations

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


MAX_ORIGIN_LENGTH = 2048
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
MAX_RESPONSE_BYTES = 4_000_000
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
RESERVED_REQUEST_HEADERS = {
    "accept", "connection", "content-length", "host", "transfer-encoding",
}


def _load_health_http():
    path = Path(__file__).resolve().with_name(
        "website_localization_health_http.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_health_client_contract", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("health HTTP contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_HTTP = _load_health_http()


class HealthClientFailed(RuntimeError):
    """Stable content-free failure with an explicit retry decision."""

    health_client_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if (
            not isinstance(code, str)
            or _HTTP.ERROR_CODE.fullmatch(code) is None
            or not isinstance(retryable, bool)
        ):
            raise ValueError("health client failure is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _fail(code: str, *, retryable: bool = False) -> None:
    raise HealthClientFailed(code, retryable=retryable)


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: float,
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
            _fail("health_client.network", retryable=True)
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), raw,
            )
        except (TimeoutError, socket.timeout, OSError):
            _fail("health_client.network", retryable=True)
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
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or not value.isascii()
        or len(value) > MAX_ORIGIN_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("health origin is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError("health origin is invalid") from None
    loopback = hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and not (allow_loopback_http and loopback))
    ):
        raise ValueError("health origin is invalid")
    return value.rstrip("/")


def _request_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > MAX_HEADERS - 2:
        _fail("health_client.authentication_invalid")
    result: dict[str, str] = {}
    observed: set[str] = set()
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or HEADER_NAME.fullmatch(name) is None
            or not isinstance(item, str)
            or not item
            or len(item) > MAX_HEADER_VALUE
            or "\r" in item
            or "\n" in item
        ):
            _fail("health_client.authentication_invalid")
        lower = name.lower()
        if lower in observed or lower in RESERVED_REQUEST_HEADERS:
            _fail("health_client.authentication_invalid")
        observed.add(lower)
        result[name] = item
    result["Accept"] = "application/json"
    result["Connection"] = "close"
    return result


def _response_headers(value: Any, body: bytes) -> dict[str, str]:
    if not isinstance(value, tuple) or len(value) > MAX_HEADERS:
        _fail("health_client.response_invalid", retryable=True)
    result: dict[str, str] = {}
    for pair in value:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or HEADER_NAME.fullmatch(pair[0]) is None
            or not isinstance(pair[1], str)
            or len(pair[1]) > MAX_HEADER_VALUE
            or "\r" in pair[1]
            or "\n" in pair[1]
        ):
            _fail("health_client.response_invalid", retryable=True)
        name = pair[0].lower()
        if name in result:
            _fail("health_client.response_invalid", retryable=True)
        result[name] = pair[1]
    expected = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }
    if any(result.get(name) != expected_value for name, expected_value in expected.items()):
        _fail("health_client.response_invalid", retryable=True)
    length = result.get("content-length")
    if length is None or not length.isascii() or not length.isdecimal():
        _fail("health_client.response_invalid", retryable=True)
    if int(length) != len(body):
        _fail("health_client.response_invalid", retryable=True)
    return result


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _payload(body: Any) -> dict[str, Any]:
    if not isinstance(body, bytes) or not body or len(body) > MAX_RESPONSE_BYTES:
        _fail("health_client.response_invalid", retryable=True)
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _fail("health_client.response_invalid", retryable=True)
    if not isinstance(value, dict):
        _fail("health_client.response_invalid", retryable=True)
    return value


class _Report:
    def __init__(self, value: dict[str, Any]):
        self.value = value

    def as_payload(self) -> dict[str, Any]:
        return self.value


@dataclass(frozen=True)
class HealthSnapshot:
    http_status: int
    report: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return deepcopy(self.report)


class WebsiteLocalizationHealthClient:
    """Read and strictly validate one remote operator health assessment."""

    def __init__(
        self,
        origin: str,
        credential_headers: Callable[[], Mapping[str, str]],
        *,
        transport: HTTPTransport | None = None,
        clock: Callable[[], float],
        timeout: float = 10.0,
        max_report_age_seconds: float = 60.0,
        max_future_skew_seconds: float = 5.0,
        allow_loopback_http: bool = False,
    ):
        self.origin = _origin(origin, allow_loopback_http)
        if not callable(credential_headers):
            raise ValueError("health credential provider is invalid")
        if transport is not None and not callable(getattr(transport, "request", None)):
            raise ValueError("health transport is invalid")
        if not callable(clock):
            raise ValueError("health clock is invalid")
        self.credential_headers = credential_headers
        self.transport = transport or URLTransport()
        self.clock = clock
        self.timeout = _finite(timeout, minimum=0.1, maximum=120, name="timeout")
        self.max_report_age_seconds = _finite(
            max_report_age_seconds, minimum=0, maximum=3600,
            name="max report age",
        )
        self.max_future_skew_seconds = _finite(
            max_future_skew_seconds, minimum=0, maximum=60,
            name="maximum future skew",
        )

    def read(self) -> HealthSnapshot:
        try:
            headers = _request_headers(self.credential_headers())
        except HealthClientFailed:
            raise
        except Exception:
            _fail("health_client.authentication_unavailable", retryable=True)
        try:
            response = self.transport.request(
                "GET", self.origin + _HTTP.HEALTH_PATH, headers, None,
                timeout=self.timeout,
            )
        except HealthClientFailed:
            raise
        except Exception:
            _fail("health_client.network", retryable=True)
        if (
            not isinstance(response, HTTPResult)
            or isinstance(response.status, bool)
            or not isinstance(response.status, int)
        ):
            _fail("health_client.response_invalid", retryable=True)
        body = response.body
        if not isinstance(body, bytes):
            _fail("health_client.response_invalid", retryable=True)
        _response_headers(response.headers, body)
        value = _payload(body)
        if value.get("schema") == _HTTP.ERROR_SCHEMA:
            if set(value) != {"schema", "error_code", "retryable"}:
                _fail("health_client.response_invalid", retryable=True)
            code = value.get("error_code")
            retryable = value.get("retryable")
            if (
                not isinstance(code, str)
                or _HTTP.ERROR_CODE.fullmatch(code) is None
                or not isinstance(retryable, bool)
                or response.status not in {400, 401, 403, 404, 503}
                or (response.status != 503 and retryable)
            ):
                _fail("health_client.response_invalid", retryable=True)
            _fail(code, retryable=retryable)
        if (
            value.get("schema") != _HTTP.RESPONSE_SCHEMA
            or set(value) != {"schema", "report"}
        ):
            _fail("health_client.response_invalid", retryable=True)
        report_value = value["report"]
        try:
            checked_at = _HTTP._timestamp(report_value.get("checked_at"))
            report = _HTTP._report_payload(
                _Report(report_value), expected_checked_at=checked_at,
            )
            now = _HTTP._timestamp(self.clock())
        except Exception:
            _fail("health_client.response_invalid", retryable=True)
        if (
            checked_at < now - self.max_report_age_seconds
            or checked_at > now + self.max_future_skew_seconds
        ):
            _fail("health_client.report_stale", retryable=True)
        expected_status = 503 if report["status"] == "blocked" else 200
        if response.status != expected_status:
            _fail("health_client.response_invalid", retryable=True)
        return HealthSnapshot(response.status, deepcopy(report))
