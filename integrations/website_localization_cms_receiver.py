#!/usr/bin/env python3
"""Fail-closed reference receiver for signed CMS localization publications.

The host owns authentication, the publication verifier, the acknowledgement
authority, and the atomic CMS write. This module parses one exact callback,
verifies it before invoking the host write, and signs an acknowledgement only
after the host confirms the same idempotency binding.
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
    expectation: PublicationExpectation,
) -> None:
    if not isinstance(expectation, PublicationExpectation):
        raise TypeError("expectation must be PublicationExpectation")
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
    expectation: TombstoneExpectation,
) -> None:
    if not isinstance(expectation, TombstoneExpectation):
        raise TypeError("expectation must be TombstoneExpectation")
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


def verify_publication_request(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    expectation: PublicationExpectation,
    *,
    now: float | int,
) -> VerifiedPublication:
    """Verify one exact publication callback without performing a write."""

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
    _verify_expectation(publication, expectation)
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


def verify_tombstone_request(
    body: bytes,
    headers: Any,
    publication_authority: CMSMessageAuthority,
    expectation: TombstoneExpectation,
) -> VerifiedTombstone:
    """Verify one exact tombstone callback without deleting CMS content."""

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
    _verify_tombstone_expectation(tombstone, expectation)
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
