#!/usr/bin/env python3
"""Canonical, output-free benchmark suite for EU website localization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


SCHEMA = "blun.website-localization-benchmark-suite.v2"
VERSION = "eu-web-diversity-2026-09-2"
LONG_FORM_MINIMUM_CHARACTERS = 400


@dataclass(frozen=True)
class SourceCase:
    key: str
    domain: str
    content_type: str
    source_text: str
    adversarial_tags: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "source_id": f"benchmark.{self.key}",
            "source_revision": VERSION,
            "source_locale": "en-IE",
            "source_sha256": hashlib.sha256(self.source_text.encode("utf-8")).hexdigest(),
            "long_form": len(self.source_text) >= LONG_FORM_MINIMUM_CHARACTERS,
        }


SOURCE_CASES: tuple[SourceCase, ...] = (
    SourceCase(
        "software-headline", "software", "headline",
        "Move from idea to launch without losing the context that makes your work distinct.",
        ("marketing_calque", "information_structure", "brand_voice"),
    ),
    SourceCase(
        "commerce-cta", "commerce", "cta",
        "Compare the options, check what is included, and choose the plan that fits your team.",
        ("unnatural_cta", "politeness", "meaning_omission"),
    ),
    SourceCase(
        "travel-marketing", "travel", "marketing",
        '<section><h2>Take the slower road north</h2><p>Cook with local hosts, cross the lake by boat, and leave room for plans to change with the weather in {{season}}.</p><a href="https://example.test/routes">Explore the route</a></section>',
        ("cultural_fit", "rhythm", "source_shaped_syntax", "html_integrity", "placeholder_integrity"),
    ),
    SourceCase(
        "payments-ui", "financial_services", "ui",
        '{"error":"Your payment was not processed.","retry":"Try again; you will not be charged twice."}',
        ("negation", "modality", "false_friend", "concise_ui", "json_integrity"),
    ),
    SourceCase(
        "api-documentation", "developer_tools", "documentation",
        "Before rotating the API key, pause new deployments. Existing sessions remain valid until they expire, but they cannot be renewed with the old key.",
        ("sequence", "negation", "modality", "terminology"),
    ),
    SourceCase(
        "health-seo", "healthcare", "seo",
        "Secure appointment booking for clinics that need clear reminders, accessible forms, and fewer missed visits.",
        ("seo_naturalness", "register", "unsupported_claim"),
    ),
    SourceCase(
        "subscription-legal-long", "subscription_services", "legal",
        "This agreement starts when the account owner accepts the order. It continues for twelve months and renews for another twelve months unless either party gives written notice at least thirty days before the current term ends. Cancellation stops the next renewal; it does not shorten the current term or create a refund. We may suspend access only when payment is overdue by more than fourteen days and only after sending a reminder. Nothing in this section limits rights that cannot legally be waived.",
        ("contract_term", "renewal", "cancellation", "negation", "modality", "long_context"),
    ),
    SourceCase(
        "offer-commercial-long", "business_software", "commercial",
        "The Standard option starts at EUR 29.90 per month when billed annually for a twelve-month contract. The displayed amount excludes VAT. New customers receive 20 percent off the first annual invoice, calculated from the undiscounted annual total; the discount does not apply to usage charges. The subscription renews for another year at the then-current standard price unless it is cancelled at least thirty days before renewal. The Professional option costs up to EUR 79.90 per month, including support but excluding VAT, and may be cancelled monthly after the initial three-month term. Additional conditions are shown before checkout.",
        ("amount", "currency", "discount_basis", "tax", "billing_interval", "contract_term", "renewal", "cancellation", "long_context"),
    ),
    SourceCase(
        "workspace-passes-commercial-long", "coworking", "commercial",
        "Day passes start at EUR 18 excluding VAT and include access from 08:00 to 18:00. Evening access costs an additional EUR 6 per visit. Customers who prepay ten visits receive 15 percent off the day-pass total, but the discount does not apply to evening access or meeting rooms. Prepaid visits expire six months after purchase and are not renewed automatically. Unused visits are refundable only during the first fourteen days, provided that none has been redeemed. A separate monthly membership is available from EUR 149 and may be cancelled with thirty days’ notice.",
        ("amount", "currency", "discount_basis", "qualifier", "tax", "billing_interval", "expiry", "cancellation", "offer_assignment", "long_context"),
    ),
    SourceCase(
        "weekend-package-commercial-long", "travel", "commercial",
        "The weekend package costs from EUR 349 per person for two nights when two adults share one room. Breakfast and the advertised boat transfer are included; the local visitor tax of EUR 3.50 per person per night is not. A 25 percent deposit is charged when the booking is confirmed, and the remaining balance is due fourteen days before arrival. Cancellations made at least thirty days before arrival receive a full refund of the deposit. Later cancellations receive no deposit refund, although one date change is permitted subject to availability and any price difference. The offer is available for stays completed by 31 March 2027.",
        ("amount", "currency", "qualifier", "tax", "deposit", "payment_timing", "cancellation", "refund", "eligibility", "offer_assignment", "long_context"),
    ),
    SourceCase(
        "course-instalments-commercial-long", "professional_education", "commercial",
        "Complete course access costs EUR 240 including VAT and remains available for twelve months after enrolment. Customers may instead choose three monthly instalments of EUR 85, for a total of EUR 255; the instalment plan is a payment schedule, not a monthly subscription. A free seven-day preview includes the first module but does not extend the twelve-month access period. Access does not renew automatically. Withdrawal within fourteen days is refundable only if less than 20 percent of the course has been opened, and an administrative fee of EUR 15 is deducted from an approved refund.",
        ("amount", "currency", "tax", "billing_interval", "commitment", "trial", "renewal", "cancellation", "refund", "percentage_words", "long_context"),
    ),
    SourceCase(
        "media-trial-commercial", "digital_media", "commercial",
        "The seven-day trial costs EUR 1. It becomes a monthly subscription at EUR 12.99 including VAT unless cancelled before the trial ends. The annual option costs EUR 119.90, is billed once at sign-up and renews yearly at the price shown before renewal. Cancelling stops the next charge but does not refund the current billing period.",
        ("amount", "currency", "tax", "trial", "billing_interval", "renewal", "cancellation", "negation", "offer_assignment"),
    ),
    SourceCase(
        "supporter-membership-commercial", "cultural_nonprofit", "commercial",
        "Supporter membership is EUR 60 per calendar year and includes two guest tickets. People joining from 1 October pay EUR 30 for the remainder of that calendar year. Membership renews automatically on 1 January at the then-current annual fee unless notice is received by 1 December. An optional donation added at checkout is not part of the membership price and is not reduced by the late-year rate.",
        ("amount", "currency", "proration", "commitment", "renewal", "cancellation", "conditions", "offer_assignment"),
    ),
    SourceCase(
        "equipment-rental-commercial", "equipment_rental", "commercial",
        "The compact unit is available from EUR 42 per day, excluding VAT, with a minimum rental of three days. The larger unit costs EUR 68 per day under the same minimum. A refundable EUR 250 deposit is charged separately for either unit. Rentals longer than fourteen consecutive days receive 10 percent off rental charges after day fourteen; delivery, collection and damage fees are excluded. Extensions depend on availability and use the daily rate in effect when the extension is confirmed. Cancellation is free until 48 hours before delivery, after which one daily charge is due.",
        ("amount", "currency", "qualifier", "tax", "minimum", "deposit", "discount_basis", "extension", "cancellation", "offer_assignment", "long_context"),
    ),
    SourceCase(
        "parcel-contract-commercial-long", "logistics", "commercial",
        "The first one hundred domestic parcels each month cost EUR 0.45 each, and parcels 101 to 500 cost EUR 0.39 each. A minimum monthly charge of EUR 35 applies even when fewer parcels are sent. Prices exclude VAT and a fuel surcharge of up to 8 percent, calculated on transport charges only. Usage is invoiced in the following month, while the account fee is charged at the start of the current month. The agreement lasts twenty-four months and renews for twelve months unless cancelled at least ninety days before the term ends. Early termination requires payment of the remaining minimum monthly charges, except when the service-level guarantee has been missed for three consecutive months.",
        ("amount", "currency", "tiered_price", "qualifier", "tax", "surcharge_basis", "billing_interval", "commitment", "renewal", "cancellation", "offer_assignment", "long_context"),
    ),
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def manifest() -> dict[str, Any]:
    body = {
        "schema": SCHEMA,
        "version": VERSION,
        "long_form_minimum_characters": LONG_FORM_MINIMUM_CHARACTERS,
        "cases": [case.as_payload() for case in SOURCE_CASES],
    }
    body["sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
    return json.loads(_canonical_json(body))


_BY_BINDING = {
    (
        case.as_payload()["source_id"],
        case.as_payload()["source_revision"],
        case.as_payload()["source_sha256"],
        case.content_type,
    ): case.as_payload()
    for case in SOURCE_CASES
}
if len(_BY_BINDING) != len(SOURCE_CASES):
    raise RuntimeError("duplicate website-localization benchmark case")


def case_for_job(job: Any) -> dict[str, Any]:
    try:
        binding = (
            job["source"]["id"], job["source"]["revision"],
            job["source"]["sha256"], job["content_type"],
        )
        case = _BY_BINDING[binding]
        if job["source"]["locale"] != case["source_locale"]:
            raise KeyError
        if job["source"]["text"] != case["source_text"]:
            raise KeyError
    except (KeyError, TypeError):
        raise ValueError("job is not an exact canonical benchmark case") from None
    return json.loads(_canonical_json(case))
