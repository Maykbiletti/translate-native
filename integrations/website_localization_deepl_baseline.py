#!/usr/bin/env python3
"""Secure acquisition of DeepL benchmark baselines and lawful fixtures.

The adapter calls only DeepL's documented API origins. It discovers stable
language support at runtime, sends exactly one complete source document, and
returns text-free provenance evidence beside the attested baseline artifact.
Credentials never enter artifacts, evidence, exceptions, or object reprs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


EVIDENCE_SCHEMA = "blun.website-localization-deepl-api-evidence.v1"
FIXTURE_EVIDENCE_SCHEMA = "blun.website-localization-lawful-baseline-fixture.v1"
OFFICIAL_ORIGINS = {
    "free": "https://api-free.deepl.com",
    "pro": "https://api.deepl.com",
}
LANGUAGES_PATH = "/v3/languages?resource=translate_text"
TRANSLATE_PATH = "/v2/translate"
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_AUTH_BYTES = 16 * 1024
MAX_LANGUAGES = 10_000
BCP47 = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,8}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
RIGHTS_BASES = frozenset(("owned", "licensed", "permission"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load baseline dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK = _load_module(
    "blun_website_localization_deepl_benchmark",
    _ROOT / "integrations" / "website_localization_benchmark.py",
)
_WORKER = _BENCHMARK._WORKER
_SUITE = _BENCHMARK._SUITE


class DeepLBaselineFailed(RuntimeError):
    """Content-free acquisition failure understood by durable orchestration."""

    benchmark_campaign_dependency_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("baseline error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("baseline retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


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
    """One bounded stdlib HTTPS attempt with redirects disabled."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: float,
    ) -> HTTPResult:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return HTTPResult(
                int(error.code),
                tuple(error.headers.items()) if error.headers is not None else (),
                b"",
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise DeepLBaselineFailed("deepl.network", retryable=True) from None
        try:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(
                int(response.status), tuple(response.headers.items()), content,
            )
        except (TimeoutError, socket.timeout, OSError):
            raise DeepLBaselineFailed("deepl.network", retryable=True) from None
        finally:
            response.close()


@dataclass(frozen=True)
class BaselineAcquisition:
    artifact: dict[str, Any]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _LanguageCapability:
    code: str
    source: bool
    target: bool


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _json_bytes(value: Any, *, code: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise DeepLBaselineFailed(code, retryable=False) from None
    if not encoded or len(encoded) > maximum:
        raise DeepLBaselineFailed(code, retryable=False)
    return encoded


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_text(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


def _sha256(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DeepLBaselineFailed(code, retryable=False)
    return value


def _identifier(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or _BENCHMARK.IDENTIFIER.fullmatch(value) is None:
        raise DeepLBaselineFailed(code, retryable=False)
    return value


def _response_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, tuple):
        raise DeepLBaselineFailed("deepl.transport_invalid", retryable=True)
    selected: dict[str, str] = {}
    for item in headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise DeepLBaselineFailed("deepl.transport_invalid", retryable=True)
        name, value = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in selected:
                raise DeepLBaselineFailed("deepl.response_headers", retryable=True)
            selected[name] = value
    return selected


def _parse_success(result: Any) -> tuple[Any, str]:
    if (
        not isinstance(result, HTTPResult)
        or isinstance(result.status, bool)
        or not isinstance(result.status, int)
    ):
        raise DeepLBaselineFailed("deepl.transport_invalid", retryable=True)
    status = result.status
    if status != 200:
        retryable = status == 429 or 500 <= status <= 599
        if status == 403:
            code = "deepl.authentication"
        elif status == 456:
            code = "deepl.quota_exceeded"
        elif status == 429:
            code = "deepl.rate_limited"
        elif 500 <= status <= 599:
            code = "deepl.unavailable"
        else:
            code = "deepl.http_error"
        raise DeepLBaselineFailed(code, retryable=retryable)
    headers = _response_headers(result.headers)
    content_type = headers.get("content-type", "").lower()
    if content_type.split(";", 1)[0].strip() != "application/json":
        raise DeepLBaselineFailed("deepl.response_headers", retryable=True)
    if not isinstance(result.body, bytes) or len(result.body) > MAX_RESPONSE_BYTES:
        raise DeepLBaselineFailed("deepl.response_too_large", retryable=True)
    length = headers.get("content-length")
    if length:
        if not length.isdigit() or int(length) != len(result.body):
            raise DeepLBaselineFailed("deepl.response_headers", retryable=True)
    if not result.body or result.body.startswith(b"\xef\xbb\xbf"):
        raise DeepLBaselineFailed("deepl.response_invalid", retryable=True)
    try:
        value = json.loads(
            result.body.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise DeepLBaselineFailed("deepl.response_invalid", retryable=True) from None
    return value, _hash_bytes(result.body)


def _validated_job(job_payload: Any, policy: Any) -> dict[str, Any]:
    try:
        policy = _BENCHMARK._validate_policy(policy)
        job = _WORKER._validated_job(job_payload)
        _BENCHMARK._validate_candidate_job_binding(job, policy)
        if job["target"]["locale"] not in policy.required_locales:
            raise DeepLBaselineFailed("deepl.locale_not_required", retryable=False)
        _SUITE.case_for_job(job)
    except DeepLBaselineFailed:
        raise
    except Exception:
        raise DeepLBaselineFailed("deepl.job_invalid", retryable=False) from None
    return job


class DeepLBaselineAdapter:
    """Acquire one current official-API baseline for one exact benchmark job."""

    def __init__(
        self,
        account_tier: str,
        api_key_provider: Callable[[], str],
        *,
        transport: HTTPTransport | None = None,
        timeout: float = 30.0,
        language_cache_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ):
        if account_tier not in OFFICIAL_ORIGINS:
            raise ValueError("account_tier must be free or pro")
        if not callable(api_key_provider):
            raise TypeError("api_key_provider must be callable")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        if (
            isinstance(language_cache_seconds, bool)
            or not isinstance(language_cache_seconds, (int, float))
            or not math.isfinite(float(language_cache_seconds))
            or not 0 <= float(language_cache_seconds) <= 3600
        ):
            raise ValueError("language_cache_seconds is outside the supported range")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._origin = OFFICIAL_ORIGINS[account_tier]
        self._api_key_provider = api_key_provider
        self._transport = URLTransport() if transport is None else transport
        if not callable(getattr(self._transport, "request", None)):
            raise TypeError("transport must provide request")
        self._timeout = float(timeout)
        self._cache_seconds = float(language_cache_seconds)
        self._clock = clock
        self._language_cache: tuple[
            float, dict[str, _LanguageCapability], str
        ] | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(origin={self._origin!r})"

    def _headers(self, *, content: bool) -> dict[str, str]:
        try:
            key = self._api_key_provider()
        except Exception:
            raise DeepLBaselineFailed("deepl.authentication", retryable=False) from None
        if (
            not isinstance(key, str)
            or not key
            or key != key.strip()
            or not key.isascii()
            or len(key.encode("ascii")) > MAX_AUTH_BYTES
            or any(ord(character) < 33 or ord(character) == 127 for character in key)
        ):
            raise DeepLBaselineFailed("deepl.authentication", retryable=False)
        headers = {
            "Accept": "application/json",
            "Authorization": "DeepL-Auth-Key " + key,
            "User-Agent": "translate-native-benchmark/1",
        }
        if content:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self, method: str, path: str, body: bytes | None,
    ) -> tuple[Any, str]:
        try:
            result = self._transport.request(
                method, self._origin + path,
                self._headers(content=body is not None), body,
                timeout=self._timeout,
            )
        except DeepLBaselineFailed:
            raise
        except Exception:
            raise DeepLBaselineFailed("deepl.transport_failed", retryable=True) from None
        return _parse_success(result)

    def _languages(self) -> tuple[dict[str, _LanguageCapability], str]:
        try:
            now = float(self._clock())
        except Exception:
            raise DeepLBaselineFailed("deepl.clock_invalid", retryable=False) from None
        if not math.isfinite(now) or now < 0:
            raise DeepLBaselineFailed("deepl.clock_invalid", retryable=False)
        if self._language_cache is not None:
            cached_at, capabilities, response_sha256 = self._language_cache
            if now - cached_at < self._cache_seconds:
                return capabilities, response_sha256
        payload, response_sha256 = self._request("GET", LANGUAGES_PATH, None)
        if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_LANGUAGES:
            raise DeepLBaselineFailed("deepl.languages_invalid", retryable=True)
        capabilities: dict[str, _LanguageCapability] = {}
        for item in payload:
            if not isinstance(item, dict):
                raise DeepLBaselineFailed("deepl.languages_invalid", retryable=True)
            code = item.get("lang")
            source = item.get("usable_as_source")
            target = item.get("usable_as_target")
            if (
                not isinstance(code, str)
                or BCP47.fullmatch(code) is None
                or not isinstance(source, bool)
                or not isinstance(target, bool)
                or item.get("status") != "stable"
            ):
                raise DeepLBaselineFailed("deepl.languages_invalid", retryable=True)
            normalized = code.lower()
            if normalized in capabilities:
                raise DeepLBaselineFailed("deepl.languages_invalid", retryable=True)
            capabilities[normalized] = _LanguageCapability(code, source, target)
        self._language_cache = (now, capabilities, response_sha256)
        return capabilities, response_sha256

    @staticmethod
    def _resolve_language(
        locale: Any,
        capabilities: Mapping[str, _LanguageCapability],
        *,
        source: bool,
    ) -> str:
        if not isinstance(locale, str) or BCP47.fullmatch(locale) is None:
            raise DeepLBaselineFailed("deepl.locale_invalid", retryable=False)
        normalized = locale.lower()
        candidates = (normalized, normalized.split("-", 1)[0])
        for candidate in dict.fromkeys(candidates):
            capability = capabilities.get(candidate)
            if capability is not None and (
                capability.source if source else capability.target
            ):
                return capability.code
        role = "source" if source else "target"
        raise DeepLBaselineFailed(
            f"deepl.{role}_language_unsupported", retryable=False,
        )

    def acquire(
        self,
        job_payload: Any,
        policy: Any,
        *,
        evidence_authority: Any,
    ) -> BaselineAcquisition:
        job = _validated_job(job_payload, policy)
        capabilities, capabilities_sha256 = self._languages()
        source_code = self._resolve_language(
            job["source"]["locale"], capabilities, source=True,
        )
        target_code = self._resolve_language(
            job["target"]["locale"], capabilities, source=False,
        )
        if source_code.lower() == target_code.lower():
            raise DeepLBaselineFailed("deepl.language_pair_invalid", retryable=False)
        request_payload = {
            "model_type": "prefer_quality_optimized",
            "preserve_formatting": True,
            "source_lang": source_code,
            "target_lang": target_code,
            "text": [job["source"]["text"]],
        }
        body = _json_bytes(
            request_payload, code="deepl.request_too_large",
            maximum=MAX_REQUEST_BYTES,
        )
        response, response_sha256 = self._request("POST", TRANSLATE_PATH, body)
        if (
            not isinstance(response, dict)
            or set(response) != {"translations"}
            or not isinstance(response["translations"], list)
            or len(response["translations"]) != 1
            or not isinstance(response["translations"][0], dict)
        ):
            raise DeepLBaselineFailed("deepl.translation_invalid", retryable=True)
        translated = response["translations"][0]
        target_text = translated.get("text")
        detected = translated.get("detected_source_language")
        model = translated.get("model_type_used")
        if (
            not isinstance(target_text, str)
            or not target_text.strip()
            or "\x00" in target_text
            or not unicodedata.is_normalized("NFC", target_text)
            or len(target_text.encode("utf-8")) > _BENCHMARK.MAX_TEXT_BYTES
            or (detected is not None and (
                not isinstance(detected, str)
                or detected.lower().split("-", 1)[0]
                != source_code.lower().split("-", 1)[0]
            ))
            or (model is not None and (
                not isinstance(model, str)
                or not model
                or len(model) > 128
            ))
        ):
            raise DeepLBaselineFailed("deepl.translation_invalid", retryable=True)
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "provider": "DeepL",
            "origin": self._origin,
            "languages_response_sha256": capabilities_sha256,
            "request_sha256": _hash_bytes(body),
            "response_sha256": response_sha256,
            "source_sha256": job["source"]["sha256"],
            "source_locale": job["source"]["locale"],
            "source_language": source_code,
            "target_locale": job["target"]["locale"],
            "target_language": target_code,
            "target_sha256": _hash_text(target_text),
            "model_type_used": model,
        }
        evidence_bytes = _json_bytes(
            evidence, code="deepl.evidence_invalid", maximum=MAX_RESPONSE_BYTES,
        )
        evidence_sha256 = _hash_bytes(evidence_bytes)
        provenance = {
            "schema": _BENCHMARK.BASELINE_PROVENANCE_SCHEMA,
            "method": "official_api",
            "evidence_id": "deepl-api:" + evidence_sha256,
            "evidence_sha256": evidence_sha256,
        }
        try:
            artifact = _BENCHMARK.create_baseline_artifact(
                job_payload, target_text, policy, provenance,
                evidence_authority=evidence_authority,
            )
        except Exception:
            raise DeepLBaselineFailed("deepl.attestation_failed", retryable=False) from None
        return BaselineAcquisition(artifact=artifact, evidence=evidence)


def create_lawful_fixture_acquisition(
    job_payload: Any,
    target_text: Any,
    policy: Any,
    fixture_evidence: Mapping[str, Any],
    *,
    evidence_authority: Any,
) -> BaselineAcquisition:
    """Attest a host-supplied fixed baseline with explicit rights evidence."""
    job = _validated_job(job_payload, policy)
    if (
        not isinstance(target_text, str)
        or not target_text.strip()
        or "\x00" in target_text
        or not unicodedata.is_normalized("NFC", target_text)
        or len(target_text.encode("utf-8")) > _BENCHMARK.MAX_TEXT_BYTES
    ):
        raise DeepLBaselineFailed("fixture.target_invalid", retryable=False)
    expected = {
        "schema", "fixture_id", "fixture_revision", "supplier_id",
        "rights_basis", "rights_evidence_sha256", "source_sha256",
        "target_locale", "target_sha256",
    }
    if not isinstance(fixture_evidence, Mapping) or set(fixture_evidence) != expected:
        raise DeepLBaselineFailed("fixture.evidence_invalid", retryable=False)
    evidence = dict(fixture_evidence)
    _identifier(evidence.get("fixture_id"), code="fixture.evidence_invalid")
    _identifier(evidence.get("fixture_revision"), code="fixture.evidence_invalid")
    _identifier(evidence.get("supplier_id"), code="fixture.evidence_invalid")
    _sha256(evidence.get("rights_evidence_sha256"), code="fixture.evidence_invalid")
    if (
        evidence.get("schema") != FIXTURE_EVIDENCE_SCHEMA
        or evidence.get("rights_basis") not in RIGHTS_BASES
        or evidence.get("source_sha256") != job["source"]["sha256"]
        or evidence.get("target_locale") != job["target"]["locale"]
        or evidence.get("target_sha256") != _hash_text(target_text)
    ):
        raise DeepLBaselineFailed("fixture.evidence_invalid", retryable=False)
    evidence_bytes = _json_bytes(
        evidence, code="fixture.evidence_invalid", maximum=MAX_RESPONSE_BYTES,
    )
    evidence_sha256 = _hash_bytes(evidence_bytes)
    provenance = {
        "schema": _BENCHMARK.BASELINE_PROVENANCE_SCHEMA,
        "method": "lawful_fixture",
        "evidence_id": "lawful-fixture:" + evidence_sha256,
        "evidence_sha256": evidence_sha256,
    }
    try:
        artifact = _BENCHMARK.create_baseline_artifact(
            job_payload, target_text, policy, provenance,
            evidence_authority=evidence_authority,
        )
    except Exception:
        raise DeepLBaselineFailed("fixture.attestation_failed", retryable=False) from None
    return BaselineAcquisition(artifact=artifact, evidence=evidence)
