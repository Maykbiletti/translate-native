"""Language-neutral commercial review contract; not a semantic language detector.

Models provide semantic evidence. This module validates coverage, exact evidence
locations and verdict consistency, not the truth of a model's interpretation.
Only a host-verified quality receipt can authorize subsequent publication.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


PUBLIC_PROFILE_SCHEMA = "translate-native.commercial-capabilities.v9"
REVIEW_SUMMARY_CAPABILITIES_SCHEMA = (
    "translate-native.commercial-review-summary-capabilities.v4"
)
REVIEW_SUMMARY_SCHEMA = "translate-native.commercial-review-summary.v3"
EVIDENCE_BINDING_SCHEMA = "translate-native.commercial-review-evidence-binding.v3"
REVIEW_RESOLUTION_CAPABILITIES_SCHEMA = (
    "translate-native.commercial-review-resolution-capabilities.v3"
)
REVIEW_RESOLUTION_SCHEMA = "translate-native.commercial-review-resolution.v3"
COMMERCIAL_LOCALE_PROFILE_SCHEMA = (
    "translate-native.commercial-locale-quality-profile.v2"
)
COMMERCIAL_RENDERING_REFERENCE_SCHEMA = (
    "translate-native.commercial-rendering-reference.v1"
)
TARGET_LOCALE = re.compile(
    r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?-[A-Z]{2}$"
)
PROFILE_VERSION = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")

DIMENSIONS = {
    "amount_currency": "Amounts, currency identity, units and price-to-product association; no conversion or rounding.",
    "discount_basis": "Discount or saving amount/rate, comparison price, eligibility and reference period.",
    "qualifiers": "From, up to, only, minimum, maximum, approximate and other limits on every claim.",
    "tax_status": "Tax included/excluded, stated rates and scope; do not infer local tax treatment.",
    "billing_interval": "Charge frequency and amount due now, separate from the advertised equivalent monthly price.",
    "commitment": "Contract duration, minimum commitment, trial duration and whether a trial is paid or free.",
    "renewal": "Automatic/manual renewal, renewal price, interval and conditions after introductory offers.",
    "cancellation": "Cancellation deadline, notice period, fees, refund conditions and exceptions.",
    "conditions": "Every remaining eligibility rule, quantity, tier count, limit, deadline and linked footnote.",
    "offer_assignment": "Which offer, tier or product each claim belongs to; never accept swapped prices or conditions.",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence_sha256(
    value: Any,
    source: str,
    target: str,
    profile: str,
    *,
    target_locale: str,
    commercial_quality_profile_version: str,
    commercial_quality_profile_sha256: str,
) -> str:
    """Bind evidence to exact texts, locale, and commercial quality generation."""
    binding = {
        "schema": EVIDENCE_BINDING_SCHEMA,
        "profile": profile,
        "target_locale": target_locale,
        "commercial_quality_profile_version": (
            commercial_quality_profile_version
        ),
        "commercial_quality_profile_sha256": (
            commercial_quality_profile_sha256
        ),
        "source_sha256": _text_sha256(source),
        "target_sha256": _text_sha256(target),
        "evidence": value,
    }
    return hashlib.sha256(_canonical_json(binding)).hexdigest()


def public_review_summary_contract(profile: str) -> dict[str, Any]:
    """Return the exact content-free summary contract external adapters consume."""
    body = {
        "schema": REVIEW_SUMMARY_CAPABILITIES_SCHEMA,
        "result_schema": REVIEW_SUMMARY_SCHEMA,
        "profile": profile,
        "required_fields": [
            "schema", "profile", "status", "review_required_dimensions",
            "evidence_sha256",
        ],
        "statuses": {
            "verified": {"review_required_dimensions": "empty"},
            "review_required": {
                "review_required_dimensions": "one-or-more",
                "requires_independent_review": True,
            },
        },
        "review_required_dimensions": {
            "allowed": list(DIMENSIONS),
            "order": list(DIMENSIONS),
            "unique": True,
        },
        "evidence_sha256": {
            "algorithm": "sha-256",
            "canonicalization": "utf-8-json-sort-keys-no-insignificant-whitespace",
            "binding_schema": EVIDENCE_BINDING_SCHEMA,
            "binding_fields": [
                "schema", "profile", "target_locale",
                "commercial_quality_profile_version",
                "commercial_quality_profile_sha256", "source_sha256",
                "target_sha256", "evidence",
            ],
            "text_hashing": "exact-utf-8",
            "covers": [
                "commercial-profile",
                "exact-target-locale",
                "commercial-quality-profile-generation",
                "exact-source-sha256",
                "exact-target-sha256",
                "offer-registry-and-proposition-assignment",
                "complete-commercial-review-evidence",
            ],
        },
        "content_policy": {
            "source_text": False,
            "target_text": False,
            "source_spans": False,
            "target_spans": False,
            "reviewer_prose": False,
            "project_prices": False,
            "project_brands": False,
        },
    }
    return {
        **body,
        "sha256": hashlib.sha256(_canonical_json(body)).hexdigest(),
    }


def public_review_resolution_contract(profile: str) -> dict[str, Any]:
    """Return the exact content-free contract for resolving uncertain checks."""
    body = {
        "schema": REVIEW_RESOLUTION_CAPABILITIES_SCHEMA,
        "result_schema": REVIEW_RESOLUTION_SCHEMA,
        "profile": profile,
        "applies_when": {
            "review_summary_status": "review_required",
            "reviewed_dimensions": "exact-ordered-review-summary-dimensions",
        },
        "required_fields": [
            "schema", "profile", "contract_sha256", "status",
            "reviewed_dimensions", "method", "receipt_sha256",
            "primary_provider", "provider",
        ],
        "status": "resolved",
        "methods": {
            "qualified_human": {
                "primary_provider": "required",
                "provider": "null",
                "receipt": "verified-qualified-human-review",
            },
            "independent_model": {
                "primary_provider": "required",
                "provider": "required",
                "provider_id_must_differ_from_primary_provider": True,
                "receipt": "verified-independent-model-review",
            },
        },
        "provider_identity": {
            "fields": ["id", "model_id", "model_version"],
            "primary_provider": "required",
            "credentials_published": False,
        },
        "reviewed_dimensions": {
            "allowed": list(DIMENSIONS),
            "order": list(DIMENSIONS),
            "unique": True,
            "must_equal_review_summary": True,
        },
        "receipt_sha256": {
            "algorithm": "sha-256",
            "covers": "exact-verified-review-receipt",
            "raw_receipt_published": False,
        },
        "content_policy": {
            "source_text": False,
            "target_text": False,
            "raw_receipt": False,
            "reviewer_prose": False,
            "qualified_human_identity": False,
            "project_prices": False,
            "project_brands": False,
        },
    }
    return {
        **body,
        "sha256": hashlib.sha256(_canonical_json(body)).hexdigest(),
    }


def _resolution_provider(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "id", "model_id", "model_version",
    }:
        raise CommercialReviewBlocked("review.commercial.resolution_invalid")
    if any(
        not isinstance(value.get(field), str)
        or PROFILE_VERSION.fullmatch(value[field]) is None
        for field in value
    ):
        raise CommercialReviewBlocked("review.commercial.resolution_invalid")
    return json.loads(_canonical_json(value))


def validate_review_resolution(
    value: Any,
    summary: Any,
    profile: str,
) -> dict[str, Any]:
    """Validate exact review-resolution routing without trusting reviewer prose."""
    summary = validate_summary(summary, profile, review_required=True)
    contract = public_review_resolution_contract(profile)
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "profile", "contract_sha256", "status",
            "reviewed_dimensions", "method", "receipt_sha256",
            "primary_provider", "provider",
        }
        or value.get("schema") != REVIEW_RESOLUTION_SCHEMA
        or value.get("profile") != profile
        or value.get("contract_sha256") != contract["sha256"]
        or value.get("status") != "resolved"
        or value.get("reviewed_dimensions")
        != summary["review_required_dimensions"]
        or value.get("method") not in {
            "qualified_human", "independent_model",
        }
        or not isinstance(value.get("receipt_sha256"), str)
        or len(value["receipt_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["receipt_sha256"]
        )
    ):
        raise CommercialReviewBlocked("review.commercial.resolution_invalid")
    primary_provider = _resolution_provider(value.get("primary_provider"))
    provider = _resolution_provider(value.get("provider"))
    if primary_provider is None or (
        value["method"] == "qualified_human" and provider is not None
    ) or (
        value["method"] == "independent_model"
        and (
            provider is None
            or provider["id"] == primary_provider["id"]
        )
    ):
        raise CommercialReviewBlocked("review.commercial.resolution_invalid")
    return json.loads(_canonical_json({
        **value,
        "primary_provider": primary_provider,
        "provider": provider,
    }))


def public_profile(profile: str) -> dict[str, Any]:
    """Return the public, brand-neutral contract implemented by this module."""
    body = {
        "schema": PUBLIC_PROFILE_SCHEMA,
        "profile": profile,
        "review_summary_schema": REVIEW_SUMMARY_SCHEMA,
        "review_summary_contract": public_review_summary_contract(profile),
        "review_resolution_schema": REVIEW_RESOLUTION_SCHEMA,
        "review_resolution_contract": public_review_resolution_contract(
            profile,
        ),
        "applies_to": {
            "content_type": "commercial",
            "locales": "all-supported-target-locales",
        },
        "dimensions": [
            {"name": name, "requirement": requirement}
            for name, requirement in DIMENSIONS.items()
        ],
        "preservation": {
            "amounts": "exact-value",
            "currencies": "same-identity-no-conversion",
            "discounts": "rate-amount-reference-basis-period-and-eligibility",
            "qualifiers": "scope-and-limit-per-claim",
            "taxes": "status-rate-and-scope-without-inference",
            "billing": "charge-amount-and-frequency",
            "commitment": "separate-duration-and-minimum-term",
            "renewal": "mode-price-interval-and-conditions",
            "cancellation": "deadline-notice-fees-refunds-and-exceptions",
            "conditions": "eligibility-limits-deadlines-footnotes-and-links",
            "offer_assignment": "every-claim-bound-to-its-own-offer",
        },
        "rendering": {
            "locale_appropriate": True,
            "natural_wording": True,
            "native_digits_allowed": True,
            "number_words_allowed": True,
            "written_percentages_allowed": True,
            "locale_separators_allowed": True,
            "rounding_allowed": False,
            "currency_conversion_allowed": False,
            "exact_characters_only_when_project_configured": True,
        },
        "verification": {
            "method": "semantic-provider-evidence",
            "deterministic_numeric_regex_is_sufficient": False,
            "evidence_granularity": "every-proposition-per-offer",
            "offer_registry": {
                "identifiers": "unique",
                "source_and_target_regions": "ordered-non-overlapping",
                "discontiguous_regions_allowed": True,
                "every_proposition_contained_in_declared_offer": True,
                "every_offer_has_exactly_one_assignment_item": True,
            },
            "directions": ["matched", "source_only", "target_only"],
            "ambiguous_values": "unresolved",
            "unresolved_route": "independent-model-or-qualified-native-domain-review",
            "automatic_publication_when_unresolved": False,
        },
        "locale_quality_profile": {
            "schema": COMMERCIAL_LOCALE_PROFILE_SCHEMA,
            "required": True,
            "binding_fields": [
                "locale", "version", "commercial_profile",
                "quality_profile_version", "quality_profile_sha256",
                "rendering_reference", "sha256",
            ],
            "rendering_reference": {
                "schema": COMMERCIAL_RENDERING_REFERENCE_SCHEMA,
                "authority": "Unicode CLDR",
                "version": "48",
                "purpose": "target-locale-rendering-guidance",
                "semantic_proof": False,
                "unresolved_route": (
                    "independent-model-or-qualified-native-domain-review"
                ),
            },
            "required_commercial_checks": list(DIMENSIONS),
            "provider_phases": [
                "transcreation", "target_native", "source_fidelity",
            ],
            "tamper_policy": "block-before-provider",
        },
        "protected_terms": "project-configuration-only",
    }
    return {
        **body,
        "sha256": hashlib.sha256(_canonical_json(body)).hexdigest(),
    }

CREATION_GUIDANCE = """Localize prices, offers and subscriptions as natural native commercial copy.
Preserve every commercial proposition and its association with the correct offer, not merely a bag of numbers.
Keep brands, product names and protected slogans exactly as configured for this project; invent no universal list.
Locale formatting, native digits, number words and written percentages are allowed when semantically equivalent.
Do not convert currency, change a tax claim, round a price, strengthen a saving or conceal a condition.
Keep billing frequency distinct from contract length. Preserve footnote and link associations.
An explicit instruction to preserve exact characters (for example a currency symbol) overrides format adaptation."""

NATIVE_GUIDANCE = """Judge commercial copy as native writing: idiomatic pricing labels, CTAs, natural number,
currency and interval expressions, unambiguous offer layout and readable conditions. Do not require digits instead
of number words, ASCII instead of native digits, or source-locale punctuation. Do not infer missing commercial facts."""

FIDELITY_GUIDANCE = """For each commercial dimension inspect the COMPLETE source and candidate, including
all offers, headings, footnotes, links and conditions. Record every applicable proposition as an item with a stable
offer label, semantic explanation and relation: 'matched', 'source_only' for an omission, or 'target_only' for an
addition. Give exact zero-based Python Unicode code-point spans with an exclusive end; the absent side of a one-sided
item MUST be null. Repeated amounts must stay attached to their own offer; number multisets do not prove fidelity.
'not_present' is valid only if a dimension is absent from BOTH texts. 'equivalent' requires nonempty matched items
covering every applicable proposition. A dimension-level 'changed' or 'uncertain' verdict requires at least one
specific item; use coverage='uncertain' for unresolved overall completeness without inventing a span.
Use 'changed' for a known defect and 'uncertain' for unresolved interpretation, coverage or insufficient language/domain
evidence. Use coverage='uncertain' unless every proposition and offer association was checked. Never resolve numeric
ambiguity by guessing. Number words, written percentages, native digits and locale separators may be equivalent;
12 months and one year may be equivalent only when the actual commercial terms agree. State the interpretation in
the explanation. No average score can override a defect. Return commercial_review in addition to the normal review."""


class CommercialReviewBlocked(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def review_contract(schema: str) -> dict[str, Any]:
    return {
        "schema": schema,
        "coverage": "complete or uncertain",
        "offers": [{
            "id": "stable unique offer identifier",
            "source_spans": [[0, 1]],
            "target_spans": [[0, 1]],
        }],
        "checks": {
            name: {
                "status": "equivalent, not_present, changed or uncertain",
                "items": [{
                    "offer": "stable offer label",
                    "relation": "matched, source_only, or target_only",
                    "source_span": [0, 1],
                    "target_span": [0, 1],
                    "explanation": description,
                }],
            }
            for name, description in DIMENSIONS.items()
        },
    }


def validate_review(
    value: Any,
    source: str,
    target: str,
    schema: str,
    *,
    target_locale: str,
    commercial_quality_profile_version: str,
    commercial_quality_profile_sha256: str,
    allow_uncertain: bool = False,
) -> dict[str, Any]:
    """Validate evidence and return a content-free, hash-bound routing summary."""
    def invalid() -> None:
        raise CommercialReviewBlocked("review.commercial.invalid")

    def span(value: Any, text: str) -> tuple[int, int]:
        if (
            not isinstance(value, list) or len(value) != 2
            or any(type(offset) is not int for offset in value)
            or not 0 <= value[0] < value[1] <= len(text)
            or not text[value[0]:value[1]].strip()
        ):
            invalid()
        return value[0], value[1]

    def regions(value: Any, text: str) -> tuple[tuple[int, int], ...]:
        if not isinstance(value, list) or len(value) > 1000:
            invalid()
        parsed = tuple(span(item, text) for item in value)
        if list(parsed) != sorted(parsed) or any(
            previous[1] > current[0]
            for previous, current in zip(parsed, parsed[1:])
        ):
            invalid()
        return parsed

    if (
        not isinstance(target_locale, str)
        or TARGET_LOCALE.fullmatch(target_locale) is None
        or not isinstance(commercial_quality_profile_version, str)
        or PROFILE_VERSION.fullmatch(commercial_quality_profile_version) is None
        or not isinstance(commercial_quality_profile_sha256, str)
        or len(commercial_quality_profile_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in commercial_quality_profile_sha256
        )
    ):
        invalid()
    if not isinstance(value, dict) or set(value) != {
        "schema", "coverage", "offers", "checks",
    }:
        invalid()
    if value["schema"] != schema or value["coverage"] not in ("complete", "uncertain"):
        invalid()
    offers = value["offers"]
    if not isinstance(offers, list) or len(offers) > 1000:
        invalid()
    offer_regions: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {}
    occupied = {"source_spans": [], "target_spans": []}
    for offer in offers:
        if not isinstance(offer, dict) or set(offer) != {
            "id", "source_spans", "target_spans",
        }:
            invalid()
        offer_id = offer["id"]
        if (
            not isinstance(offer_id, str)
            or PROFILE_VERSION.fullmatch(offer_id) is None
            or offer_id in offer_regions
        ):
            invalid()
        parsed = {
            "source_spans": regions(offer["source_spans"], source),
            "target_spans": regions(offer["target_spans"], target),
        }
        if not parsed["source_spans"] and not parsed["target_spans"]:
            invalid()
        offer_regions[offer_id] = parsed
        for side in occupied:
            occupied[side].extend(
                (start, end, offer_id) for start, end in parsed[side]
            )
    for side in occupied:
        ordered = sorted(occupied[side])
        if any(
            previous[1] > current[0]
            for previous, current in zip(ordered, ordered[1:])
        ):
            invalid()
    checks = value["checks"]
    if not isinstance(checks, dict) or set(checks) != set(DIMENSIONS):
        invalid()
    uncertain_dimensions: set[str] = set()
    coverage_uncertain = value["coverage"] == "uncertain"
    changed = False
    evidenced = False
    for name, check in checks.items():
        if not isinstance(check, dict) or set(check) != {"status", "items"}:
            invalid()
        status, items = check["status"], check["items"]
        if status not in ("equivalent", "not_present", "changed", "uncertain"):
            invalid()
        if not isinstance(items, list) or len(items) > 1000:
            invalid()
        if (
            (status == "not_present" and items)
            or (status in ("equivalent", "changed", "uncertain") and not items)
        ):
            invalid()
        seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "offer", "relation", "source_span", "target_span", "explanation",
            }:
                invalid()
            if (
                not isinstance(item["offer"], str)
                or PROFILE_VERSION.fullmatch(item["offer"]) is None
                or item["offer"] not in offer_regions
                or not isinstance(item["explanation"], str)
                or not item["explanation"].strip()
                or len(item["explanation"]) > 2000
            ):
                invalid()
            relation = item["relation"]
            source_span = target_span = None
            if relation == "matched":
                source_span = span(item["source_span"], source)
                target_span = span(item["target_span"], target)
            elif relation == "source_only":
                source_span = span(item["source_span"], source)
                if item["target_span"] is not None:
                    invalid()
            elif relation == "target_only":
                if item["source_span"] is not None:
                    invalid()
                target_span = span(item["target_span"], target)
            else:
                invalid()
            declared = offer_regions[item["offer"]]
            if source_span is not None and not any(
                region[0] <= source_span[0]
                and source_span[1] <= region[1]
                for region in declared["source_spans"]
            ):
                invalid()
            if target_span is not None and not any(
                region[0] <= target_span[0]
                and target_span[1] <= region[1]
                for region in declared["target_spans"]
            ):
                invalid()
            if status == "equivalent" and relation != "matched":
                invalid()
            identity = (
                item["offer"], relation,
                tuple(item["source_span"]) if item["source_span"] is not None else None,
                tuple(item["target_span"]) if item["target_span"] is not None else None,
            )
            if identity in seen:
                invalid()
            seen.add(identity)
        if status == "uncertain":
            uncertain_dimensions.add(name)
        changed |= status == "changed"
        evidenced |= status == "equivalent"
    if changed:
        raise CommercialReviewBlocked("review.commercial.changed")
    # Every evidenced condition must resolve to an explicitly reviewed offer.
    assignment = checks["offer_assignment"]
    if assignment["status"] != "equivalent" or not any(
        check["status"] == "equivalent" for name, check in checks.items() if name != "offer_assignment"
    ):
        uncertain_dimensions.add("offer_assignment")
    else:
        assigned = [item["offer"] for item in assignment["items"]]
        if (
            any(item["relation"] != "matched" for item in assignment["items"])
            or len(assigned) != len(set(assigned))
            or set(assigned) != set(offer_regions)
        ):
            invalid()
    if coverage_uncertain or not evidenced:
        uncertain_dimensions.update(DIMENSIONS)
    summary = {
        "schema": REVIEW_SUMMARY_SCHEMA,
        "profile": schema,
        "status": "review_required" if uncertain_dimensions else "verified",
        "review_required_dimensions": [
            name for name in DIMENSIONS if name in uncertain_dimensions
        ],
        "evidence_sha256": evidence_sha256(
            value,
            source,
            target,
            schema,
            target_locale=target_locale,
            commercial_quality_profile_version=(
                commercial_quality_profile_version
            ),
            commercial_quality_profile_sha256=(
                commercial_quality_profile_sha256
            ),
        ),
    }
    if uncertain_dimensions and not allow_uncertain:
        raise CommercialReviewBlocked("review.commercial.independent_review_required")
    return summary


def validate_summary(
    value: Any,
    profile: str,
    *,
    review_required: bool,
) -> dict[str, Any]:
    """Validate a persisted summary without retaining customer or reviewer text."""
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "profile", "status", "review_required_dimensions",
            "evidence_sha256",
        }
        or value["schema"] != REVIEW_SUMMARY_SCHEMA
        or value["profile"] != profile
        or value["status"] not in {"verified", "review_required"}
        or not isinstance(value["evidence_sha256"], str)
        or len(value["evidence_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in value["evidence_sha256"])
        or not isinstance(value["review_required_dimensions"], list)
    ):
        raise CommercialReviewBlocked("review.commercial.summary_invalid")
    dimensions = value["review_required_dimensions"]
    if (
        len(dimensions) != len(set(dimensions))
        or any(name not in DIMENSIONS for name in dimensions)
        or dimensions != [name for name in DIMENSIONS if name in dimensions]
        or (value["status"] == "verified") != (not dimensions)
        or (value["status"] == "review_required" and not review_required)
    ):
        raise CommercialReviewBlocked("review.commercial.summary_invalid")
    return json.loads(_canonical_json(value))
