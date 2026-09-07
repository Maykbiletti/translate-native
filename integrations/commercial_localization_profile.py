"""Language-neutral commercial review contract; not a semantic language detector.

Models provide semantic evidence. This module validates coverage, exact evidence
locations and verdict consistency, not the truth of a model's interpretation.
Only a host-verified quality receipt can authorize subsequent publication.
"""

from __future__ import annotations

from typing import Any


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
all offers, headings, footnotes, links and conditions. Record every applicable proposition as an item with exact
source and target character spans (zero-based Python Unicode code-point offsets, end exclusive), a stable offer
label and a semantic explanation. Repeated amounts must stay attached to their own offer; number multisets do not
prove fidelity. Check for target-only additions as well as source omissions. 'not_present' is valid only if a dimension
is absent from BOTH texts. 'equivalent' requires nonempty items covering every applicable proposition.
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
                    "source_span": [0, 1],
                    "target_span": [0, 1],
                    "explanation": description,
                }],
            }
            for name, description in DIMENSIONS.items()
        },
    }


def validate_review(value: Any, source: str, target: str, schema: str) -> None:
    """Block missing evidence and uncertainty, without guessing numerical meaning."""
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
    uncertain = value["coverage"] == "uncertain"
    changed = False
    evidenced = False
    for check in checks.values():
        if not isinstance(check, dict) or set(check) != {"status", "items"}:
            invalid()
        status, items = check["status"], check["items"]
        if status not in ("equivalent", "not_present", "changed", "uncertain"):
            invalid()
        if not isinstance(items, list) or len(items) > 1000:
            invalid()
        if (status == "not_present" and items) or (status == "equivalent" and not items):
            invalid()
        seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"offer", "source_span", "target_span", "explanation"}:
                invalid()
            for field in ("offer", "explanation"):
                if not isinstance(item[field], str) or not item[field].strip() or len(item[field]) > 2000:
                    invalid()
            span(item["source_span"], source)
            span(item["target_span"], target)
            identity = (item["offer"], tuple(item["source_span"]), tuple(item["target_span"]))
            if identity in seen:
                invalid()
            seen.add(identity)
        uncertain |= status == "uncertain"
        changed |= status == "changed"
        evidenced |= status == "equivalent"
    if changed:
        raise CommercialReviewBlocked("review.commercial.changed")
    if uncertain or not evidenced:
        raise CommercialReviewBlocked("review.commercial.independent_review_required")
    # Every evidenced condition must resolve to an explicitly reviewed offer.
    assignment = checks["offer_assignment"]
    if assignment["status"] != "equivalent" or not any(
        check["status"] == "equivalent" for name, check in checks.items() if name != "offer_assignment"
    ):
        raise CommercialReviewBlocked("review.commercial.independent_review_required")
    offers = {item["offer"] for item in assignment["items"]}
    if any(item["offer"] not in offers for check in checks.values() for item in check["items"]):
        invalid()
