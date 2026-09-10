#!/usr/bin/env python3
"""Provider-neutral HTTPS adapter for one anonymous benchmark review pass."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


REQUEST_SCHEMA = "blun.website-localization-benchmark-review-http-request.v1"
RESPONSE_SCHEMA = "blun.website-localization-benchmark-review-http-response.v1"
MAX_ENDPOINT_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 4096
MAX_REQUEST_BYTES = 8_500_000
MAX_RESPONSE_BYTES = 4_000_000
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
REVIEW_ID = re.compile(r"^benchmark-review-[0-9a-f]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
RESERVED_HEADERS = {
    "accept", "accept-encoding", "connection", "content-length", "content-type",
    "host", "idempotency-key", "transfer-encoding", "x-benchmark-review-id",
    "x-benchmark-review-request-sha256", "x-benchmark-review-phase",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark reviewer dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK = _load_module(
    "blun_website_localization_http_benchmark_reviewer",
    _ROOT / "integrations" / "website_localization_benchmark.py",
)


class HTTPBenchmarkReviewerFailed(RuntimeError):
    """Content-free adapter failure understood by benchmark orchestration."""

    benchmark_reviewer_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("HTTP benchmark reviewer error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("HTTP benchmark reviewer retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
    ) -> HTTPResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class URLTransport:
    """Perform one bounded HTTP request without following redirects."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
    ) -> HTTPResult:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST",
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response_headers = tuple(error.headers.items()) if error.headers else ()
            return HTTPResult(int(error.code), response_headers, b"")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise HTTPBenchmarkReviewerFailed("network", retryable=True) from None
        try:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), response_body,
            )
        except (TimeoutError, socket.timeout, OSError):
            raise HTTPBenchmarkReviewerFailed("network", retryable=True) from None
        finally:
            response.close()


def _canonical_json(value: Any, *, code: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise HTTPBenchmarkReviewerFailed(code, retryable=False) from None
    if not encoded or len(encoded) > maximum:
        raise HTTPBenchmarkReviewerFailed(code, retryable=False)
    return encoded


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or not value.isascii()
        or len(value) > MAX_ENDPOINT_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint is invalid") from None
    if (
        not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ValueError("endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and allow_loopback_http
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        pass
    else:
        raise ValueError("endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint is invalid")
    return value


def _authentication_headers(provider: Callable[[], Mapping[str, str]]) -> dict[str, str]:
    try:
        supplied = provider()
    except Exception:
        raise HTTPBenchmarkReviewerFailed("authentication", retryable=False) from None
    if not isinstance(supplied, Mapping) or not supplied:
        raise HTTPBenchmarkReviewerFailed("authentication", retryable=False)
    result: dict[str, str] = {}
    normalized_names: set[str] = set()
    for name, value in supplied.items():
        normalized = name.lower() if isinstance(name, str) else ""
        if (
            not isinstance(name, str)
            or HEADER_NAME.fullmatch(name) is None
            or normalized in RESERVED_HEADERS
            or normalized in normalized_names
            or not isinstance(value, str)
            or not value
            or not value.isascii()
            or len(value) > MAX_HEADER_VALUE_LENGTH
            or "\r" in value
            or "\n" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise HTTPBenchmarkReviewerFailed("authentication", retryable=False)
        normalized_names.add(normalized)
        result[name] = value
    return result


def _response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise HTTPBenchmarkReviewerFailed("transport_invalid", retryable=True)
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise HTTPBenchmarkReviewerFailed("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                raise HTTPBenchmarkReviewerFailed("response_headers", retryable=True)
            result[name] = content
    return result


def _blind_input(value: dict[str, Any], *, phase: str, locale: str) -> None:
    common = {
        "blind_id", "benchmark_version", "benchmark_suite", "target",
        "content_type", "audience", "tone_profile", "policy_version",
        "quality_profile", "variants", "response_schema",
    }
    phase_fields = (
        {"target_terms"}
        if phase == "target_native"
        else {"source", "glossary", "protected_terms"}
    )
    if set(value) != common | phase_fields:
        code = "source_blindness" if phase == "target_native" else "request_invalid"
        raise HTTPBenchmarkReviewerFailed(code, retryable=False)
    variants = value["variants"]
    response_schema = value["response_schema"]
    benchmark_suite = value["benchmark_suite"]
    commercial_fidelity = (
        phase == "source_fidelity" and value["content_type"] == "commercial"
    )
    commercial_dimensions = (
        benchmark_suite.get("commercial_dimensions")
        if isinstance(benchmark_suite, dict)
        and commercial_fidelity
        else None
    )
    if (
        not isinstance(benchmark_suite, dict)
        or not isinstance(value["target"], dict)
        or value["target"].get("locale") != locale
        or not isinstance(variants, list)
        or len(variants) != 2
        or [item.get("label") if isinstance(item, dict) else None for item in variants]
        != ["A", "B"]
        or any(
            set(item) != {"label", "text"}
            or not isinstance(item["text"], str)
            or not item["text"]
            for item in variants
        )
        or not isinstance(response_schema, dict)
        or response_schema.get("schema") != _BENCHMARK.REVIEW_SCHEMA
        or response_schema.get("phase") != phase
        or response_schema.get("target_locale") != locale
        or response_schema.get("blind_id") != value["blind_id"]
    ):
        raise HTTPBenchmarkReviewerFailed("request_invalid", retryable=False)
    if commercial_fidelity and commercial_dimensions != list(
        _BENCHMARK._WORKER._COMMERCIAL.DIMENSIONS
    ):
        raise HTTPBenchmarkReviewerFailed("request_invalid", retryable=False)
    expected_response_schema = _BENCHMARK._review_response_contract(
        phase=phase,
        locale=locale,
        blind_id=value["blind_id"],
        commercial_dimensions=(
            commercial_dimensions if commercial_fidelity else None
        ),
    )
    if response_schema != expected_response_schema:
        raise HTTPBenchmarkReviewerFailed("request_invalid", retryable=False)
    if not commercial_fidelity and "commercial_dimensions" in benchmark_suite:
        raise HTTPBenchmarkReviewerFailed("request_invalid", retryable=False)


def _request_payload(request: Any) -> tuple[dict[str, Any], bytes]:
    try:
        payload = request.as_payload()
        review_id = request.review_id
        phase = request.phase
        target_locale = request.target_locale
        system_instruction = request.system_instruction
        review_input = request.input
    except Exception:
        raise HTTPBenchmarkReviewerFailed("request_invalid", retryable=False) from None
    input_value = review_input if isinstance(review_input, dict) else {}
    commercial_fidelity = (
        phase == "source_fidelity"
        and input_value.get("content_type") == "commercial"
    )
    expected_system = {
        "target_native": _BENCHMARK._NATIVE_SYSTEM,
        "source_fidelity": _BENCHMARK._FIDELITY_SYSTEM + (
            "\n" + _BENCHMARK._COMMERCIAL_BENCHMARK_FIDELITY_SYSTEM
            if commercial_fidelity else ""
        ),
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "schema", "review_id", "phase", "target_locale",
            "system_instruction", "input",
        }
        or payload != {
            "schema": request.schema,
            "review_id": review_id,
            "phase": phase,
            "target_locale": target_locale,
            "system_instruction": system_instruction,
            "input": review_input,
        }
        or request.schema != _BENCHMARK.BENCHMARK_SCHEMA
        or not isinstance(review_id, str)
        or REVIEW_ID.fullmatch(review_id) is None
        or phase not in expected_system
        or system_instruction != expected_system[phase]
        or not isinstance(target_locale, str)
        or _BENCHMARK.IDENTIFIER.fullmatch(target_locale) is None
        or not isinstance(review_input, dict)
        or not isinstance(review_input.get("blind_id"), str)
        or not review_input["blind_id"].startswith("blind-")
    ):
        raise HTTPBenchmarkReviewerFailed("request_invalid", retryable=False)
    _blind_input(review_input, phase=phase, locale=target_locale)
    raw = _canonical_json(payload, code="request_invalid", maximum=MAX_REQUEST_BYTES)
    return payload, raw


class HTTPBenchmarkReviewerAdapter:
    """Send exactly one bound, anonymous review request to an attached service."""

    def __init__(
        self,
        endpoint: str,
        authentication_headers: Callable[[], Mapping[str, str]],
        *,
        transport: HTTPTransport | None = None,
        timeout: float = 60.0,
        allow_loopback_http: bool = False,
    ):
        if not isinstance(allow_loopback_http, bool):
            raise TypeError("allow_loopback_http must be boolean")
        self.endpoint = _endpoint(endpoint, allow_loopback_http)
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.timeout = float(timeout)

    def review(self, request: Any) -> Mapping[str, Any]:
        payload, request_bytes = _request_payload(request)
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        review_id = payload["review_id"]
        body = _canonical_json(
            {
                "schema": REQUEST_SCHEMA,
                "review_id": review_id,
                "request_sha256": request_sha256,
                "review": payload,
            },
            code="request_invalid",
            maximum=MAX_REQUEST_BYTES,
        )
        headers = _authentication_headers(self.authentication_headers)
        headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": review_id,
            "X-Benchmark-Review-Id": review_id,
            "X-Benchmark-Review-Phase": payload["phase"],
            "X-Benchmark-Review-Request-Sha256": request_sha256,
        })
        try:
            result = self.transport.post(
                self.endpoint, headers, body, timeout=self.timeout,
            )
        except HTTPBenchmarkReviewerFailed:
            raise
        except Exception:
            raise HTTPBenchmarkReviewerFailed("network", retryable=True) from None
        try:
            _, current_request_bytes = _request_payload(request)
        except HTTPBenchmarkReviewerFailed:
            raise HTTPBenchmarkReviewerFailed("request_mutated", retryable=False) from None
        if current_request_bytes != request_bytes:
            raise HTTPBenchmarkReviewerFailed("request_mutated", retryable=False)
        if (
            not isinstance(result, HTTPResult)
            or isinstance(result.status, bool)
            or not isinstance(result.status, int)
            or not 100 <= result.status <= 599
        ):
            raise HTTPBenchmarkReviewerFailed("transport_invalid", retryable=True)
        if result.status != 200:
            if 300 <= result.status <= 399:
                raise HTTPBenchmarkReviewerFailed("redirect", retryable=False)
            retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            raise HTTPBenchmarkReviewerFailed("http_status", retryable=retryable)
        response_headers = _response_headers(result.headers)
        content_type = response_headers.get("content-type", "").lower().replace(" ", "")
        if content_type not in {"application/json", "application/json;charset=utf-8"}:
            raise HTTPBenchmarkReviewerFailed("response_content_type", retryable=True)
        if (
            not isinstance(result.body, bytes)
            or not result.body
            or len(result.body) > MAX_RESPONSE_BYTES
        ):
            raise HTTPBenchmarkReviewerFailed("response_size", retryable=True)
        declared = response_headers.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdecimal()
            or int(declared) != len(result.body)
        ):
            raise HTTPBenchmarkReviewerFailed("response_size", retryable=True)
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            envelope = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise HTTPBenchmarkReviewerFailed("response_json", retryable=True) from None
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema", "review_id", "request_sha256", "review",
        }:
            raise HTTPBenchmarkReviewerFailed("response_invalid", retryable=False)
        if (
            envelope["schema"] != RESPONSE_SCHEMA
            or envelope["review_id"] != review_id
            or envelope["request_sha256"] != request_sha256
        ):
            raise HTTPBenchmarkReviewerFailed("response_binding", retryable=False)
        response = envelope["review"]
        if not isinstance(response, dict):
            raise HTTPBenchmarkReviewerFailed("response_invalid", retryable=False)
        try:
            _BENCHMARK._validate_review(
                response,
                phase=payload["phase"],
                locale=payload["target_locale"],
                blind_id=payload["input"]["blind_id"],
                commercial_dimensions=(
                    payload["input"]["benchmark_suite"].get(
                        "commercial_dimensions",
                    )
                    if payload["phase"] == "source_fidelity"
                    and payload["input"]["content_type"] == "commercial"
                    else None
                ),
            )
        except Exception:
            raise HTTPBenchmarkReviewerFailed("response_invalid", retryable=False) from None
        _canonical_json(response, code="response_invalid", maximum=MAX_RESPONSE_BYTES)
        return response
