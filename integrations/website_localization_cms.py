#!/usr/bin/env python3
"""Signed CMS ingress and all-locales publication outbox.

The host owns the SQLite connections, signature authorities, and transport.
This module accepts one authenticated content-change event, resumes its
idempotent queue insertion, and publishes only a complete signed locale bundle.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol


SCHEMA_VERSION = 4
CHANGE_SCHEMA = "blun.cms-content-change.v2"
CANCELLATION_SCHEMA = "blun.cms-content-cancellation.v1"
TOMBSTONE_SCHEMA = "blun.cms-content-tombstone.v1"
PUBLICATION_SCHEMA = "blun.cms-localization-publication.v3"
ACK_SCHEMA = "blun.cms-localization-publication-ack.v1"
TOMBSTONE_DELIVERY_SCHEMA = "blun.cms-localization-tombstone.v1"
TOMBSTONE_ACK_SCHEMA = "blun.cms-localization-tombstone-ack.v1"
CAPABILITIES_SCHEMA = "blun.website-localization-capabilities.v4"
PUBLICATION_HTTP_CONTRACT_SCHEMA = (
    "blun.cms-localization-publication-http-capabilities.v2"
)
PUBLICATION_HTTP_REQUEST_SCHEMA = "blun.cms-localization-publication-http.v1"
PUBLICATION_HTTP_RESPONSE_SCHEMA = "blun.cms-localization-publication-http-ack.v1"
TOMBSTONE_HTTP_REQUEST_SCHEMA = "blun.cms-localization-tombstone-http.v1"
TOMBSTONE_HTTP_RESPONSE_SCHEMA = "blun.cms-localization-tombstone-http-ack.v1"
PUBLICATION_HEALTH_SCHEMA = "blun.cms-localization-publication-health.v1"
PUBLICATION_HEALTH_ACK_SCHEMA = "blun.cms-localization-publication-health-ack.v1"
PUBLICATION_HEALTH_HTTP_REQUEST_SCHEMA = (
    "blun.cms-localization-publication-health-http.v1"
)
PUBLICATION_HEALTH_HTTP_RESPONSE_SCHEMA = (
    "blun.cms-localization-publication-health-http-ack.v1"
)
PUBLICATION_HTTP_BINDING_HEADERS = (
    ("Idempotency-Key", "delivery_id"),
    ("X-Localization-Delivery-Id", "delivery_id"),
    ("X-Localization-Payload-Sha256", "payload_sha256"),
)
PUBLICATION_HEALTH_HTTP_BINDING_HEADERS = (
    ("Idempotency-Key", "probe_id"),
    ("X-Localization-Probe-Id", "probe_id"),
    ("X-Localization-Contract-Sha256", "contract_sha256"),
)
MAX_MESSAGE_BYTES = 4_000_000
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SIGNATURE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+=-]{1,4096}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVENT_COLUMNS = (
    "event_id", "event_sha256", "event_json", "signature_algorithm", "key_id",
    "signature", "plan_id", "status", "created_at", "updated_at",
)
_DELIVERY_COLUMNS = (
    "delivery_id", "event_id", "plan_id", "payload_json", "payload_sha256",
    "signature_algorithm", "key_id", "signature", "status", "attempts",
    "max_attempts", "next_attempt_at", "lease_owner", "lease_token",
    "lease_expires_at", "last_error_code", "last_error_detail_hash",
    "created_at", "updated_at",
)
_EVENT_TOPIC_COLUMNS = (
    "event_id", "site_id", "source_id", "generation", "created_at",
)
_SUPERSESSION_COLUMNS = (
    "event_id", "superseded_by_event_id", "created_at",
)
_CANCELLATION_COLUMNS = (
    "cancellation_id", "event_id", "cancellation_sha256", "cancellation_json",
    "signature_algorithm", "key_id", "signature", "created_at",
)
_TOMBSTONE_COLUMNS = (
    "tombstone_id", "event_id", "tombstone_sha256", "tombstone_json",
    "request_signature_algorithm", "request_key_id", "request_signature",
    "delivery_id", "plan_id", "payload_json", "payload_sha256",
    "signature_algorithm", "key_id", "signature", "status", "attempts",
    "max_attempts", "next_attempt_at", "lease_owner", "lease_token",
    "lease_expires_at", "last_error_code", "last_error_detail_hash",
    "created_at", "updated_at",
)
_LOCALE_PROFILE_FIELDS = (
    "locale", "eu_code", "language", "native_name", "script",
    "quality_profile_version", "quality_profile_sha256", "direction",
)
_EXPECTED_CONTENT_TYPES = frozenset({
    "headline", "cta", "marketing", "ui", "documentation", "seo", "legal",
    "commercial",
})
_EXPECTED_COMMERCIAL_DIMENSIONS = (
    "amount_currency", "discount_basis", "qualifiers", "tax_status",
    "billing_interval", "commitment", "renewal", "cancellation",
    "conditions", "offer_assignment",
)
_DEFAULT_TARGET_POLICY = "all-eu-official-locales-except-source-language"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required CMS dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_PLANNER = _load_module(
    "blun_website_localization_cms_planner",
    _ROOT / "integrations" / "website_localization.py",
)
_COMMERCIAL = _load_module(
    "blun_website_localization_cms_commercial_profile",
    _ROOT / "integrations" / "commercial_localization_profile.py",
)
_RELEASE = _load_module(
    "blun_website_localization_cms_release",
    _ROOT / "integrations" / "website_localization_release.py",
)
_QUEUE = _RELEASE._QUEUE


class CMSBridgeBlocked(RuntimeError):
    """Stable, content-free CMS bridge failure."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("CMS failure code is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CMSMessageSignature:
    algorithm: str
    key_id: str
    signature: str


class CMSMessageAuthority(Protocol):
    def sign(self, payload: bytes) -> CMSMessageSignature: ...
    def verify(self, payload: bytes, signature: CMSMessageSignature) -> bool: ...


class CMSPublisher(Protocol):
    def publish(self, request: "CMSPublicationRequest") -> Mapping[str, Any]: ...


class CMSPublishFailed(RuntimeError):
    """Provider-neutral transport failure with an explicit retry decision."""

    def __init__(self, code: str, *, retryable: bool, detail: str | None = None):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("publisher failure code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be boolean")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("publisher failure detail must be text")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.detail = detail


def _declared_publish_failure(error: Exception) -> CMSPublishFailed | None:
    """Normalize an adapter failure without depending on module class identity."""

    if isinstance(error, CMSPublishFailed):
        return error
    try:
        declared = error.cms_publish_failure is True
        code = error.code
        retryable = error.retryable
    except Exception:
        return None
    if (
        not declared
        or not isinstance(code, str)
        or ERROR_CODE.fullmatch(code) is None
        or not isinstance(retryable, bool)
    ):
        return None
    full_code = "publisher." + code
    if ERROR_CODE.fullmatch(full_code) is None:
        full_code = "publisher.failure"
    return CMSPublishFailed(full_code, retryable=retryable)


@dataclass(frozen=True)
class IngestedChange:
    event_id: str
    plan_id: str
    job_count: int
    inserted_jobs: int
    status: str


@dataclass(frozen=True)
class CancelledChange:
    cancellation_id: str
    event_id: str
    status: str
    newly_cancelled: bool


@dataclass(frozen=True)
class TombstoneAccepted:
    tombstone_id: str
    event_id: str
    delivery_id: str
    status: str
    newly_requested: bool


@dataclass(frozen=True)
class CMSPublicationRequest:
    delivery_id: str
    payload: dict[str, Any]
    payload_sha256: str
    signature: CMSMessageSignature


@dataclass(frozen=True)
class CMSTombstoneRequest:
    delivery_id: str
    payload: dict[str, Any]
    payload_sha256: str
    signature: CMSMessageSignature


@dataclass(frozen=True)
class ClaimedDelivery:
    request: CMSPublicationRequest
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class ClaimedTombstone:
    request: CMSTombstoneRequest
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class DeliveryStatus:
    delivery_id: str
    event_id: str
    plan_id: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    last_error_code: str | None
    last_error_detail_hash: str | None
    payload_sha256: str


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    delivery_id: str | None = None
    attempt: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class LocaleProgress:
    job_id: str
    target_locale: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    last_error_detail_hash: str | None
    result_sha256: str | None


@dataclass(frozen=True)
class ChangeProgress:
    event_id: str
    site_id: str
    plan_id: str
    website_version: str
    source_sequence: int
    job_count: int
    counts: dict[str, int]
    locales: tuple[LocaleProgress, ...]
    cancelled: bool
    queue_recovery_pending: bool


@dataclass(frozen=True)
class ChangeLifecycle:
    event_id: str
    site_id: str
    plan_id: str
    website_version: str
    source_sequence: int
    status: str
    required_locales: tuple[str, ...]
    approved_locales: tuple[str, ...]
    blocked_locales: tuple[tuple[str, str], ...]
    queue_counts: dict[str, int]
    delivery: dict[str, Any] | None
    tombstone: dict[str, Any] | None


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise CMSBridgeBlocked("cms.json.invalid") from error
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise CMSBridgeBlocked("cms.message.too_large")
    return encoded


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: Any, code: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CMSBridgeBlocked(code)
    if len(value) > limit or "\x00" in value or not unicodedata.is_normalized("NFC", value):
        raise CMSBridgeBlocked(code)
    return value


def _token(value: Any, code: str) -> str:
    value = _text(value, code)
    if TOKEN.fullmatch(value) is None:
        raise CMSBridgeBlocked(code)
    return value


def _timestamp(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CMSBridgeBlocked(code)
    value = float(value)
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise CMSBridgeBlocked(code)
    return value


def _valid_release_evidence(
    value: Any,
    *,
    locale: Any,
    target_sha256: Any,
    approval_id: Any,
) -> bool:
    try:
        evidence = _RELEASE.validate_publication_evidence(value)
    except _RELEASE.LocalizationReleaseBlocked:
        return False
    return (
        evidence["target_locale"] == locale
        and evidence["target_sha256"] == target_sha256
        and evidence["approval_id"] == approval_id
    )


def _positive_integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2**63:
        raise CMSBridgeBlocked(code)
    return value


def _duration(value: Any, code: str, *, maximum: float = MAX_LEASE_SECONDS) -> float:
    value = _timestamp(value, code)
    if value <= 0 or value > maximum:
        raise CMSBridgeBlocked(code)
    return value


def _signature(value: Any, code: str = "cms.signature.invalid") -> CMSMessageSignature:
    if not isinstance(value, CMSMessageSignature):
        expected = tuple(field.name for field in fields(CMSMessageSignature))
        try:
            actual = tuple(field.name for field in fields(value))
            parameters = type(value).__dataclass_params__
            if (
                not is_dataclass(value)
                or isinstance(value, type)
                or type(value).__name__ != CMSMessageSignature.__name__
                or parameters.frozen is not True
                or actual != expected
            ):
                raise TypeError("incompatible signature")
            value = CMSMessageSignature(**{
                field: getattr(value, field) for field in expected
            })
        except Exception:
            raise CMSBridgeBlocked(code) from None
    for part in (value.algorithm, value.key_id):
        if not isinstance(part, str) or TOKEN.fullmatch(part) is None:
            raise CMSBridgeBlocked(code)
    if not isinstance(value.signature, str) or SIGNATURE_VALUE.fullmatch(value.signature) is None:
        raise CMSBridgeBlocked(code)
    return value


def _verify(authority: Any, payload: bytes, signature: CMSMessageSignature, code: str) -> None:
    verify = getattr(authority, "verify", None)
    try:
        accepted = callable(verify) and verify(payload, signature) is True
    except Exception:
        accepted = False
    if not accepted:
        raise CMSBridgeBlocked(code)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise CMSBridgeBlocked("cms.transaction.external")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class WebsiteLocalizationCMSBridge:
    """Authenticated CMS change ingestion and complete-bundle delivery."""

    def __init__(self, connection: sqlite3.Connection, queue: Any, release_store: Any):
        if not isinstance(connection, sqlite3.Connection):
            raise CMSBridgeBlocked("cms.connection.invalid")
        if not isinstance(queue, _QUEUE.LocalizationQueue):
            raise CMSBridgeBlocked("cms.queue.invalid")
        if not isinstance(release_store, _RELEASE.LocalizationReleaseStore):
            raise CMSBridgeBlocked("cms.release_store.invalid")
        if release_store.queue is not queue:
            raise CMSBridgeBlocked("cms.queue_mismatch")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.queue = queue
        self.release_store = release_store
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1, 2, 3, SCHEMA_VERSION}:
            raise CMSBridgeBlocked("cms.schema.unsupported")
        if version == 0:
            self._create_schema()
        elif version == 1:
            self._migrate_v1()
        elif version == 2:
            self._migrate_v2()
        elif version == 3:
            self._migrate_v3()
        self._verify_schema()

    def localization_capabilities(self) -> dict[str, Any]:
        """Return the canonical, content-free planner contract without mutation."""
        try:
            profiles = _PLANNER.EU_OFFICIAL_LOCALES
            content_types = _PLANNER.CONTENT_TYPES
            quality_passes = _PLANNER.QUALITY_PASSES
            if (
                not isinstance(profiles, tuple)
                or len(profiles) != 24
                or not isinstance(content_types, frozenset)
                or content_types != _EXPECTED_CONTENT_TYPES
                or quality_passes != ("target_native", "source_fidelity")
                or _PLANNER.EU_LANGUAGE_SOURCE
                != "https://european-union.europa.eu/principles-countries-history/languages_en"
            ):
                raise CMSBridgeBlocked("cms.capabilities.registry_invalid")

            commercial = _COMMERCIAL.public_profile(_PLANNER.COMMERCIAL_PROFILE)
            review_summary_contract = _COMMERCIAL.public_review_summary_contract(
                _PLANNER.COMMERCIAL_PROFILE,
            )
            if (
                not isinstance(commercial, dict)
                or set(commercial) != {
                    "schema", "profile", "applies_to", "dimensions",
                    "preservation", "rendering", "verification",
                    "locale_quality_profile", "protected_terms",
                    "review_summary_schema",
                    "review_summary_contract", "sha256",
                }
                or commercial["schema"] != _COMMERCIAL.PUBLIC_PROFILE_SCHEMA
                or commercial["profile"] != _PLANNER.COMMERCIAL_PROFILE
                or commercial["review_summary_schema"]
                != _COMMERCIAL.REVIEW_SUMMARY_SCHEMA
                or commercial["review_summary_contract"] != review_summary_contract
                or commercial["applies_to"] != {
                    "content_type": "commercial",
                    "locales": "all-supported-target-locales",
                }
                or commercial["protected_terms"] != "project-configuration-only"
                or commercial["locale_quality_profile"] != {
                    "schema": _COMMERCIAL.COMMERCIAL_LOCALE_PROFILE_SCHEMA,
                    "required": True,
                    "binding_fields": [
                        "locale", "version", "commercial_profile",
                        "quality_profile_version", "quality_profile_sha256",
                        "sha256",
                    ],
                    "required_commercial_checks": list(
                        _EXPECTED_COMMERCIAL_DIMENSIONS
                    ),
                    "provider_phases": [
                        "transcreation", "target_native", "source_fidelity",
                    ],
                    "tamper_policy": "block-before-provider",
                }
                or [item.get("name") for item in commercial["dimensions"]]
                != list(_EXPECTED_COMMERCIAL_DIMENSIONS)
                or tuple(_COMMERCIAL.DIMENSIONS) != _EXPECTED_COMMERCIAL_DIMENSIONS
            ):
                raise CMSBridgeBlocked("cms.capabilities.registry_invalid")
            unsigned_review_summary_contract = dict(review_summary_contract)
            review_summary_digest = unsigned_review_summary_contract.pop(
                "sha256", None,
            )
            if (
                set(review_summary_contract) != {
                    "schema", "result_schema", "profile", "required_fields",
                    "statuses", "review_required_dimensions", "evidence_sha256",
                    "content_policy", "sha256",
                }
                or review_summary_contract["schema"]
                != _COMMERCIAL.REVIEW_SUMMARY_CAPABILITIES_SCHEMA
                or review_summary_contract["result_schema"]
                != _COMMERCIAL.REVIEW_SUMMARY_SCHEMA
                or review_summary_contract["profile"]
                != _PLANNER.COMMERCIAL_PROFILE
                or review_summary_contract["required_fields"] != [
                    "schema", "profile", "status", "review_required_dimensions",
                    "evidence_sha256",
                ]
                or review_summary_contract["statuses"] != {
                    "verified": {"review_required_dimensions": "empty"},
                    "review_required": {
                        "review_required_dimensions": "one-or-more",
                        "requires_independent_review": True,
                    },
                }
                or review_summary_contract["review_required_dimensions"] != {
                    "allowed": list(_EXPECTED_COMMERCIAL_DIMENSIONS),
                    "order": list(_EXPECTED_COMMERCIAL_DIMENSIONS),
                    "unique": True,
                }
                or review_summary_contract["evidence_sha256"] != {
                    "algorithm": "sha-256",
                    "canonicalization": (
                        "utf-8-json-sort-keys-no-insignificant-whitespace"
                    ),
                    "binding_schema": _COMMERCIAL.EVIDENCE_BINDING_SCHEMA,
                    "binding_fields": [
                        "schema", "profile", "source_sha256", "target_sha256",
                        "evidence",
                    ],
                    "text_hashing": "exact-utf-8",
                    "covers": [
                        "commercial-profile",
                        "exact-source-sha256",
                        "exact-target-sha256",
                        "complete-commercial-review-evidence",
                    ],
                }
                or review_summary_contract["content_policy"] != {
                    "source_text": False,
                    "target_text": False,
                    "source_spans": False,
                    "target_spans": False,
                    "reviewer_prose": False,
                    "project_prices": False,
                    "project_brands": False,
                }
                or not isinstance(review_summary_digest, str)
                or SHA256.fullmatch(review_summary_digest) is None
                or review_summary_digest
                != _hash(_canonical_json(unsigned_review_summary_contract))
            ):
                raise CMSBridgeBlocked("cms.capabilities.registry_invalid")
            unsigned_commercial = dict(commercial)
            commercial_digest = unsigned_commercial.pop("sha256", None)
            if (
                not isinstance(commercial_digest, str)
                or SHA256.fullmatch(commercial_digest) is None
                or commercial_digest != _hash(_canonical_json(unsigned_commercial))
            ):
                raise CMSBridgeBlocked("cms.capabilities.registry_invalid")

            locales = []
            seen_locales: set[str] = set()
            seen_languages: set[str] = set()
            seen_eu_codes: set[str] = set()
            for profile in profiles:
                if (
                    not is_dataclass(profile)
                    or isinstance(profile, type)
                    or type(profile).__name__ != "LocaleProfile"
                    or type(profile).__dataclass_params__.frozen is not True
                    or tuple(field.name for field in fields(profile))
                    != _LOCALE_PROFILE_FIELDS
                ):
                    raise CMSBridgeBlocked("cms.capabilities.registry_invalid")
                item = {
                    field: getattr(profile, field) for field in _LOCALE_PROFILE_FIELDS
                }
                locale = _token(item["locale"], "cms.capabilities.registry_invalid")
                eu_code = _token(item["eu_code"], "cms.capabilities.registry_invalid")
                language = _token(item["language"], "cms.capabilities.registry_invalid")
                native_name = _text(
                    item["native_name"], "cms.capabilities.registry_invalid",
                )
                script = _token(item["script"], "cms.capabilities.registry_invalid")
                version = _token(
                    item["quality_profile_version"],
                    "cms.capabilities.registry_invalid",
                )
                digest = item["quality_profile_sha256"]
                direction = item["direction"]
                if (
                    _PLANNER.canonicalize_locale(locale) != locale
                    or locale.split("-", 1)[0] != language
                    or not isinstance(digest, str)
                    or SHA256.fullmatch(digest) is None
                    or direction not in {"ltr", "rtl"}
                    or locale in seen_locales
                    or language in seen_languages
                    or eu_code in seen_eu_codes
                ):
                    raise CMSBridgeBlocked("cms.capabilities.registry_invalid")
                quality = _PLANNER.quality_profile_for(locale)
                commercial_quality = _PLANNER.commercial_quality_profile_for(
                    locale,
                )
                if (
                    not isinstance(quality, dict)
                    or quality.get("locale") != locale
                    or quality.get("version") != version
                    or quality.get("sha256") != digest
                    or not isinstance(commercial_quality, dict)
                    or commercial_quality.get("locale") != locale
                    or commercial_quality.get("commercial_profile")
                    != _PLANNER.COMMERCIAL_PROFILE
                    or commercial_quality.get("quality_profile_version")
                    != version
                    or commercial_quality.get("quality_profile_sha256")
                    != digest
                    or commercial_quality.get("schema")
                    != _COMMERCIAL.COMMERCIAL_LOCALE_PROFILE_SCHEMA
                    or not isinstance(commercial_quality.get("version"), str)
                    or TOKEN.fullmatch(commercial_quality["version"]) is None
                    or not isinstance(commercial_quality.get("sha256"), str)
                    or SHA256.fullmatch(commercial_quality["sha256"]) is None
                ):
                    raise CMSBridgeBlocked("cms.capabilities.registry_invalid")
                seen_locales.add(locale)
                seen_languages.add(language)
                seen_eu_codes.add(eu_code)
                locales.append({
                    "locale": locale,
                    "eu_code": eu_code,
                    "language": language,
                    "native_name": native_name,
                    "script": script,
                    "direction": direction,
                    "quality_profile_version": version,
                    "quality_profile_sha256": digest,
                    "commercial_quality_profile_version": (
                        commercial_quality["version"]
                    ),
                    "commercial_quality_profile_sha256": (
                        commercial_quality["sha256"]
                    ),
                })

            body = {
                "schema": CAPABILITIES_SCHEMA,
                "change_schema": CHANGE_SCHEMA,
                "cancellation_schema": CANCELLATION_SCHEMA,
                "tombstone_schema": TOMBSTONE_SCHEMA,
                "publication_schema": PUBLICATION_SCHEMA,
                "tombstone_delivery_schema": TOMBSTONE_DELIVERY_SCHEMA,
                "plan_schema": _token(
                    _PLANNER.SCHEMA, "cms.capabilities.registry_invalid",
                ),
                "job_schema": _token(
                    _PLANNER.JOB_SCHEMA, "cms.capabilities.registry_invalid",
                ),
                "eu_language_source": _PLANNER.EU_LANGUAGE_SOURCE,
                "default_target_policy": _DEFAULT_TARGET_POLICY,
                "content_types": sorted(content_types),
                "quality_passes": list(quality_passes),
                "commercial_profile": commercial,
                "publication_http": self._publication_http_capabilities(),
                "locales": locales,
            }
            canonical = _canonical_json(body)
            return {**body, "sha256": _hash(canonical)}
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.capabilities.registry_invalid") from None

    @staticmethod
    def _publication_http_capabilities() -> dict[str, Any]:
        """Describe the built-in outbound adapter without deployment secrets."""
        try:
            operations = [
                {
                    "name": "publication",
                    "payload_schema": _token(
                        PUBLICATION_SCHEMA, "cms.capabilities.registry_invalid",
                    ),
                    "request_schema": _token(
                        PUBLICATION_HTTP_REQUEST_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "acknowledgement_schema": _token(
                        ACK_SCHEMA, "cms.capabilities.registry_invalid",
                    ),
                    "response_schema": _token(
                        PUBLICATION_HTTP_RESPONSE_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "acknowledgement_status": "accepted",
                },
                {
                    "name": "tombstone",
                    "payload_schema": _token(
                        TOMBSTONE_DELIVERY_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "request_schema": _token(
                        TOMBSTONE_HTTP_REQUEST_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "acknowledgement_schema": _token(
                        TOMBSTONE_ACK_SCHEMA, "cms.capabilities.registry_invalid",
                    ),
                    "response_schema": _token(
                        TOMBSTONE_HTTP_RESPONSE_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "acknowledgement_status": "deleted",
                },
                {
                    "name": "health",
                    "payload_schema": _token(
                        PUBLICATION_HEALTH_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "request_schema": _token(
                        PUBLICATION_HEALTH_HTTP_REQUEST_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "acknowledgement_schema": _token(
                        PUBLICATION_HEALTH_ACK_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "response_schema": _token(
                        PUBLICATION_HEALTH_HTTP_RESPONSE_SCHEMA,
                        "cms.capabilities.registry_invalid",
                    ),
                    "acknowledgement_status": "healthy",
                },
            ]
            headers = []
            seen_headers: set[str] = set()
            for name, binding in PUBLICATION_HTTP_BINDING_HEADERS:
                if (
                    not isinstance(name, str)
                    or not name.isascii()
                    or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", name)
                    or binding not in {"delivery_id", "payload_sha256"}
                    or name.lower() in seen_headers
                ):
                    raise ValueError
                seen_headers.add(name.lower())
                headers.append({
                    "name": name,
                    "binding": _token(
                        binding, "cms.capabilities.registry_invalid",
                    ),
                })
            body = {
                "schema": _token(
                    PUBLICATION_HTTP_CONTRACT_SCHEMA,
                    "cms.capabilities.registry_invalid",
                ),
                "method": "POST",
                "request_content_type": "application/json; charset=utf-8",
                "response_content_types": [
                    "application/json", "application/json; charset=utf-8",
                ],
                "delivery_semantics": "at-least-once",
                "release_evidence_schema": _token(
                    _RELEASE.PUBLICATION_EVIDENCE_SCHEMA,
                    "cms.capabilities.registry_invalid",
                ),
                "binding_headers": headers,
                "health_binding_headers": [
                    {
                        "name": _token(
                            name, "cms.capabilities.registry_invalid",
                        ),
                        "binding": _token(
                            binding, "cms.capabilities.registry_invalid",
                        ),
                    }
                    for name, binding in PUBLICATION_HEALTH_HTTP_BINDING_HEADERS
                ],
                "operations": operations,
            }
            health_headers = body["health_binding_headers"]
            if (
                len(health_headers) != len(PUBLICATION_HEALTH_HTTP_BINDING_HEADERS)
                or len({item["name"].lower() for item in health_headers})
                != len(health_headers)
                or any(
                    item["binding"] not in {"probe_id", "contract_sha256"}
                    or not item["name"].isascii()
                    or re.fullmatch(r"[A-Za-z0-9-]{1,128}", item["name"]) is None
                    for item in health_headers
                )
            ):
                raise ValueError
            return {**body, "sha256": _hash(_canonical_json(body))}
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.capabilities.registry_invalid") from None

    def _create_revision_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE cms_event_topics (
                event_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation > 0),
                created_at REAL NOT NULL,
                FOREIGN KEY (event_id) REFERENCES cms_change_events (event_id),
                UNIQUE (site_id, source_id, generation)
            )
        """)
        self.connection.execute("""
            CREATE INDEX cms_event_topic_order
            ON cms_event_topics (site_id, source_id, generation, event_id)
        """)
        self.connection.execute("""
            CREATE TABLE cms_event_supersessions (
                event_id TEXT PRIMARY KEY,
                superseded_by_event_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                CHECK (event_id <> superseded_by_event_id),
                FOREIGN KEY (event_id) REFERENCES cms_change_events (event_id),
                FOREIGN KEY (superseded_by_event_id) REFERENCES cms_change_events (event_id)
            )
        """)
        self._create_cancellation_schema()

    def _create_cancellation_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS cms_event_cancellations (
                cancellation_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                cancellation_sha256 TEXT NOT NULL,
                cancellation_json TEXT NOT NULL,
                signature_algorithm TEXT NOT NULL,
                key_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (event_id) REFERENCES cms_change_events (event_id)
            )
        """)

    def _create_tombstone_schema(self) -> None:
        self.connection.execute(f"""
            CREATE TABLE IF NOT EXISTS cms_tombstone_deliveries (
                tombstone_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                tombstone_sha256 TEXT NOT NULL,
                tombstone_json TEXT NOT NULL,
                request_signature_algorithm TEXT NOT NULL,
                request_key_id TEXT NOT NULL,
                request_signature TEXT NOT NULL,
                delivery_id TEXT NOT NULL UNIQUE,
                plan_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                signature_algorithm TEXT NOT NULL,
                key_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'leased', 'retry_wait', 'succeeded', 'failed')
                ),
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                max_attempts INTEGER NOT NULL CHECK (
                    max_attempts BETWEEN 1 AND {MAX_ATTEMPTS}
                ),
                next_attempt_at REAL NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at REAL,
                last_error_code TEXT,
                last_error_detail_hash TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (event_id) REFERENCES cms_change_events (event_id)
            )
        """)
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS cms_tombstone_ready
            ON cms_tombstone_deliveries (status, next_attempt_at, created_at, delivery_id)
        """)

    def _create_schema(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE cms_change_events (
                    event_id TEXT PRIMARY KEY,
                    event_sha256 TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    signature_algorithm TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('accepted', 'enqueued')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.connection.execute(f"""
                CREATE TABLE cms_publication_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    signature_algorithm TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'leased', 'retry_wait', 'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (
                        max_attempts BETWEEN 1 AND {MAX_ATTEMPTS}
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_error_detail_hash TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (event_id) REFERENCES cms_change_events (event_id)
                )
            """)
            self.connection.execute("""
                CREATE INDEX cms_publication_ready
                ON cms_publication_deliveries (status, next_attempt_at, created_at, delivery_id)
            """)
            self._create_revision_schema()
            self._create_tombstone_schema()
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_v1(self) -> None:
        try:
            with _transaction(self.connection):
                event_columns = tuple(
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(cms_change_events)")
                )
                delivery_columns = tuple(
                    row["name"]
                    for row in self.connection.execute(
                        "PRAGMA table_info(cms_publication_deliveries)"
                    )
                )
                if event_columns != _EVENT_COLUMNS or delivery_columns != _DELIVERY_COLUMNS:
                    raise CMSBridgeBlocked("cms.schema.altered")
                self._create_revision_schema()
                generations: dict[tuple[str, str], int] = {}
                last_created_at: dict[tuple[str, str], float] = {}
                rows = self.connection.execute(
                    "SELECT * FROM cms_change_events ORDER BY created_at, event_id"
                ).fetchall()
                for row in rows:
                    if _hash(row["event_json"]) != row["event_sha256"]:
                        raise CMSBridgeBlocked("cms.migration.event_invalid")
                    try:
                        event = json.loads(row["event_json"])
                    except json.JSONDecodeError:
                        raise CMSBridgeBlocked("cms.migration.event_invalid") from None
                    if _canonical_json(event) != row["event_json"]:
                        raise CMSBridgeBlocked("cms.migration.event_invalid")
                    event, plan = self._validated_event(event, allow_legacy=True)
                    if plan.plan_id != row["plan_id"]:
                        raise CMSBridgeBlocked("cms.migration.event_invalid")
                    topic = (event["site_id"], event["localization"]["source_id"])
                    if event.get("schema") == CHANGE_SCHEMA:
                        generation = event["source_sequence"]
                    else:
                        if last_created_at.get(topic) == float(row["created_at"]):
                            raise CMSBridgeBlocked("cms.migration.order_ambiguous")
                        generation = generations.get(topic, 0) + 1
                    generations[topic] = max(generations.get(topic, 0), generation)
                    last_created_at[topic] = float(row["created_at"])
                    self.connection.execute(
                        "INSERT INTO cms_event_topics VALUES (?, ?, ?, ?, ?)",
                        (row["event_id"], *topic, generation, row["created_at"]),
                    )
                self.connection.execute("""
                    INSERT INTO cms_event_supersessions
                    SELECT older.event_id, newest.event_id, newest.created_at
                    FROM cms_event_topics AS older
                    JOIN cms_change_events AS older_event
                        ON older_event.event_id = older.event_id
                    JOIN cms_event_topics AS newest
                        ON newest.site_id = older.site_id
                       AND newest.source_id = older.source_id
                    JOIN cms_change_events AS newest_event
                        ON newest_event.event_id = newest.event_id
                    LEFT JOIN cms_publication_deliveries AS delivery
                        ON delivery.event_id = older.event_id
                       AND delivery.status = 'succeeded'
                    WHERE older_event.status = 'enqueued'
                      AND newest_event.status = 'enqueued'
                      AND newest.generation = (
                          SELECT MAX(candidate.generation)
                          FROM cms_event_topics AS candidate
                          JOIN cms_change_events AS candidate_event
                              ON candidate_event.event_id = candidate.event_id
                          WHERE candidate.site_id = older.site_id
                            AND candidate.source_id = older.source_id
                            AND candidate_event.status = 'enqueued'
                      )
                      AND older.generation < newest.generation
                      AND delivery.event_id IS NULL
                """)
                self.connection.execute("""
                    UPDATE cms_publication_deliveries
                    SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error_code = 'event_superseded',
                        last_error_detail_hash = NULL, updated_at = (
                            SELECT created_at FROM cms_event_supersessions
                            WHERE cms_event_supersessions.event_id =
                                  cms_publication_deliveries.event_id
                        )
                    WHERE event_id IN (SELECT event_id FROM cms_event_supersessions)
                      AND status <> 'succeeded'
                """)
                self._create_tombstone_schema()
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.migration.failed") from None

    def _migrate_v2(self) -> None:
        try:
            with _transaction(self.connection):
                event_columns = tuple(
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(cms_change_events)")
                )
                delivery_columns = tuple(
                    row["name"] for row in self.connection.execute(
                        "PRAGMA table_info(cms_publication_deliveries)"
                    )
                )
                topic_columns = tuple(
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(cms_event_topics)")
                )
                supersession_columns = tuple(
                    row["name"] for row in self.connection.execute(
                        "PRAGMA table_info(cms_event_supersessions)"
                    )
                )
                if (
                    event_columns != _EVENT_COLUMNS
                    or delivery_columns != _DELIVERY_COLUMNS
                    or topic_columns != _EVENT_TOPIC_COLUMNS
                    or supersession_columns != _SUPERSESSION_COLUMNS
                ):
                    raise CMSBridgeBlocked("cms.schema.altered")
                self._create_cancellation_schema()
                self._create_tombstone_schema()
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.migration.failed") from None

    def _migrate_v3(self) -> None:
        try:
            with _transaction(self.connection):
                self._create_tombstone_schema()
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.migration.failed") from None

    def _verify_schema(self) -> None:
        event_columns = tuple(
            row["name"] for row in self.connection.execute("PRAGMA table_info(cms_change_events)")
        )
        delivery_columns = tuple(
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(cms_publication_deliveries)")
        )
        topic_columns = tuple(
            row["name"] for row in self.connection.execute("PRAGMA table_info(cms_event_topics)")
        )
        supersession_columns = tuple(
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(cms_event_supersessions)")
        )
        cancellation_columns = tuple(
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(cms_event_cancellations)")
        )
        tombstone_columns = tuple(
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(cms_tombstone_deliveries)")
        )
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if (
            version != SCHEMA_VERSION
            or event_columns != _EVENT_COLUMNS
            or delivery_columns != _DELIVERY_COLUMNS
            or topic_columns != _EVENT_TOPIC_COLUMNS
            or supersession_columns != _SUPERSESSION_COLUMNS
            or cancellation_columns != _CANCELLATION_COLUMNS
            or tombstone_columns != _TOMBSTONE_COLUMNS
        ):
            raise CMSBridgeBlocked("cms.schema.altered")

    def _validated_event(
        self,
        event: Any,
        *,
        allow_legacy: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        if not isinstance(event, dict):
            raise CMSBridgeBlocked("cms.event.invalid")
        if not isinstance(allow_legacy, bool):
            raise CMSBridgeBlocked("cms.event.legacy_mode_invalid")
        expected = {
            "schema", "event_id", "site_id", "website_version",
            "source_sequence", "localization",
        }
        legacy = event.get("schema") == "blun.cms-content-change.v1"
        if legacy:
            expected.remove("source_sequence")
        if (
            set(event) != expected
            or (event.get("schema") != CHANGE_SCHEMA and not (allow_legacy and legacy))
        ):
            raise CMSBridgeBlocked("cms.event.invalid")
        _token(event.get("event_id"), "cms.event_id.invalid")
        _token(event.get("site_id"), "cms.site_id.invalid")
        _token(event.get("website_version"), "cms.website_version.invalid")
        if not legacy:
            _positive_integer(event.get("source_sequence"), "cms.source_sequence.invalid")
        localization = event.get("localization")
        try:
            plan = _PLANNER.plan_from_mapping(localization)
        except _PLANNER.LocalizationPlanBlocked:
            raise CMSBridgeBlocked("cms.localization.invalid") from None
        return event, plan

    def _validated_cancellation(self, cancellation: Any) -> dict[str, Any]:
        expected = {
            "schema", "cancellation_id", "event_id", "site_id",
            "website_version", "source_id", "source_sequence",
        }
        if (
            not isinstance(cancellation, dict)
            or set(cancellation) != expected
            or cancellation.get("schema") != CANCELLATION_SCHEMA
        ):
            raise CMSBridgeBlocked("cms.cancellation.invalid")
        for field in (
            "cancellation_id", "event_id", "site_id", "website_version", "source_id",
        ):
            _token(cancellation.get(field), f"cms.cancellation.{field}_invalid")
        _positive_integer(
            cancellation.get("source_sequence"),
            "cms.cancellation.source_sequence_invalid",
        )
        return cancellation

    def _validated_tombstone(self, tombstone: Any) -> dict[str, Any]:
        expected = {
            "schema", "tombstone_id", "event_id", "site_id",
            "website_version", "source_id", "source_sequence",
        }
        if (
            not isinstance(tombstone, dict)
            or set(tombstone) != expected
            or tombstone.get("schema") != TOMBSTONE_SCHEMA
        ):
            raise CMSBridgeBlocked("cms.tombstone.invalid")
        for field in (
            "tombstone_id", "event_id", "site_id", "website_version", "source_id",
        ):
            _token(tombstone.get(field), f"cms.tombstone.{field}_invalid")
        _positive_integer(
            tombstone.get("source_sequence"),
            "cms.tombstone.source_sequence_invalid",
        )
        return tombstone

    def _verify_stored_cancellation(
        self,
        row: sqlite3.Row,
        event: dict[str, Any],
        topic: sqlite3.Row,
        event_key_id: str,
        verifier: CMSMessageAuthority,
    ) -> dict[str, Any]:
        if _hash(row["cancellation_json"]) != row["cancellation_sha256"]:
            raise CMSBridgeBlocked("cms.cancellation.tampered")
        try:
            cancellation = json.loads(row["cancellation_json"])
        except json.JSONDecodeError:
            raise CMSBridgeBlocked("cms.cancellation.tampered") from None
        if _canonical_json(cancellation) != row["cancellation_json"]:
            raise CMSBridgeBlocked("cms.cancellation.tampered")
        cancellation = self._validated_cancellation(cancellation)
        signature = _signature(CMSMessageSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        _verify(
            verifier,
            row["cancellation_json"].encode("utf-8"),
            signature,
            "cms.cancellation.signature_rejected",
        )
        if (
            cancellation["cancellation_id"] != row["cancellation_id"]
            or cancellation["event_id"] != row["event_id"]
            or cancellation["event_id"] != event["event_id"]
            or cancellation["site_id"] != event["site_id"]
            or cancellation["website_version"] != event["website_version"]
            or cancellation["source_id"] != event["localization"]["source_id"]
            or cancellation["source_sequence"] != int(topic["generation"])
            or row["key_id"] != event_key_id
        ):
            raise CMSBridgeBlocked("cms.cancellation.binding_invalid")
        return cancellation

    def cancel_change(
        self,
        cancellation: Any,
        signature: CMSMessageSignature,
        verifier: CMSMessageAuthority,
        *,
        now: float | int,
    ) -> CancelledChange:
        cancellation = self._validated_cancellation(cancellation)
        signature = _signature(signature)
        now = _timestamp(now, "cms.time.invalid")
        cancellation_json = _canonical_json(cancellation)
        cancellation_hash = _hash(cancellation_json)
        _verify(
            verifier,
            cancellation_json.encode("utf-8"),
            signature,
            "cms.cancellation.signature_rejected",
        )
        event, _ = self._load_event(
            cancellation["event_id"],
            verifier,
            allow_superseded=True,
            allow_cancelled=True,
            allow_accepted=True,
        )
        topic = self.connection.execute(
            "SELECT * FROM cms_event_topics WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        event_row = self.connection.execute(
            "SELECT key_id FROM cms_change_events WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if topic is None or event_row is None:
            raise CMSBridgeBlocked("cms.event.topic_invalid")
        if signature.key_id != event_row["key_id"]:
            raise CMSBridgeBlocked("cms.cancellation.scope_rejected")
        expected = {
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "website_version": event["website_version"],
            "source_id": event["localization"]["source_id"],
            "source_sequence": int(topic["generation"]),
        }
        if any(cancellation[field] != value for field, value in expected.items()):
            raise CMSBridgeBlocked("cms.cancellation.binding_invalid")

        with _transaction(self.connection):
            delivery = self.connection.execute("""
                SELECT status FROM cms_publication_deliveries WHERE event_id = ?
            """, (event["event_id"],)).fetchone()
            if delivery is not None and delivery["status"] == "succeeded":
                raise CMSBridgeBlocked("cms.cancellation.already_published")
            if delivery is not None and delivery["status"] == "leased":
                raise CMSBridgeBlocked("cms.cancellation.delivery_in_flight")
            by_id = self.connection.execute("""
                SELECT event_id, cancellation_sha256
                FROM cms_event_cancellations WHERE cancellation_id = ?
            """, (cancellation["cancellation_id"],)).fetchone()
            by_event = self.connection.execute("""
                SELECT cancellation_id, cancellation_sha256
                FROM cms_event_cancellations WHERE event_id = ?
            """, (event["event_id"],)).fetchone()
            if by_id is not None and (
                by_id["event_id"] != event["event_id"]
                or by_id["cancellation_sha256"] != cancellation_hash
            ):
                raise CMSBridgeBlocked("cms.cancellation.idempotency_collision")
            if by_event is not None and (
                by_event["cancellation_id"] != cancellation["cancellation_id"]
                or by_event["cancellation_sha256"] != cancellation_hash
            ):
                raise CMSBridgeBlocked("cms.cancellation.idempotency_collision")
            newly_cancelled = by_id is None and by_event is None
            if newly_cancelled:
                self.connection.execute("""
                    INSERT INTO cms_event_cancellations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cancellation["cancellation_id"], event["event_id"],
                    cancellation_hash, cancellation_json, signature.algorithm,
                    signature.key_id, signature.signature, now,
                ))
            self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'event_cancelled',
                    last_error_detail_hash = NULL, updated_at = ?
                WHERE event_id = ? AND status <> 'succeeded'
            """, (now, event["event_id"]))
        return CancelledChange(
            cancellation["cancellation_id"], event["event_id"], "cancelled",
            newly_cancelled,
        )

    def request_tombstone(
        self,
        tombstone: Any,
        signature: CMSMessageSignature,
        event_verifier: CMSMessageAuthority,
        publication_authority: CMSMessageAuthority,
        *,
        now: float | int,
        max_attempts: int = 5,
    ) -> TombstoneAccepted:
        """Accept and sign deletion only for an exactly confirmed publication."""
        tombstone = self._validated_tombstone(tombstone)
        signature = _signature(signature)
        now = _timestamp(now, "cms.time.invalid")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise CMSBridgeBlocked("cms.max_attempts.invalid")
        tombstone_json = _canonical_json(tombstone)
        tombstone_hash = _hash(tombstone_json)
        _verify(
            event_verifier,
            tombstone_json.encode("utf-8"),
            signature,
            "cms.tombstone.signature_rejected",
        )
        event, plan = self._load_event(
            tombstone["event_id"], event_verifier, allow_superseded=True,
        )
        topic = self.connection.execute(
            "SELECT generation FROM cms_event_topics WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        event_row = self.connection.execute(
            "SELECT key_id FROM cms_change_events WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if topic is None or event_row is None:
            raise CMSBridgeBlocked("cms.event.topic_invalid")
        if signature.key_id != event_row["key_id"]:
            raise CMSBridgeBlocked("cms.tombstone.scope_rejected")
        expected = {
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "website_version": event["website_version"],
            "source_id": event["localization"]["source_id"],
            "source_sequence": int(topic["generation"]),
        }
        if any(tombstone[field] != value for field, value in expected.items()):
            raise CMSBridgeBlocked("cms.tombstone.binding_invalid")
        if self.connection.execute(
            "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone() is not None:
            raise CMSBridgeBlocked("cms.tombstone.not_published")
        publication_row = self.connection.execute(
            "SELECT * FROM cms_publication_deliveries WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if publication_row is None or publication_row["status"] != "succeeded":
            raise CMSBridgeBlocked("cms.tombstone.not_published")
        publication = self._request_from_row(
            publication_row, publication_authority, now,
            require_current_approvals=False,
        )

        existing = self.connection.execute(
            "SELECT * FROM cms_tombstone_deliveries WHERE tombstone_id = ? OR event_id = ?",
            (tombstone["tombstone_id"], event["event_id"]),
        ).fetchone()
        if existing is not None:
            if (
                existing["tombstone_id"] != tombstone["tombstone_id"]
                or existing["event_id"] != event["event_id"]
                or existing["tombstone_sha256"] != tombstone_hash
                or existing["request_key_id"] != signature.key_id
            ):
                raise CMSBridgeBlocked("cms.tombstone.idempotency_collision")
            request = self._tombstone_request_from_row(
                existing, event_verifier, publication_authority,
            )
            return TombstoneAccepted(
                tombstone["tombstone_id"], event["event_id"],
                request.delivery_id, existing["status"], False,
            )

        locales = sorted(
            item["locale"] for item in publication.payload["localizations"]
        )
        unsigned = {
            "schema": TOMBSTONE_DELIVERY_SCHEMA,
            "tombstone_id": tombstone["tombstone_id"],
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "website_version": event["website_version"],
            "plan_id": plan.plan_id,
            "source_id": event["localization"]["source_id"],
            "source_sequence": int(topic["generation"]),
            "publication_delivery_id": publication.delivery_id,
            "publication_payload_sha256": publication.payload_sha256,
            "locales": locales,
        }
        delivery_id = "blun-cms-tombstone-" + _hash(_canonical_json(unsigned))
        payload = {**unsigned, "delivery_id": delivery_id}
        payload_json = _canonical_json(payload)
        payload_hash = _hash(payload_json)
        sign = getattr(publication_authority, "sign", None)
        try:
            delivery_signature = _signature(
                sign(payload_json.encode("utf-8")) if callable(sign) else None,
            )
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.tombstone.signing_failed") from None
        _verify(
            publication_authority,
            payload_json.encode("utf-8"),
            delivery_signature,
            "cms.tombstone.signature_rejected",
        )
        with _transaction(self.connection):
            concurrent = self.connection.execute(
                "SELECT * FROM cms_tombstone_deliveries WHERE tombstone_id = ? OR event_id = ?",
                (tombstone["tombstone_id"], event["event_id"]),
            ).fetchone()
            if concurrent is not None:
                if (
                    concurrent["tombstone_id"] != tombstone["tombstone_id"]
                    or concurrent["event_id"] != event["event_id"]
                    or concurrent["tombstone_sha256"] != tombstone_hash
                    or concurrent["request_key_id"] != signature.key_id
                ):
                    raise CMSBridgeBlocked("cms.tombstone.idempotency_collision")
                request = self._tombstone_request_from_row(
                    concurrent, event_verifier, publication_authority,
                )
                return TombstoneAccepted(
                    tombstone["tombstone_id"], event["event_id"],
                    request.delivery_id, concurrent["status"], False,
                )
            live_publication = self.connection.execute(
                "SELECT status, payload_sha256 FROM cms_publication_deliveries WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if (
                live_publication is None
                or live_publication["status"] != "succeeded"
                or live_publication["payload_sha256"] != publication.payload_sha256
            ):
                raise CMSBridgeBlocked("cms.tombstone.not_published")
            self.connection.execute("""
                INSERT INTO cms_tombstone_deliveries (
                    tombstone_id, event_id, tombstone_sha256, tombstone_json,
                    request_signature_algorithm, request_key_id, request_signature,
                    delivery_id, plan_id, payload_json, payload_sha256,
                    signature_algorithm, key_id, signature, status, attempts,
                    max_attempts, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'pending', 0, ?, ?, ?, ?)
            """, (
                tombstone["tombstone_id"], event["event_id"], tombstone_hash,
                tombstone_json, signature.algorithm, signature.key_id,
                signature.signature, delivery_id, plan.plan_id, payload_json,
                payload_hash, delivery_signature.algorithm,
                delivery_signature.key_id, delivery_signature.signature,
                max_attempts, now, now, now,
            ))
        return TombstoneAccepted(
            tombstone["tombstone_id"], event["event_id"], delivery_id,
            "pending", True,
        )

    def ingest_change(
        self,
        event: Any,
        signature: CMSMessageSignature,
        verifier: CMSMessageAuthority,
        *,
        max_attempts: int = 3,
        now: float | int,
    ) -> IngestedChange:
        event, plan = self._validated_event(event, allow_legacy=True)
        signature = _signature(signature)
        now = _timestamp(now, "cms.time.invalid")
        event_json = _canonical_json(event)
        event_hash = _hash(event_json)
        _verify(verifier, event_json.encode("utf-8"), signature, "cms.event.signature_rejected")
        event_id = event["event_id"]
        site_id = event["site_id"]
        source_id = event["localization"]["source_id"]

        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT event_sha256, plan_id FROM cms_change_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                if event.get("schema") != CHANGE_SCHEMA:
                    raise CMSBridgeBlocked("cms.event.legacy_replay_only")
                generation = event["source_sequence"]
                collision = self.connection.execute("""
                    SELECT 1 FROM cms_event_topics
                    WHERE site_id = ? AND source_id = ? AND generation = ?
                """, (site_id, source_id, generation)).fetchone()
                if collision is not None:
                    raise CMSBridgeBlocked("cms.event.sequence_collision")
                self.connection.execute("""
                    INSERT INTO cms_change_events VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """, (
                    event_id, event_hash, event_json, signature.algorithm, signature.key_id,
                    signature.signature, plan.plan_id, now, now,
                ))
                self.connection.execute(
                    "INSERT INTO cms_event_topics VALUES (?, ?, ?, ?, ?)",
                    (event_id, site_id, source_id, generation, now),
                )
            elif row["event_sha256"] != event_hash or row["plan_id"] != plan.plan_id:
                raise CMSBridgeBlocked("cms.event.idempotency_collision")
            else:
                topic = self.connection.execute(
                    "SELECT site_id, source_id FROM cms_event_topics WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if topic is None or (topic["site_id"], topic["source_id"]) != (site_id, source_id):
                    raise CMSBridgeBlocked("cms.event.topic_invalid")

            cancelled = self.connection.execute(
                "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
                (event_id,),
            ).fetchone() is not None

        if cancelled:
            self._load_event(
                event_id,
                verifier,
                allow_superseded=True,
                allow_cancelled=True,
                allow_accepted=True,
            )
            return IngestedChange(event_id, plan.plan_id, len(plan.jobs), 0, "cancelled")

        try:
            inserted = self.queue.enqueue_plan(plan, max_attempts=max_attempts, now=now)
        except _QUEUE.LocalizationQueueBlocked:
            raise CMSBridgeBlocked("cms.queue.rejected") from None
        with _transaction(self.connection):
            cancelled = self.connection.execute(
                "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
                (event_id,),
            ).fetchone() is not None
            if cancelled:
                self._load_event(
                    event_id,
                    verifier,
                    allow_superseded=True,
                    allow_cancelled=True,
                    allow_accepted=True,
                )
            else:
                updated = self.connection.execute("""
                    UPDATE cms_change_events SET status = 'enqueued', updated_at = ?
                    WHERE event_id = ? AND event_sha256 = ? AND plan_id = ?
                """, (now, event_id, event_hash, plan.plan_id))
                if updated.rowcount != 1:
                    raise CMSBridgeBlocked("cms.event.identity_lost")
                newest = self.connection.execute("""
                    SELECT topic.event_id, topic.generation
                    FROM cms_event_topics AS topic
                    JOIN cms_change_events AS event ON event.event_id = topic.event_id
                    WHERE topic.site_id = ? AND topic.source_id = ?
                      AND event.status = 'enqueued'
                    ORDER BY topic.generation DESC LIMIT 1
                """, (site_id, source_id)).fetchone()
                if newest is None:
                    raise CMSBridgeBlocked("cms.event.topic_invalid")
                older = self.connection.execute("""
                    SELECT older.event_id
                    FROM cms_event_topics AS older
                    JOIN cms_change_events AS events ON events.event_id = older.event_id
                    LEFT JOIN cms_publication_deliveries AS delivery
                        ON delivery.event_id = older.event_id AND delivery.status = 'succeeded'
                    WHERE older.site_id = ? AND older.source_id = ?
                      AND older.generation < ? AND events.status = 'enqueued'
                      AND delivery.event_id IS NULL
                    ORDER BY older.generation, older.event_id
                """, (site_id, source_id, newest["generation"])).fetchall()
                for prior in older:
                    self.connection.execute("""
                        INSERT OR IGNORE INTO cms_event_supersessions
                        VALUES (?, ?, ?)
                    """, (prior["event_id"], newest["event_id"], now))
                self.connection.execute("""
                    UPDATE cms_publication_deliveries
                    SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error_code = 'event_superseded',
                        last_error_detail_hash = NULL, updated_at = ?
                    WHERE event_id IN (
                        SELECT supersession.event_id FROM cms_event_supersessions AS supersession
                        JOIN cms_event_topics AS topic ON topic.event_id = supersession.event_id
                        WHERE topic.site_id = ? AND topic.source_id = ?
                    ) AND status <> 'succeeded'
                """, (now, site_id, source_id))
                superseded = self.connection.execute(
                    "SELECT 1 FROM cms_event_supersessions WHERE event_id = ?",
                    (event_id,),
                ).fetchone() is not None
        if cancelled:
            return IngestedChange(
                event_id, plan.plan_id, len(plan.jobs), inserted, "cancelled",
            )
        return IngestedChange(
            event_id, plan.plan_id, len(plan.jobs), inserted,
            "superseded" if superseded else "enqueued",
        )

    def resume_accepted_change(
        self,
        event_id: Any,
        verifier: CMSMessageAuthority,
        *,
        max_attempts: int = 3,
        now: float | int,
    ) -> IngestedChange | None:
        """Resume one signed event persisted before its queue transaction."""
        event_id = _token(event_id, "cms.event_id.invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_change_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise CMSBridgeBlocked("cms.event.not_enqueued")
        if row["status"] != "accepted":
            return None

        event, _ = self._load_event(
            event_id,
            verifier,
            allow_cancelled=True,
            allow_accepted=True,
        )
        signature = _signature(CMSMessageSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        return self.ingest_change(
            event,
            signature,
            verifier,
            max_attempts=max_attempts,
            now=now,
        )

    def change_progress(
        self,
        event_id: Any,
        event_verifier: CMSMessageAuthority,
        *,
        site_id: Any,
        requester_key_id: Any,
        now: float | int,
    ) -> ChangeProgress:
        """Return content-free progress only to the event's exact site credential."""
        event_id = _token(event_id, "cms.event_id.invalid")
        site_id = _token(site_id, "cms.site_id.invalid")
        requester_key_id = _token(
            requester_key_id, "cms.status.requester_key_id.invalid",
        )
        now = _timestamp(now, "cms.time.invalid")
        topic = self.connection.execute(
            "SELECT site_id, generation FROM cms_event_topics WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_row = self.connection.execute(
            "SELECT key_id, status FROM cms_change_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if (
            topic is None
            or event_row is None
            or topic["site_id"] != site_id
            or event_row["key_id"] != requester_key_id
        ):
            raise CMSBridgeBlocked("cms.status.scope_rejected")

        cancelled = self.connection.execute(
            "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
            (event_id,),
        ).fetchone() is not None
        queue_recovery_pending = (
            event_row["status"] == "accepted" and not cancelled
        )
        event, plan = self._load_event(
            event_id,
            event_verifier,
            allow_cancelled=cancelled,
            allow_accepted=cancelled or queue_recovery_pending,
        )
        if event["site_id"] != site_id:
            raise CMSBridgeBlocked("cms.status.scope_rejected")
        if cancelled and event_row["status"] == "accepted":
            counts = {status: 0 for status in _QUEUE._STATUSES}
            counts["cancelled"] = len(plan.jobs)
            locales = tuple(LocaleProgress(
                job_id=job.job_id,
                target_locale=job.target.locale,
                status="cancelled",
                attempts=0,
                max_attempts=0,
                next_attempt_at=0.0,
                lease_expires_at=None,
                lease_expired=False,
                last_error_code="event_cancelled",
                last_error_detail_hash=None,
                result_sha256=None,
            ) for job in sorted(plan.jobs, key=lambda item: item.target.locale))
            return ChangeProgress(
                event_id=event["event_id"],
                site_id=event["site_id"],
                plan_id=plan.plan_id,
                website_version=event["website_version"],
                source_sequence=int(topic["generation"]),
                job_count=len(plan.jobs),
                counts=counts,
                locales=locales,
                cancelled=True,
                queue_recovery_pending=False,
            )
        if queue_recovery_pending:
            counts = {status: 0 for status in _QUEUE._STATUSES}
            locales = tuple(LocaleProgress(
                job_id=job.job_id,
                target_locale=job.target.locale,
                status="awaiting_queue_resume",
                attempts=0,
                max_attempts=0,
                next_attempt_at=0.0,
                lease_expires_at=None,
                lease_expired=False,
                last_error_code="cms.event.awaiting_queue_resume",
                last_error_detail_hash=None,
                result_sha256=None,
            ) for job in sorted(plan.jobs, key=lambda item: item.target.locale))
            return ChangeProgress(
                event_id=event["event_id"],
                site_id=event["site_id"],
                plan_id=plan.plan_id,
                website_version=event["website_version"],
                source_sequence=int(topic["generation"]),
                job_count=len(plan.jobs),
                counts=counts,
                locales=locales,
                cancelled=False,
                queue_recovery_pending=True,
            )
        locales = []
        try:
            for job in plan.jobs:
                status = self.queue.status(job.job_id)
                if (
                    status.target_locale != job.target.locale
                    or plan.plan_id not in status.plan_ids
                ):
                    raise CMSBridgeBlocked("cms.queue.identity_lost")
                if status.status == "succeeded":
                    self.queue.result(job.job_id)
                locales.append(LocaleProgress(
                    job_id=status.job_id,
                    target_locale=status.target_locale,
                    status=status.status,
                    attempts=status.attempts,
                    max_attempts=status.max_attempts,
                    next_attempt_at=status.next_attempt_at,
                    lease_expires_at=status.lease_expires_at,
                    lease_expired=(
                        status.status == "leased"
                        and status.lease_expires_at is not None
                        and status.lease_expires_at <= now
                    ),
                    last_error_code=status.last_error_code,
                    last_error_detail_hash=status.last_error_detail_hash,
                    result_sha256=status.result_sha256,
                ))
            counts = self.queue.plan_counts(plan.plan_id)
        except _QUEUE.LocalizationQueueBlocked:
            raise CMSBridgeBlocked("cms.queue.integrity_failed") from None
        if sum(counts.values()) != len(plan.jobs) or len(locales) != len(plan.jobs):
            raise CMSBridgeBlocked("cms.queue.identity_lost")
        return ChangeProgress(
            event_id=event["event_id"],
            site_id=event["site_id"],
            plan_id=plan.plan_id,
            website_version=event["website_version"],
            source_sequence=int(topic["generation"]),
            job_count=len(plan.jobs),
            counts=counts,
            locales=tuple(sorted(locales, key=lambda item: item.target_locale)),
            cancelled=cancelled,
            queue_recovery_pending=False,
        )

    def change_lifecycle(
        self,
        event_id: Any,
        event_verifier: CMSMessageAuthority,
        approval_authority: Any,
        publication_authority: CMSMessageAuthority,
        *,
        site_id: Any,
        requester_key_id: Any,
        now: float | int,
    ) -> ChangeLifecycle:
        """Return the verified end-to-end state for one tenant-scoped event."""
        now = _timestamp(now, "cms.time.invalid")
        progress = self.change_progress(
            event_id,
            event_verifier,
            site_id=site_id,
            requester_key_id=requester_key_id,
            now=now,
        )
        event, plan = self._load_event(
            progress.event_id,
            event_verifier,
            allow_cancelled=progress.cancelled,
            allow_accepted=(
                progress.cancelled or progress.queue_recovery_pending
            ),
        )
        if progress.queue_recovery_pending:
            required_locales = tuple(sorted(
                job.target.locale for job in plan.jobs
            ))
            return ChangeLifecycle(
                event_id=progress.event_id,
                site_id=progress.site_id,
                plan_id=progress.plan_id,
                website_version=progress.website_version,
                source_sequence=progress.source_sequence,
                status="queue_recovery",
                required_locales=required_locales,
                approved_locales=(),
                blocked_locales=tuple(
                    (locale, "queue.awaiting_resume")
                    for locale in required_locales
                ),
                queue_counts=progress.counts,
                delivery=None,
                tombstone=None,
            )
        try:
            readiness = self.release_store.readiness(
                plan, approval_authority, now=now,
            )
        except _RELEASE.LocalizationReleaseBlocked:
            raise CMSBridgeBlocked("cms.release.integrity_failed") from None
        expected_release_blocks = {"approval.missing", "approval.expired"}
        if any(
            code not in expected_release_blocks
            for _, code in readiness.blocked
        ):
            raise CMSBridgeBlocked("cms.release.integrity_failed")

        row = self.connection.execute(
            "SELECT * FROM cms_publication_deliveries WHERE event_id = ?",
            (progress.event_id,),
        ).fetchone()
        delivery = None
        if row is not None:
            request = self._request_from_row(
                row, publication_authority, now, require_current_approvals=False,
            )
            payload = request.payload
            expected_payload_keys = {
                "schema", "delivery_id", "event_id", "site_id",
                "website_version", "plan_id", "source_id", "source_revision",
                "source_sequence", "source_sha256", "localizations",
            }
            expected_localization_keys = {
                "locale", "target_text", "target_sha256", "approval_id",
                "approval_expires_at", "release_evidence",
            }
            localizations = payload.get("localizations")
            if (
                set(payload) != expected_payload_keys
                or payload.get("schema") != PUBLICATION_SCHEMA
                or payload.get("delivery_id") != row["delivery_id"]
                or payload.get("event_id") != event["event_id"]
                or payload.get("site_id") != event["site_id"]
                or payload.get("website_version") != event["website_version"]
                or payload.get("plan_id") != plan.plan_id
                or payload.get("source_id") != event["localization"]["source_id"]
                or payload.get("source_revision")
                != event["localization"]["source_revision"]
                or payload.get("source_sequence") != progress.source_sequence
                or payload.get("source_sha256") != plan.source_hash
                or not isinstance(localizations, list)
                or any(
                    not isinstance(item, dict)
                    or set(item) != expected_localization_keys
                    for item in localizations
                )
                or tuple(sorted(item["locale"] for item in localizations))
                != readiness.required_locales
                or any(
                    _token(item["locale"], "cms.delivery.tampered")
                    != item["locale"]
                    or not isinstance(item["target_text"], str)
                    or not item["target_text"]
                    or not unicodedata.is_normalized("NFC", item["target_text"])
                    or SHA256.fullmatch(str(item["target_sha256"])) is None
                    or _token(item["approval_id"], "cms.delivery.tampered")
                    != item["approval_id"]
                    or _timestamp(
                        item["approval_expires_at"], "cms.delivery.tampered",
                    ) <= 0
                    or not _valid_release_evidence(
                        item["release_evidence"],
                        locale=item["locale"],
                        target_sha256=item["target_sha256"],
                        approval_id=item["approval_id"],
                    )
                    for item in localizations
                )
            ):
                raise CMSBridgeBlocked("cms.delivery.tampered")
            delivery_status = self.delivery_status(request.delivery_id)
            try:
                next_attempt_at = _timestamp(
                    delivery_status.next_attempt_at, "cms.delivery.tampered",
                )
                lease_expires_at = (
                    None
                    if delivery_status.lease_expires_at is None
                    else _timestamp(
                        delivery_status.lease_expires_at,
                        "cms.delivery.tampered",
                    )
                )
            except CMSBridgeBlocked:
                raise
            if (
                delivery_status.event_id != progress.event_id
                or delivery_status.plan_id != progress.plan_id
                or delivery_status.payload_sha256 != request.payload_sha256
                or delivery_status.status
                not in {"pending", "leased", "retry_wait", "failed", "succeeded"}
                or isinstance(delivery_status.attempts, bool)
                or not isinstance(delivery_status.attempts, int)
                or isinstance(delivery_status.max_attempts, bool)
                or not isinstance(delivery_status.max_attempts, int)
                or not 0 <= delivery_status.attempts <= delivery_status.max_attempts
                or not 1 <= delivery_status.max_attempts <= MAX_ATTEMPTS
                or (
                    delivery_status.last_error_code is not None
                    and (
                        not isinstance(delivery_status.last_error_code, str)
                        or ERROR_CODE.fullmatch(delivery_status.last_error_code) is None
                    )
                )
                or (
                    delivery_status.last_error_detail_hash is not None
                    and SHA256.fullmatch(
                        str(delivery_status.last_error_detail_hash),
                    ) is None
                )
                or (delivery_status.status == "leased") != (lease_expires_at is not None)
                or (
                    delivery_status.status in {"pending", "leased", "succeeded"}
                    and delivery_status.last_error_code is not None
                )
                or (
                    delivery_status.status in {"retry_wait", "failed"}
                    and delivery_status.last_error_code is None
                )
            ):
                raise CMSBridgeBlocked("cms.delivery.tampered")
            delivery = {
                "delivery_id": delivery_status.delivery_id,
                "status": delivery_status.status,
                "attempts": delivery_status.attempts,
                "max_attempts": delivery_status.max_attempts,
                "next_attempt_at": next_attempt_at,
                "lease_expires_at": lease_expires_at,
                "lease_expired": (
                    delivery_status.status == "leased"
                    and lease_expires_at is not None
                    and lease_expires_at <= now
                ),
                "last_error_code": delivery_status.last_error_code,
                "last_error_detail_hash": delivery_status.last_error_detail_hash,
            }

        tombstone_row = self.connection.execute(
            "SELECT * FROM cms_tombstone_deliveries WHERE event_id = ?",
            (progress.event_id,),
        ).fetchone()
        tombstone = None
        if tombstone_row is not None:
            tombstone_request = self._tombstone_request_from_row(
                tombstone_row, event_verifier, publication_authority,
            )
            tombstone_status = self.tombstone_status(tombstone_request.delivery_id)
            tombstone = {
                "tombstone_id": tombstone_row["tombstone_id"],
                "delivery_id": tombstone_status.delivery_id,
                "status": tombstone_status.status,
                "attempts": tombstone_status.attempts,
                "max_attempts": tombstone_status.max_attempts,
                "next_attempt_at": tombstone_status.next_attempt_at,
                "lease_expires_at": tombstone_status.lease_expires_at,
                "lease_expired": (
                    tombstone_status.status == "leased"
                    and tombstone_status.lease_expires_at is not None
                    and tombstone_status.lease_expires_at <= now
                ),
                "last_error_code": tombstone_status.last_error_code,
                "last_error_detail_hash": tombstone_status.last_error_detail_hash,
            }

        if progress.cancelled:
            status = "cancelled"
        elif tombstone is not None and tombstone["status"] == "succeeded":
            status = "deleted"
        elif tombstone is not None and tombstone["status"] == "failed":
            status = "deletion_failed"
        elif tombstone is not None:
            status = "deleting"
        elif delivery is not None and delivery["status"] == "succeeded":
            status = "published"
        elif delivery is not None and delivery["status"] == "failed":
            status = "publication_failed"
        elif delivery is not None and any(
            code == "approval.expired" for _, code in readiness.blocked
        ):
            status = "publication_blocked"
        elif delivery is not None:
            status = "publishing"
        elif readiness.ready:
            status = "ready"
        elif progress.counts["failed"]:
            status = "localization_failed"
        elif progress.counts["succeeded"] == progress.job_count:
            status = "awaiting_approval"
        else:
            status = "processing"

        return ChangeLifecycle(
            event_id=progress.event_id,
            site_id=progress.site_id,
            plan_id=progress.plan_id,
            website_version=progress.website_version,
            source_sequence=progress.source_sequence,
            status=status,
            required_locales=readiness.required_locales,
            approved_locales=readiness.approved_locales,
            blocked_locales=readiness.blocked,
            queue_counts=progress.counts,
            delivery=delivery,
            tombstone=tombstone,
        )

    def _load_event(
        self,
        event_id: Any,
        verifier: CMSMessageAuthority,
        *,
        allow_superseded: bool = False,
        allow_cancelled: bool = False,
        allow_accepted: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        event_id = _token(event_id, "cms.event_id.invalid")
        if not isinstance(allow_superseded, bool):
            raise CMSBridgeBlocked("cms.event.supersession_mode_invalid")
        if not isinstance(allow_cancelled, bool):
            raise CMSBridgeBlocked("cms.event.cancellation_mode_invalid")
        if not isinstance(allow_accepted, bool):
            raise CMSBridgeBlocked("cms.event.status_mode_invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_change_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise CMSBridgeBlocked("cms.event.not_enqueued")
        supersession = self.connection.execute(
            "SELECT 1 FROM cms_event_supersessions WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if supersession is not None and not allow_superseded:
            raise CMSBridgeBlocked("cms.event.superseded")
        if _hash(row["event_json"]) != row["event_sha256"]:
            raise CMSBridgeBlocked("cms.event.tampered")
        try:
            event = json.loads(row["event_json"])
        except json.JSONDecodeError:
            raise CMSBridgeBlocked("cms.event.tampered") from None
        if _canonical_json(event) != row["event_json"]:
            raise CMSBridgeBlocked("cms.event.tampered")
        signature = _signature(CMSMessageSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        _verify(verifier, row["event_json"].encode("utf-8"), signature, "cms.event.signature_rejected")
        event, plan = self._validated_event(event, allow_legacy=True)
        if plan.plan_id != row["plan_id"]:
            raise CMSBridgeBlocked("cms.event.plan_mismatch")
        topic = self.connection.execute("""
            SELECT site_id, source_id, generation FROM cms_event_topics
            WHERE event_id = ?
        """, (event_id,)).fetchone()
        if topic is None or (
            topic["site_id"], topic["source_id"]
        ) != (event["site_id"], event["localization"]["source_id"]):
            raise CMSBridgeBlocked("cms.event.topic_invalid")
        cancellation = self.connection.execute(
            "SELECT * FROM cms_event_cancellations WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if cancellation is not None:
            self._verify_stored_cancellation(
                cancellation, event, topic, row["key_id"], verifier,
            )
            if not allow_cancelled:
                raise CMSBridgeBlocked("cms.event.cancelled")
        if (
            row["status"] != "enqueued"
            and not (allow_accepted and row["status"] == "accepted")
        ):
            raise CMSBridgeBlocked("cms.event.not_enqueued")
        newer = self.connection.execute("""
            SELECT 1
            FROM cms_event_topics AS candidate
            JOIN cms_change_events AS candidate_event
                ON candidate_event.event_id = candidate.event_id
            WHERE candidate.site_id = ? AND candidate.source_id = ?
              AND candidate.generation > ? AND candidate_event.status = 'enqueued'
            LIMIT 1
        """, (topic["site_id"], topic["source_id"], topic["generation"])).fetchone()
        if newer is not None and not allow_superseded:
            raise CMSBridgeBlocked("cms.event.superseded")
        return event, plan

    def _event_is_current(self, event_id: str) -> bool:
        row = self.connection.execute("""
            SELECT topic.site_id, topic.source_id, topic.generation
            FROM cms_event_topics AS topic
            JOIN cms_change_events AS event ON event.event_id = topic.event_id
            WHERE topic.event_id = ? AND event.status = 'enqueued'
              AND topic.event_id NOT IN (SELECT event_id FROM cms_event_supersessions)
              AND topic.event_id NOT IN (SELECT event_id FROM cms_event_cancellations)
        """, (event_id,)).fetchone()
        if row is None:
            return False
        return self.connection.execute("""
            SELECT 1
            FROM cms_event_topics AS newer
            JOIN cms_change_events AS event ON event.event_id = newer.event_id
            WHERE newer.site_id = ? AND newer.source_id = ?
              AND newer.generation > ? AND event.status = 'enqueued'
            LIMIT 1
        """, (row["site_id"], row["source_id"], row["generation"])).fetchone() is None

    def prepare_delivery(
        self,
        event_id: Any,
        event_verifier: CMSMessageAuthority,
        approval_authority: Any,
        publication_authority: CMSMessageAuthority,
        *,
        now: float | int,
        max_attempts: int = 5,
    ) -> CMSPublicationRequest:
        now = _timestamp(now, "cms.time.invalid")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise CMSBridgeBlocked("cms.max_attempts.invalid")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise CMSBridgeBlocked("cms.max_attempts.invalid")
        event, plan = self._load_event(event_id, event_verifier)
        try:
            bundle = self.release_store.publication_bundle(
                plan, approval_authority, now=now,
            )
        except _RELEASE.LocalizationReleaseBlocked:
            raise CMSBridgeBlocked("cms.website.not_ready") from None
        required = tuple(sorted(job.target.locale for job in plan.jobs))
        actual = tuple(item.target_locale for item in bundle)
        if actual != required:
            raise CMSBridgeBlocked("cms.bundle.incomplete")
        source_sequence = event.get("source_sequence")
        if source_sequence is None:
            topic = self.connection.execute(
                "SELECT generation FROM cms_event_topics WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if topic is None:
                raise CMSBridgeBlocked("cms.event.topic_invalid")
            source_sequence = topic["generation"]
        unsigned = {
            "schema": PUBLICATION_SCHEMA,
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "website_version": event["website_version"],
            "plan_id": plan.plan_id,
            "source_id": event["localization"]["source_id"],
            "source_revision": event["localization"]["source_revision"],
            "source_sequence": source_sequence,
            "source_sha256": plan.source_hash,
            "localizations": [
                {
                    "locale": item.target_locale,
                    "target_text": item.candidate,
                    "target_sha256": item.target_sha256,
                    "approval_id": item.approval_id,
                    "approval_expires_at": item.expires_at,
                    "release_evidence": item.release_evidence,
                }
                for item in bundle
            ],
        }
        delivery_id = "blun-cms-delivery-" + _hash(_canonical_json(unsigned))
        payload = {**unsigned, "delivery_id": delivery_id}
        payload_json = _canonical_json(payload)
        payload_hash = _hash(payload_json)
        sign = getattr(publication_authority, "sign", None)
        try:
            signature = _signature(sign(payload_json.encode("utf-8"))) if callable(sign) else None
        except CMSBridgeBlocked:
            raise
        except Exception:
            raise CMSBridgeBlocked("cms.publication.signing_failed") from None
        if signature is None:
            raise CMSBridgeBlocked("cms.publication.authority_invalid")
        _verify(
            publication_authority,
            payload_json.encode("utf-8"),
            signature,
            "cms.publication.signature_rejected",
        )
        with _transaction(self.connection):
            if self.connection.execute(
                "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone() is not None:
                raise CMSBridgeBlocked("cms.event.cancelled")
            prior = self.connection.execute(
                "SELECT delivery_id, payload_sha256 FROM cms_publication_deliveries WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if prior is not None:
                if prior["delivery_id"] != delivery_id or prior["payload_sha256"] != payload_hash:
                    raise CMSBridgeBlocked("cms.delivery.idempotency_collision")
            else:
                self.connection.execute("""
                    INSERT INTO cms_publication_deliveries (
                        delivery_id, event_id, plan_id, payload_json, payload_sha256,
                        signature_algorithm, key_id, signature, status, attempts,
                        max_attempts, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (
                    delivery_id, event["event_id"], plan.plan_id, payload_json, payload_hash,
                    signature.algorithm, signature.key_id, signature.signature,
                    max_attempts, now, now, now,
                ))
        return CMSPublicationRequest(delivery_id, payload, payload_hash, signature)

    def _request_from_row(
        self,
        row: sqlite3.Row,
        authority: CMSMessageAuthority,
        now: float,
        *,
        require_current_approvals: bool = True,
    ) -> CMSPublicationRequest:
        if not isinstance(require_current_approvals, bool):
            raise CMSBridgeBlocked("cms.delivery.validation_mode_invalid")
        if _hash(row["payload_json"]) != row["payload_sha256"]:
            raise CMSBridgeBlocked("cms.delivery.tampered")
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            raise CMSBridgeBlocked("cms.delivery.tampered") from None
        if _canonical_json(payload) != row["payload_json"] or payload.get("delivery_id") != row["delivery_id"]:
            raise CMSBridgeBlocked("cms.delivery.tampered")
        localizations = payload.get("localizations")
        if not isinstance(localizations, list) or not localizations:
            raise CMSBridgeBlocked("cms.delivery.tampered")
        expiries = [item.get("approval_expires_at") for item in localizations if isinstance(item, dict)]
        if len(expiries) != len(localizations):
            raise CMSBridgeBlocked("cms.delivery.tampered")
        validated_expiries = tuple(
            _timestamp(expiry, "cms.delivery.tampered") for expiry in expiries
        )
        try:
            for item in localizations:
                if not isinstance(item, dict):
                    raise _RELEASE.LocalizationReleaseBlocked(
                        "publication.evidence.invalid"
                    )
                if not _valid_release_evidence(
                    item.get("release_evidence"),
                    locale=item.get("locale"),
                    target_sha256=item.get("target_sha256"),
                    approval_id=item.get("approval_id"),
                ):
                    raise _RELEASE.LocalizationReleaseBlocked(
                        "publication.evidence.invalid"
                    )
        except _RELEASE.LocalizationReleaseBlocked:
            raise CMSBridgeBlocked("cms.delivery.tampered") from None
        if require_current_approvals and any(
            expiry <= now for expiry in validated_expiries
        ):
            raise CMSBridgeBlocked("cms.delivery.approval_expired")
        signature = _signature(CMSMessageSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        _verify(
            authority,
            row["payload_json"].encode("utf-8"),
            signature,
            "cms.delivery.signature_invalid",
        )
        return CMSPublicationRequest(row["delivery_id"], payload, row["payload_sha256"], signature)

    def _tombstone_request_from_row(
        self,
        row: sqlite3.Row,
        event_verifier: CMSMessageAuthority,
        publication_authority: CMSMessageAuthority,
    ) -> CMSTombstoneRequest:
        if (
            _hash(row["tombstone_json"]) != row["tombstone_sha256"]
            or _hash(row["payload_json"]) != row["payload_sha256"]
        ):
            raise CMSBridgeBlocked("cms.tombstone.tampered")
        try:
            tombstone = json.loads(row["tombstone_json"])
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            raise CMSBridgeBlocked("cms.tombstone.tampered") from None
        if (
            _canonical_json(tombstone) != row["tombstone_json"]
            or _canonical_json(payload) != row["payload_json"]
        ):
            raise CMSBridgeBlocked("cms.tombstone.tampered")
        tombstone = self._validated_tombstone(tombstone)
        event, plan = self._load_event(
            row["event_id"], event_verifier, allow_superseded=True,
        )
        topic = self.connection.execute(
            "SELECT generation FROM cms_event_topics WHERE event_id = ?",
            (row["event_id"],),
        ).fetchone()
        event_row = self.connection.execute(
            "SELECT key_id FROM cms_change_events WHERE event_id = ?",
            (row["event_id"],),
        ).fetchone()
        if topic is None or event_row is None:
            raise CMSBridgeBlocked("cms.tombstone.tampered")
        request_signature = _signature(CMSMessageSignature(
            row["request_signature_algorithm"], row["request_key_id"],
            row["request_signature"],
        ))
        _verify(
            event_verifier, row["tombstone_json"].encode("utf-8"),
            request_signature, "cms.tombstone.signature_rejected",
        )
        request_binding = {
            "tombstone_id": row["tombstone_id"],
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "website_version": event["website_version"],
            "source_id": event["localization"]["source_id"],
            "source_sequence": int(topic["generation"]),
        }
        if (
            any(tombstone[field] != value for field, value in request_binding.items())
            or row["request_key_id"] != event_row["key_id"]
        ):
            raise CMSBridgeBlocked("cms.tombstone.binding_invalid")
        publication_row = self.connection.execute(
            "SELECT * FROM cms_publication_deliveries WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if publication_row is None or publication_row["status"] != "succeeded":
            raise CMSBridgeBlocked("cms.tombstone.publication_invalid")
        publication = self._request_from_row(
            publication_row, publication_authority, 0,
            require_current_approvals=False,
        )
        expected_keys = {
            "schema", "delivery_id", "tombstone_id", "event_id", "site_id",
            "website_version", "plan_id", "source_id", "source_sequence",
            "publication_delivery_id", "publication_payload_sha256", "locales",
        }
        locales = sorted(
            item["locale"] for item in publication.payload["localizations"]
        )
        expected_payload = {
            "schema": TOMBSTONE_DELIVERY_SCHEMA,
            "delivery_id": row["delivery_id"],
            "tombstone_id": row["tombstone_id"],
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "website_version": event["website_version"],
            "plan_id": plan.plan_id,
            "source_id": event["localization"]["source_id"],
            "source_sequence": int(topic["generation"]),
            "publication_delivery_id": publication.delivery_id,
            "publication_payload_sha256": publication.payload_sha256,
            "locales": locales,
        }
        unsigned = {key: value for key, value in payload.items() if key != "delivery_id"}
        if (
            set(payload) != expected_keys
            or payload != expected_payload
            or locales != sorted(set(locales))
            or payload["delivery_id"]
            != "blun-cms-tombstone-" + _hash(_canonical_json(unsigned))
            or row["plan_id"] != plan.plan_id
        ):
            raise CMSBridgeBlocked("cms.tombstone.tampered")
        delivery_signature = _signature(CMSMessageSignature(
            row["signature_algorithm"], row["key_id"], row["signature"],
        ))
        _verify(
            publication_authority, row["payload_json"].encode("utf-8"),
            delivery_signature, "cms.tombstone.delivery_signature_invalid",
        )
        return CMSTombstoneRequest(
            row["delivery_id"], payload, row["payload_sha256"], delivery_signature,
        )

    def claim_tombstone(
        self,
        worker_id: Any,
        event_verifier: CMSMessageAuthority,
        publication_authority: CMSMessageAuthority,
        *,
        now: float | int,
        lease_seconds: float | int = 300,
    ) -> ClaimedTombstone | None:
        worker_id = _token(worker_id, "cms.worker_id.invalid")
        now = _timestamp(now, "cms.time.invalid")
        lease_seconds = _duration(lease_seconds, "cms.lease.invalid")
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_tombstone_deliveries
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ? AND attempts >= max_attempts
            """, (now, now))
            self.connection.execute("""
                UPDATE cms_tombstone_deliveries
                SET status = 'retry_wait', next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ? AND attempts < max_attempts
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_tombstone_deliveries
                WHERE status IN ('pending', 'retry_wait') AND next_attempt_at <= ?
                  AND attempts < max_attempts
                ORDER BY created_at, delivery_id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            request = self._tombstone_request_from_row(
                row, event_verifier, publication_authority,
            )
            lease_token = secrets.token_urlsafe(32)
            expires = now + lease_seconds
            updated = self.connection.execute("""
                UPDATE cms_tombstone_deliveries
                SET status = 'leased', attempts = attempts + 1, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?, last_error_code = NULL,
                    last_error_detail_hash = NULL, updated_at = ?
                WHERE delivery_id = ? AND status IN ('pending', 'retry_wait')
            """, (worker_id, lease_token, expires, now, row["delivery_id"]))
            if updated.rowcount != 1:
                raise CMSBridgeBlocked("cms.tombstone.claim_lost")
            return ClaimedTombstone(
                request, int(row["attempts"]) + 1, int(row["max_attempts"]),
                worker_id, lease_token, expires,
            )

    def _live_tombstone(
        self,
        claim: Any,
        event_verifier: CMSMessageAuthority,
        publication_authority: CMSMessageAuthority,
        now: float,
    ) -> sqlite3.Row:
        if not isinstance(claim, ClaimedTombstone):
            raise CMSBridgeBlocked("cms.tombstone.claim_invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_tombstone_deliveries WHERE delivery_id = ?",
            (claim.request.delivery_id,),
        ).fetchone()
        if row is None or row["status"] != "leased":
            raise CMSBridgeBlocked("cms.tombstone.lease_lost")
        if row["lease_owner"] != claim.lease_owner or row["lease_token"] != claim.lease_token:
            raise CMSBridgeBlocked("cms.tombstone.lease_lost")
        if float(row["lease_expires_at"]) <= now:
            raise CMSBridgeBlocked("cms.tombstone.lease_expired")
        request = self._tombstone_request_from_row(
            row, event_verifier, publication_authority,
        )
        if request != claim.request:
            raise CMSBridgeBlocked("cms.tombstone.claim_mutated")
        return row

    def _finish_tombstone(
        self,
        claim: ClaimedTombstone,
        event_verifier: CMSMessageAuthority,
        publication_authority: CMSMessageAuthority,
        *,
        now: float,
        error: CMSPublishFailed | None,
    ) -> DeliveryStatus:
        with _transaction(self.connection):
            row = self._live_tombstone(
                claim, event_verifier, publication_authority, now,
            )
            if error is None:
                status, next_attempt, code, detail_hash = "succeeded", now, None, None
            else:
                terminal = not error.retryable or int(row["attempts"]) >= int(row["max_attempts"])
                status = "failed" if terminal else "retry_wait"
                next_attempt = now if terminal else now + min(
                    3600.0, 5.0 * (2 ** (int(row["attempts"]) - 1)),
                )
                code = error.code
                detail_hash = _hash(error.detail) if error.detail is not None else None
            updated = self.connection.execute("""
                UPDATE cms_tombstone_deliveries
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, last_error_detail_hash = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                status, next_attempt, code, detail_hash, now,
                claim.request.delivery_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise CMSBridgeBlocked("cms.tombstone.finish_lost")
        return self.tombstone_status(claim.request.delivery_id)

    def run_tombstone(
        self,
        publisher: CMSPublisher,
        event_verifier: CMSMessageAuthority,
        publication_authority: CMSMessageAuthority,
        *,
        worker_id: Any,
        clock: Callable[[], float] = time.time,
        lease_seconds: float | int = 300,
        operation_guard: Callable[[float], Any] | None = None,
    ) -> DeliveryOutcome:
        if operation_guard is not None and not callable(operation_guard):
            raise CMSBridgeBlocked("cms.tombstone.operation_guard_invalid")
        claim = self.claim_tombstone(
            worker_id, event_verifier, publication_authority,
            now=clock(), lease_seconds=lease_seconds,
        )
        if claim is None:
            return DeliveryOutcome("idle")
        if operation_guard is not None:
            try:
                operation_guard(float(lease_seconds))
            except Exception:
                raise CMSBridgeBlocked("cms.tombstone.operation_guard_failed") from None
        publish = getattr(publisher, "publish", None)
        try:
            if not callable(publish):
                raise CMSPublishFailed("publisher.invalid", retryable=False)
            acknowledgement = publish(claim.request)
            expected = {
                "schema": TOMBSTONE_ACK_SCHEMA,
                "delivery_id": claim.request.delivery_id,
                "payload_sha256": claim.request.payload_sha256,
                "status": "deleted",
            }
            if not isinstance(acknowledgement, Mapping) or dict(acknowledgement) != expected:
                raise CMSPublishFailed("publisher.ack_invalid", retryable=True)
        except Exception as original_error:
            error = _declared_publish_failure(original_error)
            if error is None:
                error = CMSPublishFailed("publisher.unavailable", retryable=True)
            status = self._finish_tombstone(
                claim, event_verifier, publication_authority,
                now=_timestamp(clock(), "cms.time.invalid"), error=error,
            )
            return DeliveryOutcome(status.status, status.delivery_id, status.attempts, error.code)
        status = self._finish_tombstone(
            claim, event_verifier, publication_authority,
            now=_timestamp(clock(), "cms.time.invalid"), error=None,
        )
        return DeliveryOutcome(status.status, status.delivery_id, status.attempts)

    def tombstone_status(self, delivery_id: Any) -> DeliveryStatus:
        delivery_id = _token(delivery_id, "cms.delivery_id.invalid")
        row = self.connection.execute("""
            SELECT delivery_id, event_id, plan_id, status, attempts, max_attempts,
                   next_attempt_at, lease_expires_at, last_error_code,
                   last_error_detail_hash, payload_sha256
            FROM cms_tombstone_deliveries WHERE delivery_id = ?
        """, (delivery_id,)).fetchone()
        if row is None:
            raise CMSBridgeBlocked("cms.tombstone.missing")
        return DeliveryStatus(**dict(row))

    def claim_delivery(
        self,
        worker_id: Any,
        authority: CMSMessageAuthority,
        *,
        now: float | int,
        lease_seconds: float | int = 300,
    ) -> ClaimedDelivery | None:
        worker_id = _token(worker_id, "cms.worker_id.invalid")
        now = _timestamp(now, "cms.time.invalid")
        lease_seconds = _duration(lease_seconds, "cms.lease.invalid")
        with _transaction(self.connection):
            self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ? AND attempts >= max_attempts
            """, (now, now))
            self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = 'retry_wait', next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = 'lease_expired', updated_at = ?
                WHERE status = 'leased' AND lease_expires_at <= ? AND attempts < max_attempts
            """, (now, now, now))
            row = self.connection.execute("""
                SELECT * FROM cms_publication_deliveries
                WHERE status IN ('pending', 'retry_wait') AND next_attempt_at <= ?
                  AND attempts < max_attempts
                  AND event_id NOT IN (SELECT event_id FROM cms_event_supersessions)
                  AND event_id NOT IN (SELECT event_id FROM cms_event_cancellations)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cms_event_topics AS current
                      JOIN cms_event_topics AS newer
                        ON newer.site_id = current.site_id
                       AND newer.source_id = current.source_id
                       AND newer.generation > current.generation
                      JOIN cms_change_events AS newer_event
                        ON newer_event.event_id = newer.event_id
                      WHERE current.event_id = cms_publication_deliveries.event_id
                        AND newer_event.status = 'enqueued'
                  )
                ORDER BY created_at, delivery_id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            request = self._request_from_row(row, authority, now)
            lease_token = secrets.token_urlsafe(32)
            expires = now + lease_seconds
            updated = self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = 'leased', attempts = attempts + 1, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?, last_error_code = NULL,
                    last_error_detail_hash = NULL, updated_at = ?
                WHERE delivery_id = ? AND status IN ('pending', 'retry_wait')
            """, (worker_id, lease_token, expires, now, row["delivery_id"]))
            if updated.rowcount != 1:
                raise CMSBridgeBlocked("cms.delivery.claim_lost")
            return ClaimedDelivery(
                request, int(row["attempts"]) + 1, int(row["max_attempts"]),
                worker_id, lease_token, expires,
            )

    def _live_delivery(self, claim: Any, now: float) -> sqlite3.Row:
        if not isinstance(claim, ClaimedDelivery):
            raise CMSBridgeBlocked("cms.delivery.claim_invalid")
        row = self.connection.execute(
            "SELECT * FROM cms_publication_deliveries WHERE delivery_id = ?",
            (claim.request.delivery_id,),
        ).fetchone()
        if row is None:
            raise CMSBridgeBlocked("cms.delivery.lease_lost")
        if not self._event_is_current(row["event_id"]):
            cancelled = self.connection.execute(
                "SELECT 1 FROM cms_event_cancellations WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone() is not None
            if cancelled:
                raise CMSBridgeBlocked("cms.delivery.event_cancelled")
            raise CMSBridgeBlocked("cms.delivery.event_superseded")
        if row["status"] != "leased":
            raise CMSBridgeBlocked("cms.delivery.lease_lost")
        if row["lease_owner"] != claim.lease_owner or row["lease_token"] != claim.lease_token:
            raise CMSBridgeBlocked("cms.delivery.lease_lost")
        if float(row["lease_expires_at"]) <= now:
            raise CMSBridgeBlocked("cms.delivery.lease_expired")
        return row

    def _finish(self, claim: ClaimedDelivery, *, now: float, error: CMSPublishFailed | None) -> DeliveryStatus:
        with _transaction(self.connection):
            row = self._live_delivery(claim, now)
            if error is None:
                status = "succeeded"
                next_attempt = now
                code = detail_hash = None
            else:
                terminal = not error.retryable or int(row["attempts"]) >= int(row["max_attempts"])
                status = "failed" if terminal else "retry_wait"
                next_attempt = now if terminal else now + min(3600.0, 5.0 * (2 ** (int(row["attempts"]) - 1)))
                code = error.code
                detail_hash = _hash(error.detail) if error.detail is not None else None
            updated = self.connection.execute("""
                UPDATE cms_publication_deliveries
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, last_error_detail_hash = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
            """, (
                status, next_attempt, code, detail_hash, now,
                claim.request.delivery_id, claim.lease_owner, claim.lease_token,
            ))
            if updated.rowcount != 1:
                raise CMSBridgeBlocked("cms.delivery.finish_lost")
        return self.delivery_status(claim.request.delivery_id)

    def run_delivery(
        self,
        publisher: CMSPublisher,
        publication_authority: CMSMessageAuthority,
        *,
        worker_id: Any,
        clock: Callable[[], float] = time.time,
        lease_seconds: float | int = 300,
        operation_guard: Callable[[float], Any] | None = None,
    ) -> DeliveryOutcome:
        if operation_guard is not None and not callable(operation_guard):
            raise CMSBridgeBlocked("cms.delivery.operation_guard_invalid")
        claim = self.claim_delivery(
            worker_id, publication_authority, now=clock(), lease_seconds=lease_seconds,
        )
        if claim is None:
            return DeliveryOutcome("idle")
        try:
            self._live_delivery(claim, _timestamp(clock(), "cms.time.invalid"))
        except CMSBridgeBlocked as error:
            if error.code in {
                "cms.delivery.event_superseded", "cms.delivery.event_cancelled",
            }:
                return DeliveryOutcome(
                    "failed", claim.request.delivery_id, claim.attempt, error.code,
                )
            raise
        if operation_guard is not None:
            try:
                operation_guard(float(lease_seconds))
            except Exception:
                raise CMSBridgeBlocked(
                    "cms.delivery.operation_guard_failed",
                ) from None
        publish = getattr(publisher, "publish", None)
        try:
            if not callable(publish):
                raise CMSPublishFailed("publisher.invalid", retryable=False)
            acknowledgement = publish(claim.request)
            expected = {
                "schema": ACK_SCHEMA,
                "delivery_id": claim.request.delivery_id,
                "payload_sha256": claim.request.payload_sha256,
                "status": "accepted",
            }
            if not isinstance(acknowledgement, Mapping) or dict(acknowledgement) != expected:
                raise CMSPublishFailed("publisher.ack_invalid", retryable=True)
        except Exception as original_error:
            error = _declared_publish_failure(original_error)
            if error is None:
                error = CMSPublishFailed("publisher.unavailable", retryable=True)
            status = self._finish(claim, now=_timestamp(clock(), "cms.time.invalid"), error=error)
            return DeliveryOutcome(status.status, status.delivery_id, status.attempts, error.code)
        status = self._finish(claim, now=_timestamp(clock(), "cms.time.invalid"), error=None)
        return DeliveryOutcome(status.status, status.delivery_id, status.attempts)

    def delivery_status(self, delivery_id: Any) -> DeliveryStatus:
        delivery_id = _token(delivery_id, "cms.delivery_id.invalid")
        row = self.connection.execute("""
            SELECT delivery_id, event_id, plan_id, status, attempts, max_attempts,
                   next_attempt_at, lease_expires_at, last_error_code,
                   last_error_detail_hash, payload_sha256
            FROM cms_publication_deliveries WHERE delivery_id = ?
        """, (delivery_id,)).fetchone()
        if row is None:
            raise CMSBridgeBlocked("cms.delivery.missing")
        return DeliveryStatus(**dict(row))
