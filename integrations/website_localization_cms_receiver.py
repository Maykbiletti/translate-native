#!/usr/bin/env python3
"""Fail-closed reference receiver for CMS localization callbacks.

The host owns authentication, message authorities, and atomic CMS operations.
This module parses each exact callback, verifies it before invoking the host,
and signs an acknowledgement only after the host confirms the same binding.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


MAX_REQUEST_BYTES = 4_000_000
MAX_RESPONSE_BYTES = 65_536
RECEIVER_PATH = "/v1/localization/callback"
RECEIVER_ERROR_SCHEMA = "blun.cms-localization-receiver-error.v1"
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+=-]{1,4096}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
PUBLICATION_FIELDS = {
    "schema", "delivery_id", "event_id", "site_id", "website_version",
    "plan_id", "source_id", "source_revision", "source_sequence",
    "source_sha256", "localizations",
}
LOCALIZATION_FIELDS = {
    "locale", "target_text", "target_sha256", "approval_id",
    "approval_expires_at", "release_evidence",
}
TOMBSTONE_FIELDS = {
    "schema", "delivery_id", "tombstone_id", "event_id", "site_id",
    "website_version", "plan_id", "source_id", "source_sequence",
    "publication_delivery_id", "publication_payload_sha256", "locales",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required CMS receiver dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CMS = _load_module(
    "blun_website_localization_receiver_cms",
    _ROOT / "integrations" / "website_localization_cms.py",
)


class CMSReceiverBlocked(RuntimeError):
    """Stable, content-free receiver failure suitable for an HTTP boundary."""

    def __init__(self, code: str, *, retryable: bool, http_status: int):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("CMS receiver error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("CMS receiver retryability must be boolean")
        if isinstance(http_status, bool) or http_status not in {400, 401, 409, 500, 503}:
            raise ValueError("CMS receiver HTTP status is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


@dataclass(frozen=True)
class VerifiedPublication:
    delivery_id: str
    payload_sha256: str
    payload: dict[str, Any]
    signature: Any


@dataclass(frozen=True)
class PublicationExpectation:
    event_id: str
    site_id: str
    website_version: str
    plan_id: str
    source_id: str
    source_revision: str
    source_sequence: int
    source_sha256: str
    required_locales: tuple[str, ...]
    content_type: str
    commercial_profile: str | None


@dataclass(frozen=True)
class VerifiedTombstone:
    delivery_id: str
    payload_sha256: str
    payload: dict[str, Any]
    signature: Any


@dataclass(frozen=True)
class TombstoneExpectation:
    tombstone_id: str
    event_id: str
    site_id: str
    website_version: str
    plan_id: str
    source_id: str
    source_sequence: int
    publication_delivery_id: str
    publication_payload_sha256: str
    locales: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedHealthProbe:
    probe_id: str
    contract_sha256: str


@dataclass(frozen=True)
class CMSReceiverHTTPResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class CMSMessageAuthority(Protocol):
    def sign(self, payload: bytes) -> Any: ...
    def verify(self, payload: bytes, signature: Any) -> bool: ...


class PublicationCommitter(Protocol):
    def __call__(self, publication: VerifiedPublication) -> Mapping[str, Any]: ...


class TombstoneCommitter(Protocol):
    def __call__(self, tombstone: VerifiedTombstone) -> Mapping[str, Any]: ...


class HealthAuthenticator(Protocol):
    def __call__(self, headers: Mapping[str, str]) -> bool: ...


class HealthChecker(Protocol):
    def __call__(self, probe: VerifiedHealthProbe) -> Mapping[str, Any]: ...


class PublicationExpectationResolver(Protocol):
    def __call__(
        self, publication: VerifiedPublication,
    ) -> PublicationExpectation | Mapping[str, Any]: ...


class TombstoneExpectationResolver(Protocol):
    def __call__(
        self, tombstone: VerifiedTombstone,
    ) -> TombstoneExpectation | Mapping[str, Any]: ...


def _canonical_json(value: Any, *, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CMSReceiverBlocked(
            "receiver.json_invalid", retryable=False, http_status=400,
        ) from None
    if not encoded or len(encoded) > maximum:
        raise CMSReceiverBlocked(
            "receiver.message_size", retryable=False, http_status=400,
        )
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


def _parse_body(body: Any) -> tuple[dict[str, Any], bytes]:
    if not isinstance(body, bytes) or not body or len(body) > MAX_REQUEST_BYTES:
        raise CMSReceiverBlocked(
            "receiver.message_size", retryable=False, http_status=400,
        )
    try:
        text = body.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("BOM rejected")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise CMSReceiverBlocked(
            "receiver.json_invalid", retryable=False, http_status=400,
        ) from None
    if not isinstance(value, dict):
        raise CMSReceiverBlocked(
            "receiver.request_invalid", retryable=False, http_status=400,
        )
    canonical = _canonical_json(value, maximum=MAX_REQUEST_BYTES)
    if canonical != body:
        raise CMSReceiverBlocked(
            "receiver.request_noncanonical", retryable=False, http_status=400,
        )
    return value, canonical


def _headers(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        items = tuple(value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = tuple(value)
    else:
        raise CMSReceiverBlocked(
            "receiver.headers_invalid", retryable=False, http_status=400,
        )
    headers: dict[str, str] = {}
    for item in items:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes, bytearray))
            or len(item) != 2
        ):
            raise CMSReceiverBlocked(
                "receiver.headers_invalid", retryable=False, http_status=400,
            )
        name, content = item
        if (
            not isinstance(name, str)
            or HEADER_NAME.fullmatch(name) is None
            or not isinstance(content, str)
            or "\r" in content
            or "\n" in content
        ):
            raise CMSReceiverBlocked(
                "receiver.headers_invalid", retryable=False, http_status=400,
            )
        normalized = name.lower()
        if normalized in headers:
            raise CMSReceiverBlocked(
                "receiver.headers_ambiguous", retryable=False, http_status=400,
            )
        headers[normalized] = content.strip()
    content_type = headers.get("content-type", "").lower().replace(" ", "")
    if content_type != "application/json;charset=utf-8":
        raise CMSReceiverBlocked(
            "receiver.content_type", retryable=False, http_status=400,
        )
    if "transfer-encoding" in headers:
        raise CMSReceiverBlocked(
            "receiver.framing_invalid", retryable=False, http_status=400,
        )
    return headers


def _signature(value: Any) -> Any:
    if not isinstance(value, dict) or set(value) != {
        "algorithm", "key_id", "signature",
    }:
        raise CMSReceiverBlocked(
            "receiver.signature_invalid", retryable=False, http_status=401,
        )
    if (
        not isinstance(value["algorithm"], str)
        or TOKEN.fullmatch(value["algorithm"]) is None
        or not isinstance(value["key_id"], str)
        or TOKEN.fullmatch(value["key_id"]) is None
        or not isinstance(value["signature"], str)
        or SIGNATURE_VALUE.fullmatch(value["signature"]) is None
    ):
        raise CMSReceiverBlocked(
            "receiver.signature_invalid", retryable=False, http_status=401,
        )
    return _CMS.CMSMessageSignature(
        value["algorithm"], value["key_id"], value["signature"],
    )


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CMSReceiverBlocked(
            "receiver.publication_invalid", retryable=False, http_status=400,
        )
    result = float(value)
    if result < 0 or result != result or result in {float("inf"), float("-inf")}:
        raise CMSReceiverBlocked(
            "receiver.publication_invalid", retryable=False, http_status=400,
        )
    return result


def _verify_publication(value: Any, payload_sha256: str, *, now: float) -> bytes:
    if not isinstance(value, dict) or set(value) != PUBLICATION_FIELDS:
        raise CMSReceiverBlocked(
            "receiver.publication_invalid", retryable=False, http_status=400,
        )
    token_fields = (
        "delivery_id", "event_id", "site_id", "website_version", "plan_id",
        "source_id", "source_revision",
    )
    if (
        value.get("schema") != _CMS.PUBLICATION_SCHEMA
        or any(
            not isinstance(value.get(field), str)
            or TOKEN.fullmatch(value[field]) is None
            for field in token_fields
        )
        or SHA256.fullmatch(str(value.get("source_sha256"))) is None
        or isinstance(value.get("source_sequence"), bool)
        or not isinstance(value.get("source_sequence"), int)
        or value["source_sequence"] <= 0
    ):
        raise CMSReceiverBlocked(
            "receiver.publication_invalid", retryable=False, http_status=400,
        )
    localizations = value.get("localizations")
    if not isinstance(localizations, list) or not localizations:
        raise CMSReceiverBlocked(
            "receiver.bundle_incomplete", retryable=False, http_status=409,
        )
    locales: list[str] = []
    for item in localizations:
        if not isinstance(item, dict) or set(item) != LOCALIZATION_FIELDS:
            raise CMSReceiverBlocked(
                "receiver.localization_invalid", retryable=False, http_status=400,
            )
        locale = item.get("locale")
        target = item.get("target_text")
        target_sha256 = item.get("target_sha256")
        approval_id = item.get("approval_id")
        if (
            not isinstance(locale, str)
            or TOKEN.fullmatch(locale) is None
            or not isinstance(target, str)
            or not target
            or not unicodedata.is_normalized("NFC", target)
            or SHA256.fullmatch(str(target_sha256)) is None
            or hashlib.sha256(target.encode("utf-8")).hexdigest() != target_sha256
            or not isinstance(approval_id, str)
            or TOKEN.fullmatch(approval_id) is None
            or _timestamp(item.get("approval_expires_at")) <= now
            or not _CMS._valid_release_evidence(
                item.get("release_evidence"),
                locale=locale,
                target_sha256=target_sha256,
                approval_id=approval_id,
            )
        ):
            raise CMSReceiverBlocked(
                "receiver.localization_invalid", retryable=False, http_status=400,
            )
        locales.append(locale)
    if locales != sorted(set(locales)):
        raise CMSReceiverBlocked(
            "receiver.bundle_invalid", retryable=False, http_status=409,
        )
    payload_bytes = _canonical_json(value, maximum=MAX_REQUEST_BYTES)
    if hashlib.sha256(payload_bytes).hexdigest() != payload_sha256:
        raise CMSReceiverBlocked(
            "receiver.payload_binding", retryable=False, http_status=409,
        )
    unsigned = {key: content for key, content in value.items() if key != "delivery_id"}
    expected_delivery_id = "blun-cms-delivery-" + hashlib.sha256(
        _canonical_json(unsigned, maximum=MAX_REQUEST_BYTES)
    ).hexdigest()
    if value["delivery_id"] != expected_delivery_id:
        raise CMSReceiverBlocked(
            "receiver.delivery_binding", retryable=False, http_status=409,
        )
    return payload_bytes


def _verify_expectation(
    publication: dict[str, Any],
    expectation: PublicationExpectation | Mapping[str, Any],
) -> None:
    if isinstance(expectation, Mapping):
        fields = tuple(PublicationExpectation.__dataclass_fields__)
        if set(expectation) != set(fields):
            raise ValueError("expectation is invalid")
        try:
            expectation = PublicationExpectation(**dict(expectation))
        except TypeError:
            raise ValueError("expectation is invalid") from None
    if not isinstance(expectation, PublicationExpectation):
        raise TypeError("expectation must be PublicationExpectation or an exact mapping")
    token_fields = (
        "event_id", "site_id", "website_version", "plan_id", "source_id",
        "source_revision", "content_type",
    )
    if (
        any(
            not isinstance(getattr(expectation, field), str)
            or TOKEN.fullmatch(getattr(expectation, field)) is None
            for field in token_fields
        )
        or expectation.content_type not in _CMS._PLANNER.CONTENT_TYPES
        or isinstance(expectation.source_sequence, bool)
        or not isinstance(expectation.source_sequence, int)
        or expectation.source_sequence <= 0
        or not isinstance(expectation.source_sha256, str)
        or SHA256.fullmatch(expectation.source_sha256) is None
        or not isinstance(expectation.required_locales, tuple)
        or not expectation.required_locales
        or expectation.required_locales
        != tuple(sorted(set(expectation.required_locales)))
        or any(
            not isinstance(locale, str) or TOKEN.fullmatch(locale) is None
            for locale in expectation.required_locales
        )
        or (
            expectation.commercial_profile is not None
            and (
                not isinstance(expectation.commercial_profile, str)
                or TOKEN.fullmatch(expectation.commercial_profile) is None
            )
        )
        or (expectation.content_type == "commercial")
        != (expectation.commercial_profile is not None)
    ):
        raise ValueError("expectation is invalid")
    expected_binding = {
        "event_id": expectation.event_id,
        "site_id": expectation.site_id,
        "website_version": expectation.website_version,
        "plan_id": expectation.plan_id,
        "source_id": expectation.source_id,
        "source_revision": expectation.source_revision,
        "source_sequence": expectation.source_sequence,
        "source_sha256": expectation.source_sha256,
    }
    if any(publication.get(field) != value for field, value in expected_binding.items()):
        raise CMSReceiverBlocked(
            "receiver.source_binding", retryable=False, http_status=409,
        )
    localizations = publication["localizations"]
    if tuple(item["locale"] for item in localizations) != expectation.required_locales:
        raise CMSReceiverBlocked(
            "receiver.bundle_incomplete", retryable=False, http_status=409,
        )
    for item in localizations:
        evidence = item["release_evidence"]
        if (
            evidence["content_type"] != expectation.content_type
            or evidence["commercial_profile"] != expectation.commercial_profile
        ):
            raise CMSReceiverBlocked(
                "receiver.release_scope", retryable=False, http_status=409,
            )


def _verify_tombstone(value: Any, payload_sha256: str) -> bytes:
    if not isinstance(value, dict) or set(value) != TOMBSTONE_FIELDS:
        raise CMSReceiverBlocked(
            "receiver.tombstone_invalid", retryable=False, http_status=400,
        )
    token_fields = (
        "delivery_id", "tombstone_id", "event_id", "site_id",
        "website_version", "plan_id", "source_id", "publication_delivery_id",
    )
    locales = value.get("locales")
    if (
        value.get("schema") != _CMS.TOMBSTONE_DELIVERY_SCHEMA
        or any(
            not isinstance(value.get(field), str)
            or TOKEN.fullmatch(value[field]) is None
            for field in token_fields
        )
        or isinstance(value.get("source_sequence"), bool)
        or not isinstance(value.get("source_sequence"), int)
        or value["source_sequence"] <= 0
        or not isinstance(value.get("publication_payload_sha256"), str)
        or SHA256.fullmatch(value["publication_payload_sha256"]) is None
        or not isinstance(locales, list)
        or not locales
        or locales != sorted(set(locales))
        or any(
            not isinstance(locale, str) or TOKEN.fullmatch(locale) is None
            for locale in locales
        )
    ):
        raise CMSReceiverBlocked(
            "receiver.tombstone_invalid", retryable=False, http_status=400,
        )
    payload_bytes = _canonical_json(value, maximum=MAX_REQUEST_BYTES)
    if hashlib.sha256(payload_bytes).hexdigest() != payload_sha256:
        raise CMSReceiverBlocked(
            "receiver.payload_binding", retryable=False, http_status=409,
        )
    unsigned = {key: content for key, content in value.items() if key != "delivery_id"}
    expected_delivery_id = "blun-cms-tombstone-" + hashlib.sha256(
        _canonical_json(unsigned, maximum=MAX_REQUEST_BYTES)
    ).hexdigest()
    if value["delivery_id"] != expected_delivery_id:
        raise CMSReceiverBlocked(
            "receiver.delivery_binding", retryable=False, http_status=409,
        )
    return payload_bytes


def _verify_tombstone_expectation(
    tombstone: dict[str, Any],
    expectation: TombstoneExpectation | Mapping[str, Any],
) -> None:
    if isinstance(expectation, Mapping):
        fields = tuple(TombstoneExpectation.__dataclass_fields__)
        if set(expectation) != set(fields):
            raise ValueError("expectation is invalid")
        try:
            expectation = TombstoneExpectation(**dict(expectation))
        except TypeError:
            raise ValueError("expectation is invalid") from None
    if not isinstance(expectation, TombstoneExpectation):
        raise TypeError(
            "expectation must be TombstoneExpectation or an exact mapping"
        )
    token_fields = (
        "tombstone_id", "event_id", "site_id", "website_version", "plan_id",
        "source_id", "publication_delivery_id",
    )
    if (
        any(
            not isinstance(getattr(expectation, field), str)
            or TOKEN.fullmatch(getattr(expectation, field)) is None
            for field in token_fields
        )
        or isinstance(expectation.source_sequence, bool)
        or not isinstance(expectation.source_sequence, int)
        or expectation.source_sequence <= 0
        or not isinstance(expectation.publication_payload_sha256, str)
        or SHA256.fullmatch(expectation.publication_payload_sha256) is None
        or not isinstance(expectation.locales, tuple)
        or not expectation.locales
        or expectation.locales != tuple(sorted(set(expectation.locales)))
        or any(
            not isinstance(locale, str) or TOKEN.fullmatch(locale) is None
            for locale in expectation.locales
        )
    ):
        raise ValueError("expectation is invalid")
    expected = {
        "tombstone_id": expectation.tombstone_id,
        "event_id": expectation.event_id,
        "site_id": expectation.site_id,
        "website_version": expectation.website_version,
        "plan_id": expectation.plan_id,
        "source_id": expectation.source_id,
        "source_sequence": expectation.source_sequence,
        "publication_delivery_id": expectation.publication_delivery_id,
        "publication_payload_sha256": expectation.publication_payload_sha256,
        "locales": list(expectation.locales),
    }
    if any(tombstone.get(field) != content for field, content in expected.items()):
        raise CMSReceiverBlocked(
            "receiver.tombstone_binding", retryable=False, http_status=409,
        )


def _verify_publication_transport(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    *,
    now: float | int,
) -> VerifiedPublication:
    if not callable(getattr(publication_authority, "verify", None)):
        raise TypeError("publication_authority must provide verify")
    now = _timestamp(now)
    envelope, _ = _parse_body(body)
    parsed_headers = _headers(headers)
    if set(envelope) != {"schema", "payload_sha256", "publication", "signature"}:
        raise CMSReceiverBlocked(
            "receiver.request_invalid", retryable=False, http_status=400,
        )
    payload_sha256 = envelope.get("payload_sha256")
    if (
        envelope.get("schema") != _CMS.PUBLICATION_HTTP_REQUEST_SCHEMA
        or not isinstance(payload_sha256, str)
        or SHA256.fullmatch(payload_sha256) is None
    ):
        raise CMSReceiverBlocked(
            "receiver.request_invalid", retryable=False, http_status=400,
        )
    payload_bytes = _verify_publication(
        envelope.get("publication"), payload_sha256, now=now,
    )
    publication = envelope["publication"]
    bindings = {
        "delivery_id": publication["delivery_id"],
        "payload_sha256": payload_sha256,
    }
    for name, binding in _CMS.PUBLICATION_HTTP_BINDING_HEADERS:
        if parsed_headers.get(name.lower()) != bindings.get(binding):
            raise CMSReceiverBlocked(
                "receiver.header_binding", retryable=False, http_status=409,
            )
    declared_length = parsed_headers.get("content-length")
    if declared_length is not None and (
        not declared_length.isascii()
        or not declared_length.isdecimal()
        or int(declared_length) != len(body)
    ):
        raise CMSReceiverBlocked(
            "receiver.framing_invalid", retryable=False, http_status=400,
        )
    signature = _signature(envelope.get("signature"))
    try:
        verified = publication_authority.verify(payload_bytes, signature) is True
    except Exception:
        verified = False
    if not verified:
        raise CMSReceiverBlocked(
            "receiver.signature_invalid", retryable=False, http_status=401,
        )
    return VerifiedPublication(
        delivery_id=publication["delivery_id"],
        payload_sha256=payload_sha256,
        payload=json.loads(payload_bytes.decode("utf-8")),
        signature=signature,
    )


def verify_publication_request(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    expectation: PublicationExpectation,
    *,
    now: float | int,
) -> VerifiedPublication:
    """Verify one exact publication callback without performing a write."""

    publication = _verify_publication_transport(
        body, headers, publication_authority, now=now,
    )
    _verify_expectation(publication.payload, expectation)
    return publication


def _verify_tombstone_transport(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
) -> VerifiedTombstone:
    if not callable(getattr(publication_authority, "verify", None)):
        raise TypeError("publication_authority must provide verify")
    envelope, _ = _parse_body(body)
    parsed_headers = _headers(headers)
    if set(envelope) != {"schema", "payload_sha256", "tombstone", "signature"}:
        raise CMSReceiverBlocked(
            "receiver.request_invalid", retryable=False, http_status=400,
        )
    payload_sha256 = envelope.get("payload_sha256")
    if (
        envelope.get("schema") != _CMS.TOMBSTONE_HTTP_REQUEST_SCHEMA
        or not isinstance(payload_sha256, str)
        or SHA256.fullmatch(payload_sha256) is None
    ):
        raise CMSReceiverBlocked(
            "receiver.request_invalid", retryable=False, http_status=400,
        )
    payload_bytes = _verify_tombstone(envelope.get("tombstone"), payload_sha256)
    tombstone = envelope["tombstone"]
    bindings = {
        "delivery_id": tombstone["delivery_id"],
        "payload_sha256": payload_sha256,
    }
    for name, binding in _CMS.PUBLICATION_HTTP_BINDING_HEADERS:
        if parsed_headers.get(name.lower()) != bindings.get(binding):
            raise CMSReceiverBlocked(
                "receiver.header_binding", retryable=False, http_status=409,
            )
    declared_length = parsed_headers.get("content-length")
    if declared_length is not None and (
        not declared_length.isascii()
        or not declared_length.isdecimal()
        or int(declared_length) != len(body)
    ):
        raise CMSReceiverBlocked(
            "receiver.framing_invalid", retryable=False, http_status=400,
        )
    signature = _signature(envelope.get("signature"))
    try:
        verified = publication_authority.verify(payload_bytes, signature) is True
    except Exception:
        verified = False
    if not verified:
        raise CMSReceiverBlocked(
            "receiver.signature_invalid", retryable=False, http_status=401,
        )
    return VerifiedTombstone(
        delivery_id=tombstone["delivery_id"],
        payload_sha256=payload_sha256,
        payload=json.loads(payload_bytes.decode("utf-8")),
        signature=signature,
    )


def verify_tombstone_request(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    expectation: TombstoneExpectation,
) -> VerifiedTombstone:
    """Verify one exact tombstone callback without deleting CMS content."""

    tombstone = _verify_tombstone_transport(
        body, headers, publication_authority,
    )
    _verify_tombstone_expectation(tombstone.payload, expectation)
    return tombstone


def verify_health_request(
    body: bytes,
    headers: Any,
    authenticate: Callable[[Mapping[str, str]], bool],
    *,
    contract_sha256: str,
) -> VerifiedHealthProbe:
    """Authenticate and verify one content-free publisher health challenge."""

    if not callable(authenticate):
        raise TypeError("authenticate must be callable")
    if (
        not isinstance(contract_sha256, str)
        or SHA256.fullmatch(contract_sha256) is None
    ):
        raise ValueError("contract_sha256 is invalid")
    envelope, _ = _parse_body(body)
    parsed_headers = _headers(headers)
    if set(envelope) != {"schema", "probe"}:
        raise CMSReceiverBlocked(
            "receiver.health_request_invalid", retryable=False, http_status=400,
        )
    probe = envelope.get("probe")
    if (
        envelope.get("schema") != _CMS.PUBLICATION_HEALTH_HTTP_REQUEST_SCHEMA
        or not isinstance(probe, dict)
        or set(probe) != {"schema", "probe_id", "contract_sha256"}
        or probe.get("schema") != _CMS.PUBLICATION_HEALTH_SCHEMA
        or not isinstance(probe.get("probe_id"), str)
        or not 16 <= len(probe["probe_id"]) <= 256
        or TOKEN.fullmatch(probe["probe_id"]) is None
        or not isinstance(probe.get("contract_sha256"), str)
        or SHA256.fullmatch(probe["contract_sha256"]) is None
    ):
        raise CMSReceiverBlocked(
            "receiver.health_request_invalid", retryable=False, http_status=400,
        )
    bindings = {
        "probe_id": probe["probe_id"],
        "contract_sha256": probe["contract_sha256"],
    }
    for name, binding in _CMS.PUBLICATION_HEALTH_HTTP_BINDING_HEADERS:
        if parsed_headers.get(name.lower()) != bindings.get(binding):
            raise CMSReceiverBlocked(
                "receiver.health_header_binding", retryable=False, http_status=409,
            )
    declared_length = parsed_headers.get("content-length")
    if declared_length is not None and (
        not declared_length.isascii()
        or not declared_length.isdecimal()
        or int(declared_length) != len(body)
    ):
        raise CMSReceiverBlocked(
            "receiver.framing_invalid", retryable=False, http_status=400,
        )
    try:
        authenticated = authenticate(dict(parsed_headers)) is True
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.authentication_failed", retryable=True, http_status=503,
        ) from None
    if not authenticated:
        raise CMSReceiverBlocked(
            "receiver.authentication_invalid", retryable=False, http_status=401,
        )
    if probe["contract_sha256"] != contract_sha256:
        raise CMSReceiverBlocked(
            "receiver.health_contract_binding", retryable=False, http_status=409,
        )
    return VerifiedHealthProbe(
        probe_id=probe["probe_id"],
        contract_sha256=probe["contract_sha256"],
    )


def _signed_acknowledgement(
    *,
    delivery_id: str,
    payload_sha256: str,
    acknowledgement_schema: str,
    status: str,
    response_schema: str,
    authority: CMSMessageAuthority,
) -> CMSReceiverHTTPResponse:
    acknowledgement = {
        "schema": acknowledgement_schema,
        "delivery_id": delivery_id,
        "payload_sha256": payload_sha256,
        "status": status,
    }
    acknowledgement_bytes = _canonical_json(
        acknowledgement, maximum=MAX_RESPONSE_BYTES,
    )
    try:
        signature = authority.sign(acknowledgement_bytes)
        signature_mapping = {
            "algorithm": signature.algorithm,
            "key_id": signature.key_id,
            "signature": signature.signature,
        }
        normalized_signature = _signature(signature_mapping)
        verified = authority.verify(
            acknowledgement_bytes, normalized_signature,
        ) is True
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.acknowledgement_signing", retryable=True, http_status=503,
        ) from None
    if not verified:
        raise CMSReceiverBlocked(
            "receiver.acknowledgement_signing", retryable=True, http_status=503,
        )
    response = {
        "schema": response_schema,
        "acknowledgement": acknowledgement,
        "signature": signature_mapping,
    }
    response_body = _canonical_json(response, maximum=MAX_RESPONSE_BYTES)
    return CMSReceiverHTTPResponse(
        status=200,
        headers=(
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(response_body))),
            ("Cache-Control", "no-store"),
        ),
        body=response_body,
    )


def _signed_health_acknowledgement(
    probe: VerifiedHealthProbe,
    authority: CMSMessageAuthority,
) -> CMSReceiverHTTPResponse:
    acknowledgement = {
        "schema": _CMS.PUBLICATION_HEALTH_ACK_SCHEMA,
        "probe_id": probe.probe_id,
        "contract_sha256": probe.contract_sha256,
        "status": "healthy",
    }
    acknowledgement_bytes = _canonical_json(
        acknowledgement, maximum=MAX_RESPONSE_BYTES,
    )
    try:
        signature = authority.sign(acknowledgement_bytes)
        signature_mapping = {
            "algorithm": signature.algorithm,
            "key_id": signature.key_id,
            "signature": signature.signature,
        }
        normalized_signature = _signature(signature_mapping)
        verified = authority.verify(
            acknowledgement_bytes, normalized_signature,
        ) is True
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.acknowledgement_signing", retryable=True, http_status=503,
        ) from None
    if not verified:
        raise CMSReceiverBlocked(
            "receiver.acknowledgement_signing", retryable=True, http_status=503,
        )
    response = {
        "schema": _CMS.PUBLICATION_HEALTH_HTTP_RESPONSE_SCHEMA,
        "acknowledgement": acknowledgement,
        "signature": signature_mapping,
    }
    response_body = _canonical_json(response, maximum=MAX_RESPONSE_BYTES)
    return CMSReceiverHTTPResponse(
        status=200,
        headers=(
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(response_body))),
            ("Cache-Control", "no-store"),
        ),
        body=response_body,
    )


def _receive_verified_publication(
    publication: VerifiedPublication,
    acknowledgement_authority: CMSMessageAuthority,
    commit: Callable[[VerifiedPublication], Mapping[str, Any]],
) -> CMSReceiverHTTPResponse:
    try:
        receipt = commit(publication)
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.commit_failed", retryable=True, http_status=503,
        ) from None
    expected_receipt = {
        "delivery_id": publication.delivery_id,
        "payload_sha256": publication.payload_sha256,
        "status": "committed",
    }
    if receipt != expected_receipt:
        raise CMSReceiverBlocked(
            "receiver.commit_unconfirmed", retryable=True, http_status=503,
        )
    return _signed_acknowledgement(
        delivery_id=publication.delivery_id,
        payload_sha256=publication.payload_sha256,
        acknowledgement_schema=_CMS.ACK_SCHEMA,
        status="accepted",
        response_schema=_CMS.PUBLICATION_HTTP_RESPONSE_SCHEMA,
        authority=acknowledgement_authority,
    )


def _receive_verified_tombstone(
    tombstone: VerifiedTombstone,
    acknowledgement_authority: CMSMessageAuthority,
    delete: Callable[[VerifiedTombstone], Mapping[str, Any]],
) -> CMSReceiverHTTPResponse:
    try:
        receipt = delete(tombstone)
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.delete_failed", retryable=True, http_status=503,
        ) from None
    expected_receipt = {
        "delivery_id": tombstone.delivery_id,
        "payload_sha256": tombstone.payload_sha256,
        "status": "deleted",
    }
    if receipt != expected_receipt:
        raise CMSReceiverBlocked(
            "receiver.delete_unconfirmed", retryable=True, http_status=503,
        )
    return _signed_acknowledgement(
        delivery_id=tombstone.delivery_id,
        payload_sha256=tombstone.payload_sha256,
        acknowledgement_schema=_CMS.TOMBSTONE_ACK_SCHEMA,
        status="deleted",
        response_schema=_CMS.TOMBSTONE_HTTP_RESPONSE_SCHEMA,
        authority=acknowledgement_authority,
    )


def receive_publication(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    acknowledgement_authority: CMSMessageAuthority,
    expectation: PublicationExpectation,
    commit: Callable[[VerifiedPublication], Mapping[str, Any]],
    *,
    now: float | int,
) -> CMSReceiverHTTPResponse:
    """Verify, commit through the host, then return one signed acknowledgement."""

    if not callable(commit):
        raise TypeError("commit must be callable")
    if (
        not callable(getattr(acknowledgement_authority, "sign", None))
        or not callable(getattr(acknowledgement_authority, "verify", None))
    ):
        raise TypeError("acknowledgement_authority must provide sign and verify")
    publication = verify_publication_request(
        body, headers, publication_authority, expectation, now=now,
    )
    return _receive_verified_publication(
        publication, acknowledgement_authority, commit,
    )


def receive_tombstone(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    acknowledgement_authority: CMSMessageAuthority,
    expectation: TombstoneExpectation,
    delete: Callable[[VerifiedTombstone], Mapping[str, Any]],
) -> CMSReceiverHTTPResponse:
    """Verify, delete through the host, then return one signed acknowledgement."""

    if not callable(delete):
        raise TypeError("delete must be callable")
    if (
        not callable(getattr(acknowledgement_authority, "sign", None))
        or not callable(getattr(acknowledgement_authority, "verify", None))
    ):
        raise TypeError("acknowledgement_authority must provide sign and verify")
    tombstone = verify_tombstone_request(
        body, headers, publication_authority, expectation,
    )
    return _receive_verified_tombstone(
        tombstone, acknowledgement_authority, delete,
    )


def receive_publication_resolved(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    acknowledgement_authority: CMSMessageAuthority,
    resolve_expectation: Callable[
        [VerifiedPublication], PublicationExpectation | Mapping[str, Any]
    ],
    commit: Callable[[VerifiedPublication], Mapping[str, Any]],
    *,
    now: float | int,
) -> CMSReceiverHTTPResponse:
    """Verify a publication before resolving its current host expectation."""

    if not callable(resolve_expectation):
        raise TypeError("resolve_expectation must be callable")
    if not callable(commit):
        raise TypeError("commit must be callable")
    if (
        not callable(getattr(acknowledgement_authority, "sign", None))
        or not callable(getattr(acknowledgement_authority, "verify", None))
    ):
        raise TypeError("acknowledgement_authority must provide sign and verify")
    publication = _verify_publication_transport(
        body, headers, publication_authority, now=now,
    )
    resolver_input = VerifiedPublication(
        delivery_id=publication.delivery_id,
        payload_sha256=publication.payload_sha256,
        payload=json.loads(_canonical_json(
            publication.payload, maximum=MAX_REQUEST_BYTES,
        ).decode("utf-8")),
        signature=publication.signature,
    )
    try:
        expectation = resolve_expectation(resolver_input)
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.expectation_failed", retryable=True, http_status=503,
        ) from None
    try:
        _verify_expectation(publication.payload, expectation)
    except (TypeError, ValueError):
        raise CMSReceiverBlocked(
            "receiver.expectation_invalid", retryable=True, http_status=503,
        ) from None
    return _receive_verified_publication(
        publication, acknowledgement_authority, commit,
    )


def receive_tombstone_resolved(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    acknowledgement_authority: CMSMessageAuthority,
    resolve_expectation: Callable[
        [VerifiedTombstone], TombstoneExpectation | Mapping[str, Any]
    ],
    delete: Callable[[VerifiedTombstone], Mapping[str, Any]],
) -> CMSReceiverHTTPResponse:
    """Verify a tombstone before resolving its current host expectation."""

    if not callable(resolve_expectation):
        raise TypeError("resolve_expectation must be callable")
    if not callable(delete):
        raise TypeError("delete must be callable")
    if (
        not callable(getattr(acknowledgement_authority, "sign", None))
        or not callable(getattr(acknowledgement_authority, "verify", None))
    ):
        raise TypeError("acknowledgement_authority must provide sign and verify")
    tombstone = _verify_tombstone_transport(
        body, headers, publication_authority,
    )
    resolver_input = VerifiedTombstone(
        delivery_id=tombstone.delivery_id,
        payload_sha256=tombstone.payload_sha256,
        payload=json.loads(_canonical_json(
            tombstone.payload, maximum=MAX_REQUEST_BYTES,
        ).decode("utf-8")),
        signature=tombstone.signature,
    )
    try:
        expectation = resolve_expectation(resolver_input)
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.expectation_failed", retryable=True, http_status=503,
        ) from None
    try:
        _verify_tombstone_expectation(tombstone.payload, expectation)
    except (TypeError, ValueError):
        raise CMSReceiverBlocked(
            "receiver.expectation_invalid", retryable=True, http_status=503,
        ) from None
    return _receive_verified_tombstone(
        tombstone, acknowledgement_authority, delete,
    )


def receive_health(
    body: bytes,
    headers: Any,
    authenticate: Callable[[Mapping[str, str]], bool],
    acknowledgement_authority: CMSMessageAuthority,
    check: Callable[[VerifiedHealthProbe], Mapping[str, Any]],
    *,
    contract_sha256: str,
) -> CMSReceiverHTTPResponse:
    """Authenticate, check the host, then sign one content-free health reply."""

    if not callable(check):
        raise TypeError("check must be callable")
    if (
        not callable(getattr(acknowledgement_authority, "sign", None))
        or not callable(getattr(acknowledgement_authority, "verify", None))
    ):
        raise TypeError("acknowledgement_authority must provide sign and verify")
    probe = verify_health_request(
        body, headers, authenticate, contract_sha256=contract_sha256,
    )
    try:
        receipt = check(probe)
    except Exception:
        raise CMSReceiverBlocked(
            "receiver.health_check_failed", retryable=True, http_status=503,
        ) from None
    expected_receipt = {
        "probe_id": probe.probe_id,
        "contract_sha256": probe.contract_sha256,
        "status": "healthy",
    }
    if receipt != expected_receipt:
        raise CMSReceiverBlocked(
            "receiver.health_unconfirmed", retryable=True, http_status=503,
        )
    return _signed_health_acknowledgement(probe, acknowledgement_authority)


class CMSReceiverApplication:
    """HTTPS-only WSGI boundary for the complete reference receiver."""

    def __init__(
        self,
        publication_authority: CMSMessageAuthority,
        acknowledgement_authority: CMSMessageAuthority,
        authenticate: Callable[[Mapping[str, str]], bool],
        resolve_publication_expectation: Callable[
            [VerifiedPublication], PublicationExpectation | Mapping[str, Any]
        ],
        commit: Callable[[VerifiedPublication], Mapping[str, Any]],
        resolve_tombstone_expectation: Callable[
            [VerifiedTombstone], TombstoneExpectation | Mapping[str, Any]
        ],
        delete: Callable[[VerifiedTombstone], Mapping[str, Any]],
        check: Callable[[VerifiedHealthProbe], Mapping[str, Any]],
        *,
        contract_sha256: str,
        clock: Callable[[], float | int],
        path: str = RECEIVER_PATH,
        require_https: bool = True,
    ):
        callbacks = {
            "authenticate": authenticate,
            "resolve_publication_expectation": resolve_publication_expectation,
            "commit": commit,
            "resolve_tombstone_expectation": resolve_tombstone_expectation,
            "delete": delete,
            "check": check,
            "clock": clock,
        }
        for name, callback in callbacks.items():
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if not callable(getattr(publication_authority, "verify", None)):
            raise TypeError("publication_authority must provide verify")
        if (
            not callable(getattr(acknowledgement_authority, "sign", None))
            or not callable(getattr(acknowledgement_authority, "verify", None))
        ):
            raise TypeError("acknowledgement_authority must provide sign and verify")
        if (
            not isinstance(contract_sha256, str)
            or SHA256.fullmatch(contract_sha256) is None
        ):
            raise ValueError("contract_sha256 is invalid")
        if (
            not isinstance(path, str)
            or not path.isascii()
            or not path.startswith("/")
            or not 2 <= len(path) <= 256
            or any(character in path for character in "?#\r\n")
        ):
            raise ValueError("path is invalid")
        if not isinstance(require_https, bool):
            raise TypeError("require_https must be boolean")
        self.publication_authority = publication_authority
        self.acknowledgement_authority = acknowledgement_authority
        self.authenticate = authenticate
        self.resolve_publication_expectation = resolve_publication_expectation
        self.commit = commit
        self.resolve_tombstone_expectation = resolve_tombstone_expectation
        self.delete = delete
        self.check = check
        self.contract_sha256 = contract_sha256
        self.clock = clock
        self.path = path
        self.require_https = require_https

    @staticmethod
    def _wsgi_headers(environ: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        items = []
        for key, value in environ.items():
            if key == "CONTENT_TYPE":
                name = "Content-Type"
            elif key == "CONTENT_LENGTH":
                name = "Content-Length"
            elif isinstance(key, str) and key.startswith("HTTP_"):
                name = key[5:].replace("_", "-")
            else:
                continue
            items.append((name, value))
        if len(items) > 64 or any(
            not isinstance(name, str)
            or not isinstance(value, str)
            or len(name) + len(value) > 16_384
            for name, value in items
        ):
            raise CMSReceiverBlocked(
                "receiver.headers_invalid", retryable=False, http_status=400,
            )
        return tuple(items)

    @staticmethod
    def _send(
        start_response: Callable[..., Any],
        response: CMSReceiverHTTPResponse,
    ) -> list[bytes]:
        phrases = {200: "OK"}
        headers = list(response.headers)
        headers.extend((
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ))
        start_response(
            f"{response.status} {phrases.get(response.status, 'Error')}", headers,
        )
        return [response.body]

    @staticmethod
    def _blocked(
        start_response: Callable[..., Any],
        status: int,
        code: str,
        *,
        retryable: bool,
    ) -> list[bytes]:
        phrases = {
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            411: "Length Required",
            413: "Content Too Large",
            415: "Unsupported Media Type",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }
        body = _canonical_json({
            "schema": RECEIVER_ERROR_SCHEMA,
            "status": "BLOCK",
            "error": code,
            "retryable": retryable,
        }, maximum=MAX_RESPONSE_BYTES)
        start_response(f"{status} {phrases[status]}", (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ))
        return [body]

    def __call__(
        self,
        environ: Mapping[str, Any],
        start_response: Callable[..., Any],
    ) -> list[bytes]:
        if not isinstance(environ, dict):
            return self._blocked(
                start_response, 500, "receiver.environment_invalid", retryable=True,
            )
        if environ.get("PATH_INFO") != self.path:
            return self._blocked(
                start_response, 404, "receiver.path_not_found", retryable=False,
            )
        if environ.get("REQUEST_METHOD") != "POST":
            return self._blocked(
                start_response, 405, "receiver.method_not_allowed", retryable=False,
            )
        if self.require_https and environ.get("wsgi.url_scheme") != "https":
            return self._blocked(
                start_response, 400, "receiver.https_required", retryable=False,
            )
        if environ.get("QUERY_STRING") not in {None, ""}:
            return self._blocked(
                start_response, 400, "receiver.query_rejected", retryable=False,
            )
        if environ.get("HTTP_TRANSFER_ENCODING"):
            return self._blocked(
                start_response, 400, "receiver.framing_invalid", retryable=False,
            )
        content_type = environ.get("CONTENT_TYPE")
        if not isinstance(content_type, str) or (
            content_type.lower().replace(" ", "")
            != "application/json;charset=utf-8"
        ):
            return self._blocked(
                start_response, 415, "receiver.content_type", retryable=False,
            )
        length = environ.get("CONTENT_LENGTH")
        if (
            not isinstance(length, str)
            or not length.isascii()
            or not length.isdecimal()
        ):
            return self._blocked(
                start_response, 411, "receiver.content_length_required",
                retryable=False,
            )
        size = int(length)
        if size <= 0:
            return self._blocked(
                start_response, 400, "receiver.message_size", retryable=False,
            )
        if size > MAX_REQUEST_BYTES:
            return self._blocked(
                start_response, 413, "receiver.message_size", retryable=False,
            )
        stream = environ.get("wsgi.input")
        try:
            body = stream.read(size)
        except Exception:
            body = None
        if not isinstance(body, bytes) or len(body) != size:
            return self._blocked(
                start_response, 400, "receiver.body_invalid", retryable=False,
            )
        try:
            raw_headers = self._wsgi_headers(environ)
            parsed_headers = _headers(raw_headers)
            try:
                authenticated = self.authenticate(dict(parsed_headers)) is True
            except Exception:
                raise CMSReceiverBlocked(
                    "receiver.authentication_failed",
                    retryable=True,
                    http_status=503,
                ) from None
            if not authenticated:
                raise CMSReceiverBlocked(
                    "receiver.authentication_invalid",
                    retryable=False,
                    http_status=401,
                )
            envelope, _ = _parse_body(body)
            schema = envelope.get("schema")
            if schema == _CMS.PUBLICATION_HTTP_REQUEST_SCHEMA:
                response = receive_publication_resolved(
                    body,
                    raw_headers,
                    self.publication_authority,
                    self.acknowledgement_authority,
                    self.resolve_publication_expectation,
                    self.commit,
                    now=self.clock(),
                )
            elif schema == _CMS.TOMBSTONE_HTTP_REQUEST_SCHEMA:
                response = receive_tombstone_resolved(
                    body,
                    raw_headers,
                    self.publication_authority,
                    self.acknowledgement_authority,
                    self.resolve_tombstone_expectation,
                    self.delete,
                )
            elif schema == _CMS.PUBLICATION_HEALTH_HTTP_REQUEST_SCHEMA:
                response = receive_health(
                    body,
                    raw_headers,
                    lambda _: True,
                    self.acknowledgement_authority,
                    self.check,
                    contract_sha256=self.contract_sha256,
                )
            else:
                raise CMSReceiverBlocked(
                    "receiver.operation_invalid", retryable=False, http_status=400,
                )
            return self._send(start_response, response)
        except CMSReceiverBlocked as error:
            return self._blocked(
                start_response,
                error.http_status,
                error.code,
                retryable=error.retryable,
            )
        except Exception:
            return self._blocked(
                start_response, 500, "receiver.internal", retryable=True,
            )
