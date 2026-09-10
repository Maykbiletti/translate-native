"""Language-neutral commercial review contract; not a semantic language detector.

Models provide semantic evidence. This module validates coverage, exact evidence
locations and verdict consistency, not the truth of a model's interpretation.
Only a host-verified quality receipt can authorize subsequent publication.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


PUBLIC_PROFILE_SCHEMA = "translate-native.commercial-capabilities.v1"
REVIEW_SUMMARY_SCHEMA = "translate-native.commercial-review-summary.v1"

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


def public_profile(profile: str) -> dict[str, Any]:
    """Return the public, brand-neutral contract implemented by this module."""
    body = {
        "schema": PUBLIC_PROFILE_SCHEMA,
        "profile": profile,
        "review_summary_schema": REVIEW_SUMMARY_SCHEMA,
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
            "directions": ["matched", "source_only", "target_only"],
            "ambiguous_values": "unresolved",
            "unresolved_route": "independent-model-or-qualified-native-domain-review",
            "automatic_publication_when_unresolved": False,
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
    allow_uncertain: bool = False,
) -> dict[str, Any]:
    """Validate evidence and return a content-free, hash-bound routing summary."""
    def invalid() -> None:
        raise CommercialReviewBlocked("review.commercial.invalid")

    def span(value: Any, text: str) -> None:
        if (
            not isinstance(value, list) or len(value) != 2
            or any(type(offset) is not int for offset in value)
            or not 0 <= value[0] < value[1] <= len(text)
            or not text[value[0]:value[1]].strip()
        ):
            invalid()

    if not isinstance(value, dict) or set(value) != {"schema", "coverage", "checks"}:
        invalid()
    if value["schema"] != schema or value["coverage"] not in ("complete", "uncertain"):
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
            for field in ("offer", "explanation"):
                if not isinstance(item[field], str) or not item[field].strip() or len(item[field]) > 2000:
                    invalid()
            relation = item["relation"]
            if relation == "matched":
                span(item["source_span"], source)
                span(item["target_span"], target)
            elif relation == "source_only":
                span(item["source_span"], source)
                if item["target_span"] is not None:
                    invalid()
            elif relation == "target_only":
                if item["source_span"] is not None:
                    invalid()
                span(item["target_span"], target)
            else:
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
        offers = {item["offer"] for item in assignment["items"]}
        if any(item["offer"] not in offers for check in checks.values() for item in check["items"]):
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
        "evidence_sha256": hashlib.sha256(_canonical_json(value)).hexdigest(),
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
