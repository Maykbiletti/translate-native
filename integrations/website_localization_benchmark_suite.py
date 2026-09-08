#!/usr/bin/env python3
"""Canonical, output-free benchmark suite for EU website localization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


SCHEMA = "blun.website-localization-benchmark-suite.v1"
VERSION = "eu-web-diversity-2026-09-1"
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
