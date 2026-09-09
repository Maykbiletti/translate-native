#!/usr/bin/env python3
"""Canonical, output-free benchmark suite for EU website localization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


SCHEMA = "blun.website-localization-benchmark-suite.v3"
VERSION = "eu-web-content-lanes-2026-09-3"
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
    SourceCase(
        "clinic-continuity-headline", "healthcare", "headline",
        "Care that follows the person, not the paperwork.",
        ("brand_voice", "idiom", "information_structure", "unsupported_claim"),
    ),
    SourceCase(
        "energy-control-headline", "climate_technology", "headline",
        "Use less energy without putting comfort on pause.",
        ("brand_voice", "negation", "rhythm", "marketing_calque"),
    ),
    SourceCase(
        "learning-progress-headline", "professional_education", "headline",
        "Turn practical experience into progress people can see.",
        ("brand_voice", "idiom", "information_structure", "source_shaped_syntax"),
    ),
    SourceCase(
        "delivery-confidence-headline", "logistics", "headline",
        "Every handover clear. Every delivery accounted for.",
        ("brand_voice", "rhythm", "ellipsis", "marketing_calque"),
    ),
    SourceCase(
        "money-clarity-headline", "financial_services", "headline",
        "See where your money is going before you decide where it should go next.",
        ("brand_voice", "information_structure", "register", "unsupported_claim"),
    ),
    SourceCase(
        "permit-guidance-headline", "public_services", "headline",
        "A clearer route through permits, forms and deadlines.",
        ("brand_voice", "register", "coordination", "false_friend"),
    ),
    SourceCase(
        "museum-belonging-headline", "cultural_nonprofit", "headline",
        "Come for the collection. Leave with a story of your own.",
        ("brand_voice", "rhythm", "cultural_fit", "source_shaped_syntax"),
    ),
    SourceCase(
        "route-planner-cta", "travel", "cta",
        "Build your route, then save it for the days when plans change.",
        ("unnatural_cta", "sequence", "register", "meaning_omission"),
    ),
    SourceCase(
        "appointment-choice-cta", "healthcare", "cta",
        "Choose a suitable appointment and tell us what support you need.",
        ("unnatural_cta", "politeness", "register", "meaning_omission"),
    ),
    SourceCase(
        "sandbox-start-cta", "developer_tools", "cta",
        "Open the sandbox and test the workflow with sample data first.",
        ("unnatural_cta", "sequence", "terminology", "modality"),
    ),
    SourceCase(
        "benefit-check-cta", "public_services", "cta",
        "Check what you may be entitled to before starting the application.",
        ("unnatural_cta", "modality", "register", "sequence"),
    ),
    SourceCase(
        "course-fit-cta", "professional_education", "cta",
        "Explore the syllabus and decide whether the course fits your next step.",
        ("unnatural_cta", "idiom", "register", "meaning_omission"),
    ),
    SourceCase(
        "budget-review-cta", "financial_services", "cta",
        "Review the forecast with your team before you publish it.",
        ("unnatural_cta", "sequence", "terminology", "politeness"),
    ),
    SourceCase(
        "volunteer-match-cta", "cultural_nonprofit", "cta",
        "Find a role that suits your time, interests and access needs.",
        ("unnatural_cta", "cultural_fit", "register", "coordination"),
    ),
    SourceCase(
        "workflow-marketing-long", "business_software", "marketing",
        "Work rarely arrives in the neat order shown on a project plan. A customer changes direction, a colleague spots a risk, and the decision that explains both is buried in yesterday’s messages. This workspace keeps the brief, discussion and next action together, so the team can adapt without rebuilding the story from memory. People still decide what matters and who may act; the service simply carries the relevant context forward. The result is calmer handovers, fewer repeated questions and work that remains recognisably yours from first draft to launch.",
        ("brand_voice", "long_context", "information_structure", "marketing_calque", "unsupported_claim", "rhythm"),
    ),
    SourceCase(
        "home-energy-marketing-long", "renewable_energy", "marketing",
        "A comfortable home should not require constant attention to every radiator, window and tariff. The energy guide learns nothing by guesswork: it uses the readings and preferences the household chooses to share, explains why it recommends a change, and leaves the final decision with the resident. On bright afternoons it may suggest moving flexible tasks earlier; before a cold evening it can show which rooms are likely to need heat first. Clear comparisons make savings visible without turning daily life into a spreadsheet or promising results that the weather cannot guarantee.",
        ("long_context", "unsupported_claim", "modality", "cultural_fit", "marketing_calque", "information_structure"),
    ),
    SourceCase(
        "clinic-welcome-marketing", "healthcare", "marketing",
        "Make each visit easier to prepare for with clear reminders, accessible forms and guidance that respects the patient’s choices.",
        ("register", "unsupported_claim", "possessive", "cultural_fit"),
    ),
    SourceCase(
        "guesthouse-marketing", "hospitality", "marketing",
        "Settle in at your own pace, ask the hosts what is in season, and keep an evening free for whatever the village recommends.",
        ("cultural_fit", "rhythm", "idiom", "marketing_calque"),
    ),
    SourceCase(
        "mentor-programme-marketing", "professional_education", "marketing",
        "Bring a real challenge, work through it with an experienced mentor, and leave with an approach your team can continue using.",
        ("brand_voice", "register", "meaning_omission", "marketing_calque"),
    ),
    SourceCase(
        "community-stage-marketing", "cultural_nonprofit", "marketing",
        "The stage belongs to more than the people under the lights: join a season shaped with neighbours, volunteers and first-time performers.",
        ("cultural_fit", "inclusive_language", "rhythm", "brand_voice"),
    ),
    SourceCase(
        "freight-visibility-marketing", "logistics", "marketing",
        "Give dispatchers, drivers and customers the same clear picture of what has moved, what is waiting and what needs attention.",
        ("coordination", "information_structure", "terminology", "marketing_calque"),
    ),
    SourceCase(
        "basket-update-ui", "commerce", "ui",
        "Your basket changed while you were checking out. Review the updated items before paying.",
        ("concise_ui", "sequence", "payment_timing", "meaning_omission"),
    ),
    SourceCase(
        "identity-check-ui", "healthcare", "ui",
        "We could not verify your details. Nothing was submitted; check them and try again.",
        ("concise_ui", "negation", "register", "false_friend"),
    ),
    SourceCase(
        "deployment-window-ui", "developer_tools", "ui",
        "Deployment paused. Existing traffic is unaffected, and queued changes will resume when the window ends.",
        ("concise_ui", "terminology", "negation", "sequence"),
    ),
    SourceCase(
        "parcel-scan-ui", "logistics", "ui",
        "Scan not recognised. Keep the parcel here and enter the tracking code manually.",
        ("concise_ui", "negation", "imperative", "terminology"),
    ),
    SourceCase(
        "assessment-submit-ui", "professional_education", "ui",
        "Submit for review? You can still read your answers, but you cannot change them afterwards.",
        ("concise_ui", "modality", "negation", "sequence"),
    ),
    SourceCase(
        "booking-hold-ui", "travel", "ui",
        "This room is being held for another guest. Choose a different room or check again shortly.",
        ("concise_ui", "politeness", "temporary_state", "unnatural_cta"),
    ),
    SourceCase(
        "application-draft-ui", "public_services", "ui",
        "Draft saved. Your application has not been sent, and no deadline has been extended.",
        ("concise_ui", "negation", "legal_effect", "false_friend"),
    ),
    SourceCase(
        "reconciliation-documentation-long", "financial_services", "documentation",
        "Run reconciliation only after the bank feed has finished importing. First compare the opening balance with the closing balance from the previous confirmed period. Then review unmatched entries and record why any deliberate difference remains. Do not create a balancing transaction merely to make the totals agree, because that hides the source of the discrepancy. If another person is still editing the period, wait until their changes are committed and refresh the ledger before continuing. Lock the period only when every exception has an owner and supporting evidence.",
        ("long_context", "sequence", "negation", "terminology", "modality", "meaning_omission"),
    ),
    SourceCase(
        "patient-import-documentation-long", "healthcare", "documentation",
        "Before importing patient records, confirm that the export belongs to the intended organisation and that the permitted purpose covers every included field. Use the validation preview to map identifiers, dates and coded values without writing to the live record. Resolve duplicate people manually; a matching name alone is not sufficient evidence that two records describe the same person. When the preview is clean, schedule the import for a quiet period and keep the signed source manifest. If validation fails after writing begins, stop the batch, preserve the audit trail and follow the documented recovery procedure rather than starting a second import.",
        ("long_context", "sequence", "negation", "privacy", "terminology", "safety"),
    ),
    SourceCase(
        "handover-documentation", "logistics", "documentation",
        "Record the seal number before handover, ask the receiving driver to confirm it, and retain both timestamps with the shipment record.",
        ("sequence", "terminology", "completeness", "coordination"),
    ),
    SourceCase(
        "case-note-documentation", "public_services", "documentation",
        "Add a case note only for information relevant to the decision, identify its source, and correct an error without deleting the original entry.",
        ("sequence", "register", "auditability", "negation"),
    ),
    SourceCase(
        "catalogue-sync-documentation", "commerce", "documentation",
        "Publish the catalogue after prices and availability finish syncing; retry failed images separately instead of restarting the product import.",
        ("sequence", "terminology", "negation", "meaning_omission"),
    ),
    SourceCase(
        "meter-reset-documentation", "renewable_energy", "documentation",
        "Reset the comparison period without deleting meter history, then wait for the next complete reading before evaluating consumption.",
        ("sequence", "negation", "terminology", "false_friend"),
    ),
    SourceCase(
        "course-release-documentation", "professional_education", "documentation",
        "Release a module to the next group only after captions, reading order and assessment feedback have passed accessibility review.",
        ("sequence", "accessibility", "terminology", "modality"),
    ),
    SourceCase(
        "island-routes-seo", "travel", "seo",
        "Plan island walks, ferry connections and flexible overnight stops with practical local guidance for changing weather.",
        ("seo_naturalness", "cultural_fit", "keyword_stuffing", "unsupported_claim"),
    ),
    SourceCase(
        "team-context-seo", "business_software", "seo",
        "A shared project workspace for decisions, handovers and approvals that keeps useful context close to the work.",
        ("seo_naturalness", "marketing_calque", "keyword_stuffing", "register"),
    ),
    SourceCase(
        "cashflow-planning-seo", "financial_services", "seo",
        "Cash-flow planning for small organisations with clear scenarios, payment timing and collaborative review.",
        ("seo_naturalness", "terminology", "unsupported_claim", "false_friend"),
    ),
    SourceCase(
        "skills-course-seo", "professional_education", "seo",
        "Practical online courses with mentor feedback, accessible materials and projects drawn from everyday work.",
        ("seo_naturalness", "register", "keyword_stuffing", "unsupported_claim"),
    ),
    SourceCase(
        "parcel-tracking-seo", "logistics", "seo",
        "Parcel tracking that explains handovers, delays and next steps without hiding uncertainty from the customer.",
        ("seo_naturalness", "negation", "register", "marketing_calque"),
    ),
    SourceCase(
        "arts-volunteering-seo", "cultural_nonprofit", "seo",
        "Flexible arts volunteering opportunities for people who want to support events, workshops and local collections.",
        ("seo_naturalness", "inclusive_language", "cultural_fit", "keyword_stuffing"),
    ),
    SourceCase(
        "permit-support-seo", "public_services", "seo",
        "Plain-language permit guidance with document checklists, application stages and contact options for extra support.",
        ("seo_naturalness", "register", "unsupported_claim", "meaning_omission"),
    ),
    SourceCase(
        "privacy-choice-legal-long", "digital_services", "legal",
        "We use the contact details supplied with an enquiry to answer it and to keep a record of the response. We do not add those details to marketing lists unless the person separately chooses to receive updates. That choice may be withdrawn at any time without affecting messages already sent or the lawful handling that took place beforehand. Access to enquiry records is limited to staff who need them for support, compliance or security. Retention periods depend on the nature of the enquiry and any legal duty to preserve it. Requests to access, correct or erase personal data are assessed under the rights and exceptions that apply in the relevant jurisdiction.",
        ("privacy", "consent", "negation", "modality", "retention", "long_context"),
    ),
    SourceCase(
        "consumer-return-legal-long", "commerce", "legal",
        "Customers may notify us that they wish to withdraw from an eligible distance purchase during the statutory withdrawal period. The item must then be returned using the instructions provided, but opening packaging solely to inspect the item does not by itself remove the right to withdraw. A deduction may apply when handling goes beyond what is reasonably necessary to establish the item’s nature, characteristics and functioning. Refunds include the standard outbound delivery charge where required by law and are made using the original payment method unless another method is expressly agreed. Separate rules apply to customised goods, sealed hygiene products after unsealing and digital content supplied after valid prior consent.",
        ("consumer_rights", "exception", "modality", "negation", "refund", "long_context"),
    ),
    SourceCase(
        "telehealth-consent-legal-long", "healthcare", "legal",
        "A remote consultation is offered only when the clinician considers that the format is appropriate for the patient’s needs and the patient agrees to use it. The patient may ask to stop the remote session or request an in-person option, although availability and clinical urgency may affect when that option can be provided. Remote care does not replace emergency services. If the connection fails and there is an immediate safety concern, the patient should use the emergency contact information already provided. Notes from the consultation form part of the clinical record and are handled under the same confidentiality, access and retention rules as notes from an in-person appointment.",
        ("consent", "safety", "modality", "negation", "confidentiality", "long_context"),
    ),
    SourceCase(
        "seller-responsibility-legal-long", "online_marketplace", "legal",
        "Each seller is responsible for describing its offer accurately, identifying the trader where the law requires it and fulfilling accepted orders. The marketplace may remove an offer or restrict an account when there is a substantiated safety, fraud or legal concern, but it does not become the seller merely by providing the listing and payment tools. Buyers must receive the seller’s applicable terms before placing an order. A dispute process may help the parties exchange evidence and seek a resolution; it does not limit any mandatory right to contact a regulator, use an external dispute body or bring a legal claim. Repeated misuse of the dispute process may lead to proportionate restrictions after notice.",
        ("marketplace_role", "modality", "negation", "consumer_rights", "long_context"),
    ),
    SourceCase(
        "credit-information-legal-long", "financial_services", "legal",
        "The affordability estimate is based on the information available when it is produced and is not an offer of credit or a guarantee that an application will be approved. A lender may carry out additional identity, income, expenditure and creditworthiness checks before making a decision. The applicant must provide complete and accurate information and should explain any material change before accepting an agreement. Declining an application does not necessarily mean that the information supplied was false. Where the law permits an automated assessment, the notice will explain the principal factors and any available route to request human review or challenge inaccurate data.",
        ("financial_disclosure", "unsupported_claim", "modality", "negation", "automated_decision", "long_context"),
    ),
    SourceCase(
        "event-cancellation-legal-long", "cultural_events", "legal",
        "If an event is cancelled, the ticket holder may choose the refund or alternative offered in the cancellation notice, subject to any mandatory rights. A change of performer, running order or reasonable start time does not automatically amount to cancellation. When an event is postponed, the original ticket remains valid for the replacement date unless the notice states that a new ticket will be issued. Travel, accommodation and other indirect costs are not reimbursed by the organiser except where the law requires otherwise. Nothing in these terms excludes liability that cannot lawfully be excluded, and any stated claim period does not shorten a longer mandatory limitation period.",
        ("cancellation", "refund", "exception", "negation", "modality", "long_context"),
    ),
    SourceCase(
        "processor-instructions-legal-long", "cloud_services", "legal",
        "The service provider processes customer personal data only on documented instructions, including instructions concerning transfers, unless applicable law requires different processing. If legally permitted, the provider will tell the customer about that requirement before acting. People authorised to handle the data are bound by confidentiality, and access is limited according to role. The provider will assist with security incidents, rights requests and compliance information to the extent described in the agreement. At the end of the service, customer personal data is returned or deleted as instructed, except for copies that must be retained by law. Sub-processors remain subject to equivalent data-protection obligations and the agreed notification procedure.",
        ("data_processing", "confidentiality", "modality", "exception", "retention", "long_context"),
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
