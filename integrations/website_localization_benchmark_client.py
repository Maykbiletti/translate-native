#!/usr/bin/env python3
"""Provider-neutral, fail-closed client for stored benchmark reports.

The client pins one authenticated origin and one campaign/policy/suite tuple.
It reads status before evidence, preserves a valid BLOCK report for diagnosis,
and never treats transport success as permission to make a superiority claim.
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


MAX_ORIGIN_LENGTH = 2048
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
RESERVED_REQUEST_HEADERS = {
    "accept", "connection", "content-length", "host", "transfer-encoding",
}


def _load_http():
    path = Path(__file__).resolve().with_name(
        "website_localization_benchmark_http.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_benchmark_client_contract", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark HTTP contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_HTTP = _load_http()
MAX_RESPONSE_BYTES = _HTTP.MAX_RESPONSE_BYTES


class BenchmarkClientFailed(RuntimeError):
    """Stable, content-free client failure with a retry decision."""

    benchmark_client_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if (
            not isinstance(code, str)
            or _HTTP.ERROR_CODE.fullmatch(code) is None
            or not isinstance(retryable, bool)
        ):
            raise ValueError("benchmark client failure is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _fail(code: str, *, retryable: bool = False) -> None:
    raise BenchmarkClientFailed(code, retryable=retryable)


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
            _fail("benchmark_client.network", retryable=True)
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), raw,
            )
        except (TimeoutError, socket.timeout, OSError):
            _fail("benchmark_client.network", retryable=True)
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
        raise ValueError("benchmark origin is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError("benchmark origin is invalid") from None
    loopback = hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        not hostname or not hostname.isascii()
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and not (allow_loopback_http and loopback))
    ):
        raise ValueError("benchmark origin is invalid")
    return value.rstrip("/")


def _request_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > MAX_HEADERS - 2:
        _fail("benchmark_client.authentication_invalid")
    result: dict[str, str] = {}
    observed: set[str] = set()
    for name, item in value.items():
        if (
            not isinstance(name, str) or HEADER_NAME.fullmatch(name) is None
            or not isinstance(item, str) or not item
            or len(item) > MAX_HEADER_VALUE or "\r" in item or "\n" in item
        ):
            _fail("benchmark_client.authentication_invalid")
        lower = name.lower()
        if lower in observed or lower in RESERVED_REQUEST_HEADERS:
            _fail("benchmark_client.authentication_invalid")
        observed.add(lower)
        result[name] = item
    result["Accept"] = "application/json"
    result["Connection"] = "close"
    return result


def _response_headers(value: Any, body: bytes) -> None:
    if not isinstance(value, tuple) or len(value) > MAX_HEADERS:
        _fail("benchmark_client.response_invalid", retryable=True)
    result: dict[str, str] = {}
    for pair in value:
        if (
            not isinstance(pair, tuple) or len(pair) != 2
            or not isinstance(pair[0], str)
            or HEADER_NAME.fullmatch(pair[0]) is None
            or not isinstance(pair[1], str) or len(pair[1]) > MAX_HEADER_VALUE
            or "\r" in pair[1] or "\n" in pair[1]
        ):
            _fail("benchmark_client.response_invalid", retryable=True)
        name = pair[0].lower()
        if name in result:
            _fail("benchmark_client.response_invalid", retryable=True)
        result[name] = pair[1]
    expected = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }
    if any(result.get(name) != item for name, item in expected.items()):
        _fail("benchmark_client.response_invalid", retryable=True)
    length = result.get("content-length")
    if length is None or not length.isascii() or not length.isdecimal():
        _fail("benchmark_client.response_invalid", retryable=True)
    if int(length) != len(body):
        _fail("benchmark_client.response_invalid", retryable=True)


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
        _fail("benchmark_client.response_invalid", retryable=True)
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _fail("benchmark_client.response_invalid", retryable=True)
    if not isinstance(value, dict):
        _fail("benchmark_client.response_invalid", retryable=True)
    return value


@dataclass(frozen=True)
class BenchmarkStatusSnapshot:
    campaign: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return deepcopy(self.campaign)


@dataclass(frozen=True)
class BenchmarkReportSnapshot:
    campaign: dict[str, Any]
    report_sha256: str
    report: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return deepcopy(self.report)


class WebsiteLocalizationBenchmarkClient:
    """Read one exact campaign's stored report through a strict HTTP boundary."""

    def __init__(
        self, origin: str, credential_headers: Callable[[], Mapping[str, str]],
        *, expected_campaign_id: str, expected_policy_sha256: str,
        expected_suite_sha256: str, transport: HTTPTransport | None = None,
        clock: Callable[[], float], timeout: float = 10.0,
        allow_loopback_http: bool = False,
    ):
        self.origin = _origin(origin, allow_loopback_http)
        if not callable(credential_headers):
            raise ValueError("benchmark credential provider is invalid")
        if transport is not None and not callable(getattr(transport, "request", None)):
            raise ValueError("benchmark transport is invalid")
        if not callable(clock):
            raise ValueError("benchmark clock is invalid")
        if (
            not isinstance(expected_campaign_id, str)
            or _HTTP.CAMPAIGN_ID.fullmatch(expected_campaign_id) is None
            or not isinstance(expected_policy_sha256, str)
            or _HTTP.SHA256.fullmatch(expected_policy_sha256) is None
            or not isinstance(expected_suite_sha256, str)
            or _HTTP.SHA256.fullmatch(expected_suite_sha256) is None
        ):
            raise ValueError("benchmark binding is invalid")
        self.credential_headers = credential_headers
        self.transport = transport or URLTransport()
        self.clock = clock
        self.timeout = _finite(timeout, minimum=0.1, maximum=120, name="timeout")
        self.expected_campaign_id = expected_campaign_id
        self.expected_policy_sha256 = expected_policy_sha256
        self.expected_suite_sha256 = expected_suite_sha256

    def _request(self, path: str) -> tuple[int, dict[str, Any]]:
        try:
            headers = _request_headers(self.credential_headers())
        except BenchmarkClientFailed:
            raise
        except Exception:
            _fail("benchmark_client.authentication_unavailable", retryable=True)
        try:
            response = self.transport.request(
                "GET", self.origin + path, headers, None, timeout=self.timeout,
            )
        except BenchmarkClientFailed:
            raise
        except Exception:
            _fail("benchmark_client.network", retryable=True)
        if (
            not isinstance(response, HTTPResult) or isinstance(response.status, bool)
            or not isinstance(response.status, int) or not isinstance(response.body, bytes)
        ):
            _fail("benchmark_client.response_invalid", retryable=True)
        _response_headers(response.headers, response.body)
        value = _payload(response.body)
        if value.get("schema") == _HTTP.ERROR_RESPONSE_SCHEMA:
            if set(value) != {"schema", "error_code", "retryable"}:
                _fail("benchmark_client.response_invalid", retryable=True)
            code, retryable = value.get("error_code"), value.get("retryable")
            if (
                not isinstance(code, str) or _HTTP.ERROR_CODE.fullmatch(code) is None
                or not isinstance(retryable, bool)
                or response.status not in {400, 401, 403, 404, 409, 503}
                or (response.status != 503 and retryable)
            ):
                _fail("benchmark_client.response_invalid", retryable=True)
            _fail(code, retryable=retryable)
        if response.status != 200:
            _fail("benchmark_client.response_invalid", retryable=True)
        return response.status, value

    def _now(self) -> float:
        try:
            return _HTTP._timestamp(self.clock())
        except Exception:
            _fail("benchmark_client.clock_invalid")

    def status(self) -> BenchmarkStatusSnapshot:
        _, value = self._request(_HTTP.STATUS_PATH)
        if (
            value.get("schema") != _HTTP.STATUS_RESPONSE_SCHEMA
            or set(value) != {"schema", "campaign"}
        ):
            _fail("benchmark_client.response_invalid", retryable=True)
        try:
            campaign = _HTTP._status_payload(value["campaign"])
        except Exception:
            _fail("benchmark_client.response_invalid", retryable=True)
        if campaign["campaign_id"] != self.expected_campaign_id:
            _fail("benchmark_client.campaign_mismatch")
        if campaign["policy_sha256"] != self.expected_policy_sha256:
            _fail("benchmark_client.policy_mismatch")
        if campaign["suite_sha256"] != self.expected_suite_sha256:
            _fail("benchmark_client.suite_mismatch")
        if self._now() >= campaign["valid_until"]:
            _fail("benchmark_client.campaign_expired")
        return BenchmarkStatusSnapshot(deepcopy(campaign))

    def report(self) -> BenchmarkReportSnapshot:
        status = self.status().as_payload()
        if status["blocked"]:
            _fail("benchmark_client.campaign_blocked")
        if not status["complete"]:
            _fail("benchmark_client.campaign_incomplete", retryable=True)
        finalization = status["report_finalization"]["status"]
        if finalization != "succeeded":
            _fail(
                "benchmark_client.report_unavailable",
                retryable=finalization in {"pending", "leased", "retry_wait"},
            )
        _, value = self._request(_HTTP.REPORT_PATH)
        if (
            value.get("schema") != _HTTP.REPORT_RESPONSE_SCHEMA
            or set(value) != {
                "schema", "campaign_id", "report_sha256", "report",
            }
            or value.get("campaign_id") != self.expected_campaign_id
            or not isinstance(value.get("report_sha256"), str)
            or _HTTP.SHA256.fullmatch(value["report_sha256"]) is None
        ):
            _fail("benchmark_client.response_invalid", retryable=True)
        try:
            report, report_sha256 = _HTTP._report_payload(value["report"], status)
        except Exception:
            _fail("benchmark_client.response_invalid", retryable=True)
        if not hashlib.sha256(_HTTP._canonical_json(report)).hexdigest() == report_sha256:
            _fail("benchmark_client.response_invalid", retryable=True)
        if report_sha256 != value["report_sha256"]:
            _fail("benchmark_client.report_digest_mismatch")
        if self._now() >= status["valid_until"]:
            _fail("benchmark_client.campaign_expired")
        return BenchmarkReportSnapshot(
            deepcopy(status), report_sha256, deepcopy(report),
        )
