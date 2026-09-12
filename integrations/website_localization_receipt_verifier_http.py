#!/usr/bin/env python3
"""Request-bound HTTPS verifier for localization quality receipts."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


REQUEST_SCHEMA = "blun.localization-receipt-verification-http-request.v1"
RESPONSE_SCHEMA = "blun.localization-receipt-verification-http-response.v1"
RECEIPT_BINDING_SCHEMA = "blun.localization-quality-receipt-binding.v2"
MAX_ENDPOINT_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 4096
MAX_TEXT_BYTES = 2_000_000
MAX_RECEIPT_LENGTH = 16_384
MAX_REQUEST_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 16_384
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
COMMERCIAL_REVIEW_SUMMARY_SCHEMA = "translate-native.commercial-review-summary.v2"
COMMERCIAL_DIMENSIONS = (
    "amount_currency", "discount_basis", "qualifiers", "tax_status",
    "billing_interval", "commitment", "renewal", "cancellation",
    "conditions", "offer_assignment",
)
BINDING_FIELDS = {
    "schema", "review_kind", "job_id", "result_sha256", "source_text",
    "target_text", "source_sha256", "target_sha256", "source_locale",
    "target_locale", "content_type", "glossary_version", "policy_version",
    "primary_provider", "review_provider", "software_version",
    "review_confidence", "quality_profile", "commercial_profile",
    "commercial_review",
    "human_review_required", "independent_review_required",
}
RESERVED_HEADERS = {
    "accept", "accept-encoding", "connection", "content-length",
    "content-type", "host", "idempotency-key", "transfer-encoding",
    "x-localization-receipt-request-id",
    "x-localization-receipt-request-sha256",
}


class HTTPReceiptVerifierFailed(RuntimeError):
    """Content-free verifier failure understood by release coordination."""

    localization_receipt_verification_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("HTTP receipt-verifier error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("HTTP receipt-verifier retryability must be boolean")
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
            raise HTTPReceiptVerifierFailed("network", retryable=True) from None
        try:
            body_bytes = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), body_bytes,
            )
        except (TimeoutError, socket.timeout, OSError):
            raise HTTPReceiptVerifierFailed("network", retryable=True) from None
        finally:
            response.close()


def _fail(code: str, *, retryable: bool = False):
    raise HTTPReceiptVerifierFailed(code, retryable=retryable)


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
        _fail(code)
    if not encoded or len(encoded) > maximum:
        _fail(code)
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


def _authentication_headers(
    provider: Callable[[], Mapping[str, str]],
) -> dict[str, str]:
    try:
        supplied = provider()
    except Exception:
        _fail("authentication")
    if not isinstance(supplied, Mapping) or not supplied:
        _fail("authentication")
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
            _fail("authentication")
        normalized_names.add(normalized)
        result[name] = value
    return result


def _safe_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\x00" not in value
        and unicodedata.is_normalized("NFC", value)
        and len(value.encode("utf-8")) <= MAX_TEXT_BYTES
    )


def _token(value: Any) -> bool:
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _provider(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "model_id", "model_version"}
        and all(_token(value[field]) for field in value)
    )


def _binding(value: Any) -> tuple[dict[str, Any], bytes]:
    if (
        not isinstance(value, dict)
        or set(value) != BINDING_FIELDS
        or value.get("schema") != RECEIPT_BINDING_SCHEMA
        or value.get("review_kind") not in {
            "quality", "qualified_human", "independent_model",
        }
    ):
        _fail("binding_invalid")
    binding = json.loads(_canonical_json(
        value, code="binding_invalid", maximum=MAX_REQUEST_BYTES,
    ))
    for field in (
        "job_id", "source_locale", "target_locale", "content_type",
        "glossary_version", "policy_version", "software_version",
    ):
        if not _token(binding[field]):
            _fail("binding_invalid")
    for field in ("result_sha256", "source_sha256", "target_sha256"):
        if not isinstance(binding[field], str) or SHA256.fullmatch(binding[field]) is None:
            _fail("binding_invalid")
    if (
        not _safe_text(binding["source_text"])
        or not _safe_text(binding["target_text"])
        or hashlib.sha256(binding["source_text"].encode("utf-8")).hexdigest()
        != binding["source_sha256"]
        or hashlib.sha256(binding["target_text"].encode("utf-8")).hexdigest()
        != binding["target_sha256"]
        or not _provider(binding["primary_provider"])
    ):
        _fail("binding_invalid")
    review_provider = binding["review_provider"]
    if (
        (binding["review_kind"] == "independent_model")
        != (review_provider is not None)
        or (review_provider is not None and not _provider(review_provider))
        or (
            review_provider is not None
            and review_provider["id"] == binding["primary_provider"]["id"]
        )
    ):
        _fail("binding_invalid")
    confidence = binding["review_confidence"]
    profile = binding["quality_profile"]
    commercial_review = binding["commercial_review"]
    if (
        not isinstance(confidence, dict)
        or set(confidence) != {"target_native", "source_fidelity"}
        or any(result not in {"high", "low"} for result in confidence.values())
        or not isinstance(profile, dict)
        or set(profile) != {"locale", "version", "sha256"}
        or profile["locale"] != binding["target_locale"]
        or not _token(profile["version"])
        or not isinstance(profile["sha256"], str)
        or SHA256.fullmatch(profile["sha256"]) is None
        or (
            binding["commercial_profile"] is not None
            and not _token(binding["commercial_profile"])
        )
        or ((binding["commercial_profile"] is None) != (commercial_review is None))
        or (
            commercial_review is not None
            and (
                not isinstance(commercial_review, dict)
                or set(commercial_review) != {
                    "schema", "profile", "status",
                    "review_required_dimensions", "evidence_sha256",
                }
                or commercial_review["schema"] != COMMERCIAL_REVIEW_SUMMARY_SCHEMA
                or commercial_review["profile"] != binding["commercial_profile"]
                or commercial_review["status"] not in {
                    "verified", "review_required",
                }
                or not isinstance(
                    commercial_review["review_required_dimensions"], list,
                )
                or commercial_review["review_required_dimensions"] != [
                    name for name in COMMERCIAL_DIMENSIONS
                    if name in commercial_review["review_required_dimensions"]
                ]
                or len(commercial_review["review_required_dimensions"])
                != len(set(commercial_review["review_required_dimensions"]))
                or (
                    commercial_review["status"] == "verified"
                    and commercial_review["review_required_dimensions"]
                )
                or (
                    commercial_review["status"] == "review_required"
                    and (
                        not commercial_review["review_required_dimensions"]
                        or not binding["independent_review_required"]
                    )
                )
                or not isinstance(commercial_review["evidence_sha256"], str)
                or SHA256.fullmatch(commercial_review["evidence_sha256"]) is None
            )
        )
        or type(binding["human_review_required"]) is not bool
        or type(binding["independent_review_required"]) is not bool
    ):
        _fail("binding_invalid")
    return binding, _canonical_json(
        binding, code="binding_invalid", maximum=MAX_REQUEST_BYTES,
    )


def _receipt(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_RECEIPT_LENGTH
        or "\x00" in value
        or not unicodedata.is_normalized("NFC", value)
    ):
        _fail("receipt_invalid")
    return value


def _response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        _fail("transport_invalid", retryable=True)
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            _fail("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                _fail("response_headers", retryable=True)
            result[name] = content
    return result


class HTTPReceiptVerifierAdapter:
    """Perform one HTTPS verification attempt for one exact receipt binding."""

    def __init__(
        self,
        endpoint: str,
        authentication_headers: Callable[[], Mapping[str, str]],
        *,
        transport: HTTPTransport | None = None,
        timeout: float = 30.0,
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

    def verify(self, *, binding: dict[str, Any], receipt: str) -> bool:
        binding, binding_bytes = _binding(binding)
        receipt = _receipt(receipt)
        binding_sha256 = hashlib.sha256(binding_bytes).hexdigest()
        receipt_sha256 = hashlib.sha256(receipt.encode("utf-8")).hexdigest()
        identity = {
            "binding_sha256": binding_sha256,
            "receipt_sha256": receipt_sha256,
        }
        request_id = "blun-l10n-receipt-" + hashlib.sha256(
            _canonical_json(identity, code="request_invalid", maximum=1024),
        ).hexdigest()
        envelope = {
            "schema": REQUEST_SCHEMA,
            "request_id": request_id,
            "binding_sha256": binding_sha256,
            "receipt_sha256": receipt_sha256,
            "binding": binding,
            "receipt": receipt,
        }
        body = _canonical_json(
            envelope, code="request_invalid", maximum=MAX_REQUEST_BYTES,
        )
        request_sha256 = hashlib.sha256(body).hexdigest()
        headers = _authentication_headers(self.authentication_headers)
        headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": request_id,
            "X-Localization-Receipt-Request-Id": request_id,
            "X-Localization-Receipt-Request-Sha256": request_sha256,
        })
        try:
            result = self.transport.post(
                self.endpoint, headers, body, timeout=self.timeout,
            )
        except HTTPReceiptVerifierFailed:
            raise
        except Exception:
            _fail("network", retryable=True)
        if (
            not isinstance(result, HTTPResult)
            or isinstance(result.status, bool)
            or not isinstance(result.status, int)
            or not 100 <= result.status <= 599
        ):
            _fail("transport_invalid", retryable=True)
        if result.status != 200:
            if 300 <= result.status <= 399:
                _fail("redirect")
            retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            _fail("http_status", retryable=retryable)
        headers_out = _response_headers(result.headers)
        content_type = headers_out.get("content-type", "").lower().replace(" ", "")
        if content_type not in {"application/json", "application/json;charset=utf-8"}:
            _fail("response_content_type", retryable=True)
        if (
            not isinstance(result.body, bytes)
            or not result.body
            or len(result.body) > MAX_RESPONSE_BYTES
        ):
            _fail("response_size", retryable=True)
        declared = headers_out.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdecimal()
            or int(declared) != len(result.body)
        ):
            _fail("response_size", retryable=True)
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            response = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            _fail("response_json", retryable=True)
        if not isinstance(response, dict) or set(response) != {
            "schema", "request_id", "request_sha256", "binding_sha256",
            "receipt_sha256", "verified",
        }:
            _fail("response_binding")
        if (
            response["schema"] != RESPONSE_SCHEMA
            or response["request_id"] != request_id
            or response["request_sha256"] != request_sha256
            or response["binding_sha256"] != binding_sha256
            or response["receipt_sha256"] != receipt_sha256
            or type(response["verified"]) is not bool
        ):
            _fail("response_binding")
        return response["verified"]
