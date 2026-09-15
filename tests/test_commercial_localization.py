from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from test_website_localization_worker import WORKER, PLANNER, ScriptedProvider, assets, candidate, job, review
from test_website_localization_fallback import RUNNER, Clock
from test_website_localization_release import (
    EVIDENCE_REQUEST_ID,
    EVIDENCE_REVISION,
    RELEASE,
    ExactReceiptVerifier,
    HmacAuthority,
    make_plan,
)


PROFILE = WORKER._COMMERCIAL
SCHEMA = PLANNER.COMMERCIAL_PROFILE
SOURCE = "Save up to €480 a year. All prices exclude VAT."
TARGET = "Spara upp till 480 € per år. Alla priser är exklusive moms."


def review_binding(locale="sv-SE"):
    profile = PLANNER.commercial_quality_profile_for(locale)
    return {
        "target_locale": locale,
        "commercial_quality_profile_version": profile["version"],
        "commercial_quality_profile_sha256": profile["sha256"],
    }


def validate_commercial(
    report, source, target, schema=SCHEMA, *, locale="sv-SE",
    allow_uncertain=False,
):
    return PROFILE.validate_review(
        report,
        source,
        target,
        schema,
        **review_binding(locale),
        allow_uncertain=allow_uncertain,
    )


def evidence_item(source, target, *, offer="offer-1", relation="matched"):
    return {
        "offer": offer,
        "relation": relation,
        "source_span": None if relation == "target_only" else [0, len(source)],
        "target_span": None if relation == "source_only" else [0, len(target)],
        "explanation": "Scripted protocol fixture; not independent linguistic evidence.",
    }


def dimension_evidence(status, items=None, *, offers=("offer-1",)):
    return {
        "status": status,
        "offer_statuses": [
            {"offer": offer, "status": status} for offer in offers
        ],
        "items": [] if items is None else items,
    }


def evidence(source=SOURCE, target=TARGET):
    checks = {
        name: dimension_evidence("not_present")
        for name in PROFILE.DIMENSIONS
    }
    for name in ("amount_currency", "discount_basis", "qualifiers", "tax_status", "offer_assignment"):
        checks[name] = dimension_evidence(
            "equivalent", [evidence_item(source, target)],
        )
    return {
        "schema": SCHEMA,
        "coverage": "complete",
        "offers": [{
            "id": "offer-1",
            "source_spans": [[0, len(source)]],
            "target_spans": [[0, len(target)]],
        }],
        "checks": checks,
    }


def provider(source=SOURCE, target=TARGET, locale="sv-SE", report=None):
    fidelity = review("source_fidelity", locale=locale)
    fidelity["commercial_review"] = evidence(source, target) if report is None else report
    return ScriptedProvider([candidate(target, locale), review("target_native", locale=locale), fidelity])


class CommercialLocalizationTests(unittest.TestCase):
    def run_worker(self, report=None):
        adapter = provider(report=report)
        return WORKER.run_localization_job(job(SOURCE, "commercial"), assets(), adapter), adapter

    def test_public_review_evidence_contract_is_exact_content_free_and_hashed(self):
        value = PROFILE.public_review_evidence_contract(SCHEMA)
        self.assertEqual(
            value["schema"], PROFILE.REVIEW_EVIDENCE_CAPABILITIES_SCHEMA,
        )
        self.assertEqual(value["result_schema"], SCHEMA)
        self.assertEqual(value["profile"], SCHEMA)
        self.assertEqual(
            value["required_fields"],
            ["schema", "coverage", "offers", "checks"],
        )
        self.assertEqual(
            value["checks"]["required_dimensions"],
            list(PROFILE.DIMENSIONS),
        )
        self.assertTrue(value["checks"]["exact_dimension_set"])
        self.assertEqual(value["offer_registry"]["max_items"], 1000)
        self.assertTrue(
            value["offer_registry"]["identifier"]["unique"],
        )
        self.assertEqual(
            value["offer_registry"]["regions"]["overlap"],
            "forbidden-within-and-across-offers",
        )
        self.assertTrue(
            value["offer_registry"]["regions"]["discontiguous"],
        )
        self.assertEqual(
            value["checks"]["item"]["span_containment"],
            "inside-named-offer-region",
        )
        self.assertEqual(
            value["checks"]["offer_statuses"]["coverage"],
            "exactly-one-per-registered-offer",
        )
        self.assertEqual(
            value["checks"]["offer_statuses"]["order"],
            "offer-registry-order",
        )
        self.assertEqual(
            value["checks"]["offer_assignment"]["equivalent"],
            "exactly-one-matched-item-per-registered-offer",
        )
        self.assertFalse(value["trust_boundary"]["semantic_truth"])
        self.assertFalse(
            value["trust_boundary"]["numeric_regex_semantic_proof"],
        )
        self.assertFalse(value["trust_boundary"]["publication_authority"])
        self.assertTrue(all(
            item is False for item in value["content_policy"].values()
        ))
        unsigned = dict(value)
        digest = unsigned.pop("sha256")
        self.assertEqual(
            digest,
            PROFILE.hashlib.sha256(PROFILE._canonical_json(unsigned)).hexdigest(),
        )
        serialized = json.dumps(value).lower()
        for private_value in ("480", '"vat"', '"blun"', '"offer-1"'):
            self.assertNotIn(private_value, serialized)

    def test_public_review_summary_contract_is_exact_content_free_and_hashed(self):
        value = PROFILE.public_review_summary_contract(SCHEMA)
        self.assertEqual(
            value["schema"], PROFILE.REVIEW_SUMMARY_CAPABILITIES_SCHEMA,
        )
        self.assertEqual(value["result_schema"], PROFILE.REVIEW_SUMMARY_SCHEMA)
        self.assertEqual(value["profile"], SCHEMA)
        self.assertEqual(
            value["review_required_dimensions"]["allowed"],
            list(PROFILE.DIMENSIONS),
        )
        self.assertEqual(
            value["review_required_dimensions"]["order"],
            list(PROFILE.DIMENSIONS),
        )
        self.assertEqual(
            value["review_required_offers"],
            {
                "item_required_fields": ["dimension", "offer_indexes"],
                "dimension_order": list(PROFILE.DIMENSIONS),
                "dimension_must_be_review_required": True,
                "offer_indexes": {
                    "meaning": "zero-based-opaque-offer-registry-position",
                    "minimum": 0,
                    "maximum_exclusive": 1000,
                    "order": "ascending",
                    "unique": True,
                },
                "configured_offer_identifiers_published": False,
            },
        )
        self.assertEqual(value["offer_count"], {
            "meaning": "opaque-offer-registry-size",
            "minimum": 0,
            "maximum": 1000,
        })
        evidence_contract_sha256 = PROFILE.public_review_evidence_contract(
            SCHEMA,
        )["sha256"]
        self.assertEqual(
            value["review_evidence_contract_sha256"],
            {
                "algorithm": "sha-256",
                "equals": evidence_contract_sha256,
                "purpose": "reject-stale-or-reinterpreted-private-evidence",
            },
        )
        self.assertEqual(
            value["evidence_sha256"],
            {
                "algorithm": "sha-256",
                "canonicalization": (
                    "utf-8-json-sort-keys-no-insignificant-whitespace"
                ),
                "binding_schema": PROFILE.EVIDENCE_BINDING_SCHEMA,
                "binding_fields": [
                    "schema", "profile",
                    "review_evidence_contract_sha256", "target_locale",
                    "commercial_quality_profile_version",
                    "commercial_quality_profile_sha256", "source_sha256",
                    "target_sha256", "evidence",
                ],
                "text_hashing": "exact-utf-8",
                "covers": [
                    "commercial-profile",
                    "exact-review-evidence-contract",
                    "exact-target-locale",
                    "commercial-quality-profile-generation",
                    "exact-source-sha256",
                    "exact-target-sha256",
                    "offer-registry-and-proposition-assignment",
                    "complete-commercial-review-evidence",
                ],
            },
        )
        unsigned = dict(value)
        digest = unsigned.pop("sha256")
        self.assertEqual(
            digest,
            PROFILE.hashlib.sha256(PROFILE._canonical_json(unsigned)).hexdigest(),
        )
        content_only = dict(value)
        content_only.pop("sha256")
        content_only.pop("review_evidence_contract_sha256")
        serialized = json.dumps(content_only).lower()
        for private_value in ("480", "vat", "blun", "offer-1"):
            self.assertNotIn(private_value, serialized)

    def test_public_review_routing_contract_is_exact_content_free_and_hashed(self):
        value = PROFILE.public_review_routing_contract(SCHEMA)
        self.assertEqual(
            value["schema"], PROFILE.REVIEW_ROUTING_CAPABILITIES_SCHEMA,
        )
        self.assertEqual(value["result_schema"], PROFILE.REVIEW_ROUTING_SCHEMA)
        self.assertEqual(value["profile"], SCHEMA)
        self.assertEqual(
            value["required_fields"],
            [
                "schema", "profile", "contract_sha256", "offer_count",
                "source_length", "target_length", "offers",
            ],
        )
        self.assertEqual(value["text_lengths"]["unit"], "unicode-code-points")
        self.assertTrue(
            value["text_lengths"]["must_equal_complete_texts"],
        )
        self.assertEqual(
            value["offers"]["coverage"],
            "exactly-one-per-registered-offer",
        )
        self.assertEqual(
            value["offers"]["regions"]["span_format"],
            "zero-based-unicode-code-points-exclusive-end",
        )
        self.assertEqual(
            value["offers"]["regions"]["overlap"],
            "forbidden-within-and-across-offers",
        )
        self.assertFalse(value["trust_boundary"]["semantic_truth"])
        self.assertFalse(
            value["trust_boundary"]["public_release_evidence"],
        )
        self.assertFalse(value["trust_boundary"]["publication_authority"])
        self.assertTrue(all(
            item is False for item in value["content_policy"].values()
        ))
        unsigned = dict(value)
        digest = unsigned.pop("sha256")
        self.assertEqual(
            digest,
            PROFILE.hashlib.sha256(PROFILE._canonical_json(unsigned)).hexdigest(),
        )
        serialized = json.dumps(value).lower()
        for private_value in ("480", '"vat"', '"blun"', '"offer-1"'):
            self.assertNotIn(private_value, serialized)

    def test_public_review_resolution_contract_is_exact_content_free_and_hashed(self):
        value = PROFILE.public_review_resolution_contract(SCHEMA)
        self.assertEqual(
            value["schema"], PROFILE.REVIEW_RESOLUTION_CAPABILITIES_SCHEMA,
        )
        self.assertEqual(value["result_schema"], PROFILE.REVIEW_RESOLUTION_SCHEMA)
        self.assertEqual(value["profile"], SCHEMA)
        self.assertEqual(value["status"], "resolved")
        self.assertEqual(
            value["applies_when"],
            {
                "review_summary_status": "review_required",
                "reviewed_dimensions": (
                    "exact-ordered-review-summary-dimensions"
                ),
                "reviewed_offer_count": (
                    "exact-review-summary-offer-count"
                ),
                "reviewed_offers": (
                    "exact-ordered-review-summary-offer-scope"
                ),
            },
        )
        self.assertEqual(
            value["reviewed_dimensions"],
            {
                "allowed": list(PROFILE.DIMENSIONS),
                "order": list(PROFILE.DIMENSIONS),
                "unique": True,
                "must_equal_review_summary": True,
            },
        )
        self.assertEqual(
            value["reviewed_offers"],
            {
                "must_equal_review_summary": True,
                "configured_offer_identifiers_published": False,
            },
        )
        self.assertEqual(value["reviewed_offer_count"], {
            "must_equal_review_summary": True,
        })
        self.assertEqual(
            value["methods"]["qualified_human"]["provider"], "null",
        )
        self.assertEqual(
            value["provider_identity"]["fields"],
            ["id", "model_id", "model_version"],
        )
        self.assertTrue(
            value["methods"]["independent_model"]
            ["provider_id_must_differ_from_primary_provider"],
        )
        self.assertFalse(value["provider_identity"]["credentials_published"])
        self.assertFalse(value["receipt_sha256"]["raw_receipt_published"])
        self.assertTrue(all(
            item is False for item in value["content_policy"].values()
        ))
        unsigned = dict(value)
        digest = unsigned.pop("sha256")
        self.assertEqual(
            digest,
            PROFILE.hashlib.sha256(PROFILE._canonical_json(unsigned)).hexdigest(),
        )
        serialized = json.dumps(value).lower()
        for private_value in ("480", "vat", "blun", "offer-1"):
            self.assertNotIn(private_value, serialized)

    def test_review_resolution_binds_contract_and_proves_model_independence(self):
        summary = {
            "schema": PROFILE.REVIEW_SUMMARY_SCHEMA,
            "profile": SCHEMA,
            "status": "review_required",
            "review_required_dimensions": ["tax_status", "cancellation"],
            "offer_count": 1,
            "review_required_offers": [
                {"dimension": "tax_status", "offer_indexes": [0]},
                {"dimension": "cancellation", "offer_indexes": [0]},
            ],
            "review_evidence_contract_sha256": (
                PROFILE.public_review_evidence_contract(SCHEMA)["sha256"]
            ),
            "evidence_sha256": "5" * 64,
        }
        contract_sha256 = PROFILE.public_review_resolution_contract(
            SCHEMA,
        )["sha256"]
        primary_provider = {
            "id": "customer-llm",
            "model_id": "king",
            "model_version": "2026-09-14",
        }
        resolution = {
            "schema": PROFILE.REVIEW_RESOLUTION_SCHEMA,
            "profile": SCHEMA,
            "contract_sha256": contract_sha256,
            "status": "resolved",
            "reviewed_dimensions": ["tax_status", "cancellation"],
            "reviewed_offer_count": 1,
            "reviewed_offers": [
                {"dimension": "tax_status", "offer_indexes": [0]},
                {"dimension": "cancellation", "offer_indexes": [0]},
            ],
            "method": "independent_model",
            "receipt_sha256": "7" * 64,
            "primary_provider": primary_provider,
            "provider": {
                "id": "independent-provider",
                "model_id": "review-model",
                "model_version": "2026-09-14",
            },
        }
        self.assertEqual(
            PROFILE.validate_review_resolution(resolution, summary, SCHEMA),
            resolution,
        )
        human = copy.deepcopy(resolution)
        human.update(method="qualified_human", provider=None)
        self.assertEqual(
            PROFILE.validate_review_resolution(human, summary, SCHEMA), human,
        )
        mutations = (
            lambda value: value.update(profile=SCHEMA + ".stale"),
            lambda value: value.update(contract_sha256="8" * 64),
            lambda value: value.update(primary_provider=None),
            lambda value: value["primary_provider"].update(model_id=""),
            lambda value: value["provider"].update(id="customer-llm"),
            lambda value: value["reviewed_offers"][0]
            ["offer_indexes"].append(1),
        )
        for mutation in mutations:
            changed = copy.deepcopy(resolution)
            mutation(changed)
            with self.subTest(changed=changed), self.assertRaises(
                PROFILE.CommercialReviewBlocked,
            ) as error:
                PROFILE.validate_review_resolution(changed, summary, SCHEMA)
            self.assertEqual(
                error.exception.code, "review.commercial.resolution_invalid",
            )

    def test_public_profile_requires_locale_bound_quality_in_all_phases(self):
        value = PROFILE.public_profile(SCHEMA)
        self.assertEqual(value["review_evidence_schema"], SCHEMA)
        self.assertEqual(
            value["review_evidence_contract"],
            PROFILE.public_review_evidence_contract(SCHEMA),
        )
        self.assertEqual(
            value["review_routing_schema"], PROFILE.REVIEW_ROUTING_SCHEMA,
        )
        self.assertEqual(
            value["review_routing_contract"],
            PROFILE.public_review_routing_contract(SCHEMA),
        )
        self.assertEqual(
            value["review_resolution_schema"],
            PROFILE.REVIEW_RESOLUTION_SCHEMA,
        )
        self.assertEqual(
            value["review_resolution_contract"],
            PROFILE.public_review_resolution_contract(SCHEMA),
        )
        contract = value["locale_quality_profile"]
        self.assertEqual(
            contract["schema"], PROFILE.COMMERCIAL_LOCALE_PROFILE_SCHEMA,
        )
        self.assertEqual(
            contract["schema"],
            PLANNER._QUALITY_PROFILES.COMMERCIAL_SCHEMA,
        )
        self.assertEqual(
            contract["required_commercial_checks"], list(PROFILE.DIMENSIONS),
        )
        self.assertEqual(
            contract["provider_phases"], list(WORKER.PHASES),
        )
        self.assertEqual(
            contract["rendering_reference"],
            {
                "schema": PROFILE.COMMERCIAL_RENDERING_REFERENCE_SCHEMA,
                "authority": "Unicode CLDR",
                "version": "48",
                "purpose": "target-locale-rendering-guidance",
                "semantic_proof": False,
                "unresolved_route": (
                    "independent-model-or-qualified-native-domain-review"
                ),
            },
        )
        self.assertIn("rendering_reference", contract["binding_fields"])
        self.assertEqual(
            tuple(PROFILE.DIMENSIONS),
            PLANNER._QUALITY_PROFILES.COMMERCIAL_REVIEW_CHECKS,
        )
        self.assertEqual(
            value["verification"]["offer_registry"],
            {
                "identifiers": "unique",
                "source_and_target_regions": "ordered-non-overlapping",
                "discontiguous_regions_allowed": True,
                "every_proposition_contained_in_declared_offer": True,
                "every_offer_has_exactly_one_assignment_item": True,
                "every_dimension_has_exactly_one_status_per_offer": True,
            },
        )
        unsigned = dict(value)
        digest = unsigned.pop("sha256")
        self.assertEqual(
            digest,
            PROFILE.hashlib.sha256(PROFILE._canonical_json(unsigned)).hexdigest(),
        )

    def test_ordered_review_preserves_source_blindness_and_hashes_full_evidence(self):
        result, adapter = self.run_worker()
        self.assertEqual([r.phase for r in adapter.requests], list(WORKER.PHASES))
        expected_commercial_quality = PLANNER.commercial_quality_profile_for(
            "sv-SE"
        )
        for request in adapter.requests:
            self.assertEqual(
                request.input["commercial_quality_profile"],
                expected_commercial_quality,
            )
        native = json.dumps(adapter.requests[1].as_payload())
        self.assertNotIn(SOURCE, native)
        self.assertNotIn('"source_span"', native)
        self.assertNotIn('"commercial_review"', native)
        self.assertNotIn("commercial_review_evidence_contract", native)
        self.assertNotIn(
            "commercial_review_evidence_contract",
            adapter.requests[0].input,
        )
        fidelity = adapter.requests[2]
        self.assertEqual(fidelity.input["source"]["text"], SOURCE)
        evidence_contract = PROFILE.public_review_evidence_contract(SCHEMA)
        self.assertEqual(
            job(SOURCE, "commercial")[
                "commercial_review_evidence_contract_sha256"
            ],
            evidence_contract["sha256"],
        )
        self.assertEqual(
            job(SOURCE, "commercial")[
                "commercial_review_routing_contract_sha256"
            ],
            PROFILE.public_review_routing_contract(SCHEMA)["sha256"],
        )
        self.assertEqual(
            fidelity.input["commercial_review_evidence_contract"],
            evidence_contract,
        )
        changed_input = copy.deepcopy(fidelity.input)
        changed_input["commercial_review_evidence_contract"]["sha256"] = (
            "0" * 64
        )
        changed_request = WORKER._request(
            job(SOURCE, "commercial"),
            "source_fidelity",
            fidelity.system_instruction,
            changed_input,
        )
        self.assertNotEqual(changed_request.request_id, fidelity.request_id)
        self.assertEqual(set(fidelity.input["response_schema"]["commercial_review"]["checks"]), set(PROFILE.DIMENSIONS))
        self.assertEqual(
            set(fidelity.input["response_schema"]["commercial_review"]),
            {"schema", "coverage", "offers", "checks"},
        )
        response = review("source_fidelity")
        response["commercial_review"] = evidence()
        self.assertEqual(result["quality_passes"][2]["response_sha256"], WORKER._hash_json(response))
        summary = result["commercial_review"]
        self.assertEqual(summary["status"], "verified")
        self.assertEqual(summary["review_required_dimensions"], [])
        self.assertEqual(
            summary["review_evidence_contract_sha256"],
            evidence_contract["sha256"],
        )
        self.assertEqual(
            summary["evidence_sha256"],
            PROFILE.evidence_sha256(
                evidence(), SOURCE, TARGET, SCHEMA, **review_binding(),
            ),
        )
        summary_without_digest = dict(summary)
        summary_without_digest.pop("evidence_sha256")
        self.assertNotIn("480", json.dumps(summary_without_digest))
        self.assertNotIn("VAT", json.dumps(summary_without_digest))
        self.assertTrue(result["release_required"])
        self.assertEqual(result["quality_profile"]["commercial"], {
            "profile": SCHEMA,
            "version": expected_commercial_quality["version"],
            "sha256": expected_commercial_quality["sha256"],
        })
        self.assertEqual(
            result["commercial_review_routing_contract_sha256"],
            PROFILE.public_review_routing_contract(SCHEMA)["sha256"],
        )

    def test_commercial_evidence_contract_drift_blocks_before_provider(self):
        canonical = PROFILE.public_review_evidence_contract(SCHEMA)
        altered = copy.deepcopy(canonical)
        altered["trust_boundary"]["publication_authority"] = True
        unsigned = dict(altered)
        unsigned.pop("sha256")
        altered["sha256"] = PROFILE.hashlib.sha256(
            PROFILE._canonical_json(unsigned)
        ).hexdigest()
        adapter = provider()
        with patch.object(
            WORKER._COMMERCIAL,
            "public_review_evidence_contract",
            return_value=altered,
        ), self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(
                job(SOURCE, "commercial"), assets(), adapter,
            )
        self.assertEqual(
            error.exception.code,
            "commercial_review_evidence_contract.binding_mismatch",
        )
        self.assertFalse(error.exception.retryable)
        self.assertEqual(adapter.requests, [])

    def test_stale_review_evidence_contract_blocks_persisted_summary(self):
        summary = validate_commercial(evidence(), SOURCE, TARGET, SCHEMA)
        summary["review_evidence_contract_sha256"] = "0" * 64
        with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
            PROFILE.validate_summary(
                summary,
                SCHEMA,
                review_required=False,
            )
        self.assertEqual(
            error.exception.code,
            "review.commercial.summary_invalid",
        )

    def test_review_digest_binds_exact_source_target_profile_and_evidence(self):
        report = evidence()
        baseline = validate_commercial(report, SOURCE, TARGET, SCHEMA)
        digests = {
            baseline["evidence_sha256"],
            validate_commercial(
                report, SOURCE + " ", TARGET, SCHEMA,
            )["evidence_sha256"],
            validate_commercial(
                report, SOURCE, TARGET + " ", SCHEMA,
            )["evidence_sha256"],
        }
        changed_profile = SCHEMA + ".next"
        changed_report = copy.deepcopy(report)
        changed_report["schema"] = changed_profile
        digests.add(validate_commercial(
            changed_report, SOURCE, TARGET, changed_profile,
        )["evidence_sha256"])
        changed_evidence = copy.deepcopy(report)
        changed_evidence["checks"]["amount_currency"]["items"][0][
            "explanation"
        ] += " Exact semantic interpretation changed."
        digests.add(validate_commercial(
            changed_evidence, SOURCE, TARGET, SCHEMA,
        )["evidence_sha256"])
        digests.add(validate_commercial(
            report, SOURCE, TARGET, SCHEMA, locale="fi-FI",
        )["evidence_sha256"])
        binding = review_binding()
        digests.add(PROFILE.validate_review(
            report,
            SOURCE,
            TARGET,
            SCHEMA,
            **{
                **binding,
                "commercial_quality_profile_version": (
                    binding["commercial_quality_profile_version"] + ".next"
                ),
            },
        )["evidence_sha256"])
        digests.add(PROFILE.validate_review(
            report,
            SOURCE,
            TARGET,
            SCHEMA,
            **{
                **binding,
                "commercial_quality_profile_sha256": "0" * 64,
            },
        )["evidence_sha256"])
        self.assertEqual(len(digests), 8)

    def test_review_rejects_missing_or_malformed_locale_profile_binding(self):
        binding = review_binding()
        mutations = (
            {**binding, "target_locale": "all"},
            {**binding, "target_locale": "sv_SE"},
            {**binding, "commercial_quality_profile_version": " stale "},
            {**binding, "commercial_quality_profile_sha256": "G" * 64},
        )
        for changed in mutations:
            with self.subTest(binding=changed), self.assertRaises(
                PROFILE.CommercialReviewBlocked,
            ) as error:
                PROFILE.validate_review(
                    evidence(), SOURCE, TARGET, SCHEMA, **changed,
                )
            self.assertEqual(error.exception.code, "review.commercial.invalid")

    def test_all_eu_locales_receive_profile_without_source_language_translation(self):
        plan = PLANNER.plan_website_localization(
            source_id="pricing", source_revision="1", source_text=SOURCE,
            source_locale="en-IE", content_type="commercial", glossary_version="g1",
            policy_version="p1", provider_id="own-model", model_id="model",
            model_version="1", software_version="1",
        )
        self.assertEqual(len(plan.jobs), 23)
        self.assertNotIn("en-IE", [j.target.locale for j in plan.jobs])
        for locale in PLANNER.EU_OFFICIAL_LOCALES:
            with self.subTest(locale=locale.locale):
                # This tests routing/protocol, not native fluency: use synthetic
                # target strings and stub only the unrelated linguistic guard.
                source_locale = "de-AT" if locale.locale == "en-IE" else "en-IE"
                payload = PLANNER.plan_website_localization(
                    source_id="pricing", source_revision="1", source_text=SOURCE,
                    source_locale=source_locale, content_type="commercial",
                    glossary_version="blun-glossary-3", policy_version="native-web-1",
                    provider_id="own-model", model_id="model", model_version="1",
                    software_version="1", target_locales=[locale.locale],
                ).jobs[0].as_payload()
                adapter = provider(locale=locale.locale)
                with patch.object(WORKER, "_integrity_errors", return_value=[]):
                    result = WORKER.run_localization_job(payload, assets(), adapter)
                self.assertEqual(result["target_locale"], locale.locale)
                self.assertEqual(payload["commercial_profile"], SCHEMA)
                locale_profile = PLANNER.commercial_quality_profile_for(
                    locale.locale
                )
                self.assertEqual(
                    payload["commercial_quality_profile"], locale_profile,
                )
                for request in adapter.requests:
                    self.assertEqual(
                        request.input["commercial_quality_profile"],
                        locale_profile,
                    )

    def test_profile_changes_invalidate_plan_and_job_ids(self):
        before = job(SOURCE, "commercial")
        with patch.object(PLANNER, "COMMERCIAL_PROFILE", "translate-native.commercial.v14"):
            after = job(SOURCE, "commercial")
        self.assertNotEqual(before["job_id"], after["job_id"])
        self.assertNotEqual(before["commercial_profile"], after["commercial_profile"])
        self.assertNotEqual(
            before["commercial_quality_profile"]["sha256"],
            after["commercial_quality_profile"]["sha256"],
        )
        with self.assertRaises(WORKER.LocalizationWorkerBlocked):
            WORKER._validated_job(after)
        self.assertNotIn("commercial_profile", job(SOURCE, "marketing"))
        self.assertNotIn("commercial_quality_profile", job(SOURCE, "marketing"))
        self.assertNotIn(
            "commercial_review_evidence_contract_sha256",
            job(SOURCE, "marketing"),
        )
        self.assertNotIn(
            "commercial_review_routing_contract_sha256",
            job(SOURCE, "marketing"),
        )
        self.assertNotIn(
            "commercial_review_resolution_contract_sha256",
            job(SOURCE, "marketing"),
        )

    def test_evidence_contract_only_change_invalidates_plan_and_job_ids(self):
        before_plan = PLANNER.plan_website_localization(
            source_id="pricing", source_revision="1", source_text=SOURCE,
            source_locale="en-IE", content_type="commercial",
            glossary_version="g1", policy_version="p1",
            provider_id="own-model", model_id="model", model_version="1",
            software_version="1", target_locales=["sv-SE"],
        )
        before = before_plan.jobs[0].as_payload()
        altered = copy.deepcopy(
            PLANNER._COMMERCIAL.public_review_evidence_contract(SCHEMA)
        )
        altered["trust_boundary"]["publication_authority"] = True
        unsigned = dict(altered)
        unsigned.pop("sha256")
        altered["sha256"] = PLANNER._digest(unsigned)
        public_profile = copy.deepcopy(
            PLANNER._COMMERCIAL.public_profile(SCHEMA)
        )
        public_profile["review_evidence_contract"] = altered
        with patch.object(
            PLANNER._COMMERCIAL,
            "public_review_evidence_contract",
            return_value=altered,
        ), patch.object(
            PLANNER._COMMERCIAL,
            "public_profile",
            return_value=public_profile,
        ):
            after_plan = PLANNER.plan_website_localization(
                source_id="pricing", source_revision="1", source_text=SOURCE,
                source_locale="en-IE", content_type="commercial",
                glossary_version="g1", policy_version="p1",
                provider_id="own-model", model_id="model", model_version="1",
                software_version="1", target_locales=["sv-SE"],
            )
            after = after_plan.jobs[0].as_payload()
        self.assertEqual(before["commercial_profile"], after["commercial_profile"])
        self.assertEqual(
            before["commercial_quality_profile"],
            after["commercial_quality_profile"],
        )
        self.assertNotEqual(
            before["commercial_review_evidence_contract_sha256"],
            after["commercial_review_evidence_contract_sha256"],
        )
        self.assertNotEqual(before["job_id"], after["job_id"])
        self.assertNotEqual(before_plan.plan_id, after_plan.plan_id)
        adapter = provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(after, assets(), adapter)
        self.assertEqual(error.exception.code, "job.binding_mismatch")
        self.assertEqual(adapter.requests, [])

    def test_commercial_evidence_contract_job_tamper_blocks_before_provider(self):
        payload = job(SOURCE, "commercial")
        payload["commercial_review_evidence_contract_sha256"] = "0" * 64
        adapter = provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(payload, assets(), adapter)
        self.assertEqual(error.exception.code, "job.binding_mismatch")
        self.assertEqual(adapter.requests, [])

    def test_routing_contract_only_change_invalidates_plan_and_job_ids(self):
        before_plan = PLANNER.plan_website_localization(
            source_id="pricing", source_revision="1", source_text=SOURCE,
            source_locale="en-IE", content_type="commercial",
            glossary_version="g1", policy_version="p1",
            provider_id="own-model", model_id="model", model_version="1",
            software_version="1", target_locales=["sv-SE"],
        )
        before = before_plan.jobs[0].as_payload()
        altered = copy.deepcopy(
            PLANNER._COMMERCIAL.public_review_routing_contract(SCHEMA)
        )
        altered["trust_boundary"]["publication_authority"] = True
        unsigned = dict(altered)
        unsigned.pop("sha256")
        altered["sha256"] = PLANNER._digest(unsigned)
        public_profile = copy.deepcopy(
            PLANNER._COMMERCIAL.public_profile(SCHEMA)
        )
        public_profile["review_routing_contract"] = altered
        with patch.object(
            PLANNER._COMMERCIAL,
            "public_review_routing_contract",
            return_value=altered,
        ), patch.object(
            PLANNER._COMMERCIAL,
            "public_profile",
            return_value=public_profile,
        ):
            after_plan = PLANNER.plan_website_localization(
                source_id="pricing", source_revision="1", source_text=SOURCE,
                source_locale="en-IE", content_type="commercial",
                glossary_version="g1", policy_version="p1",
                provider_id="own-model", model_id="model", model_version="1",
                software_version="1", target_locales=["sv-SE"],
            )
            after = after_plan.jobs[0].as_payload()
        self.assertEqual(before["commercial_profile"], after["commercial_profile"])
        self.assertEqual(
            before["commercial_review_evidence_contract_sha256"],
            after["commercial_review_evidence_contract_sha256"],
        )
        self.assertNotEqual(
            before["commercial_review_routing_contract_sha256"],
            after["commercial_review_routing_contract_sha256"],
        )
        self.assertNotEqual(before["job_id"], after["job_id"])
        self.assertNotEqual(before_plan.plan_id, after_plan.plan_id)
        adapter = provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(after, assets(), adapter)
        self.assertEqual(error.exception.code, "job.binding_mismatch")
        self.assertEqual(adapter.requests, [])

    def test_commercial_routing_contract_job_tamper_blocks_before_provider(self):
        payload = job(SOURCE, "commercial")
        payload["commercial_review_routing_contract_sha256"] = "0" * 64
        adapter = provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(payload, assets(), adapter)
        self.assertEqual(error.exception.code, "job.binding_mismatch")
        self.assertEqual(adapter.requests, [])

    def test_resolution_contract_only_change_invalidates_plan_and_job_ids(self):
        before_plan = PLANNER.plan_website_localization(
            source_id="pricing", source_revision="1", source_text=SOURCE,
            source_locale="en-IE", content_type="commercial",
            glossary_version="g1", policy_version="p1",
            provider_id="own-model", model_id="model", model_version="1",
            software_version="1", target_locales=["sv-SE"],
        )
        before = before_plan.jobs[0].as_payload()
        altered = copy.deepcopy(
            PLANNER._COMMERCIAL.public_review_resolution_contract(SCHEMA)
        )
        altered["status"] = "verified"
        unsigned = dict(altered)
        unsigned.pop("sha256")
        altered["sha256"] = PLANNER._digest(unsigned)
        public_profile = copy.deepcopy(
            PLANNER._COMMERCIAL.public_profile(SCHEMA)
        )
        public_profile["review_resolution_contract"] = altered
        with patch.object(
            PLANNER._COMMERCIAL,
            "public_review_resolution_contract",
            return_value=altered,
        ), patch.object(
            PLANNER._COMMERCIAL,
            "public_profile",
            return_value=public_profile,
        ):
            after_plan = PLANNER.plan_website_localization(
                source_id="pricing", source_revision="1", source_text=SOURCE,
                source_locale="en-IE", content_type="commercial",
                glossary_version="g1", policy_version="p1",
                provider_id="own-model", model_id="model", model_version="1",
                software_version="1", target_locales=["sv-SE"],
            )
            after = after_plan.jobs[0].as_payload()
        self.assertEqual(before["commercial_profile"], after["commercial_profile"])
        self.assertEqual(
            before["commercial_review_evidence_contract_sha256"],
            after["commercial_review_evidence_contract_sha256"],
        )
        self.assertEqual(
            before["commercial_review_routing_contract_sha256"],
            after["commercial_review_routing_contract_sha256"],
        )
        self.assertNotEqual(
            before["commercial_review_resolution_contract_sha256"],
            after["commercial_review_resolution_contract_sha256"],
        )
        self.assertNotEqual(before["job_id"], after["job_id"])
        self.assertNotEqual(before_plan.plan_id, after_plan.plan_id)
        adapter = provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(after, assets(), adapter)
        self.assertEqual(error.exception.code, "job.binding_mismatch")
        self.assertEqual(adapter.requests, [])

    def test_commercial_resolution_contract_job_tamper_blocks_before_provider(self):
        payload = job(SOURCE, "commercial")
        payload["commercial_review_resolution_contract_sha256"] = "0" * 64
        adapter = provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(payload, assets(), adapter)
        self.assertEqual(error.exception.code, "job.binding_mismatch")
        self.assertEqual(adapter.requests, [])

    def test_commercial_routing_contract_drift_blocks_before_provider(self):
        canonical = PROFILE.public_review_routing_contract(SCHEMA)
        altered = copy.deepcopy(canonical)
        altered["trust_boundary"]["publication_authority"] = True
        unsigned = dict(altered)
        unsigned.pop("sha256")
        altered["sha256"] = PROFILE.hashlib.sha256(
            PROFILE._canonical_json(unsigned)
        ).hexdigest()
        adapter = provider()
        with patch.object(
            WORKER._COMMERCIAL,
            "public_review_routing_contract",
            return_value=altered,
        ), self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(
                job(SOURCE, "commercial"), assets(), adapter,
            )
        self.assertEqual(
            error.exception.code,
            "commercial_review_routing_contract.binding_mismatch",
        )
        self.assertFalse(error.exception.retryable)
        self.assertEqual(adapter.requests, [])

    def test_commercial_locale_profile_tamper_blocks_before_provider(self):
        for mutate in (
            lambda value: value.update(locale="fi-FI"),
            lambda value: value.update(version="stale"),
            lambda value: value.update(sha256="0" * 64),
            lambda value: value["native_review_focus"].append("weakened"),
            lambda value: value["rendering_reference"]["patterns"].update(
                currency="#,##0.00 ¤"
            ),
        ):
            payload = job(SOURCE, "commercial")
            mutate(payload["commercial_quality_profile"])
            adapter = provider()
            with self.subTest(payload=payload), self.assertRaises(
                WORKER.LocalizationWorkerBlocked
            ) as error:
                WORKER.run_localization_job(payload, assets(), adapter)
            self.assertEqual(error.exception.code, "job.binding_mismatch")
            self.assertEqual(adapter.requests, [])

    def test_each_dimension_blocks_known_changes_and_routes_uncertainty(self):
        for dimension in PROFILE.DIMENSIONS:
            report = evidence()
            report["checks"][dimension] = dimension_evidence(
                "changed", [evidence_item(SOURCE, TARGET)],
            )
            with self.subTest(dimension=dimension, verdict="changed"):
                with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
                    self.run_worker(report)
                self.assertEqual(error.exception.code, "review.commercial.changed")
                self.assertFalse(error.exception.retryable)

            report = evidence()
            report["checks"][dimension] = dimension_evidence(
                "uncertain", [evidence_item(SOURCE, TARGET)],
            )
            with self.subTest(dimension=dimension, verdict="uncertain"):
                result, _ = self.run_worker(report)
                self.assertEqual(result["review_confidence"]["source_fidelity"], "low")
                self.assertTrue(result["independent_review_required"])
                self.assertEqual(
                    result["commercial_review"]["review_required_dimensions"],
                    [dimension],
                )

    def test_malformed_evidence_blocks_while_unresolved_coverage_routes_to_review(self):
        mutations = []
        report = evidence(); del report["checks"]["renewal"]; mutations.append(report)
        report = evidence(); report["checks"]["tax_status"]["items"] = []; mutations.append(report)
        report = evidence(); report["schema"] = "old"; mutations.append(report)
        for report in mutations:
            with self.subTest(report=report), self.assertRaises(WORKER.LocalizationWorkerBlocked):
                self.run_worker(report)
        for report in (
            {**evidence(), "coverage": "uncertain"},
            {
                "schema": SCHEMA,
                "coverage": "complete",
                "offers": [],
                "checks": {
                    name: dimension_evidence("not_present", offers=())
                    for name in PROFILE.DIMENSIONS
                },
            },
        ):
            with self.subTest(report=report):
                result, _ = self.run_worker(report)
                self.assertTrue(result["independent_review_required"])
                self.assertEqual(result["review_confidence"]["source_fidelity"], "low")
                self.assertEqual(
                    result["commercial_review"]["review_required_dimensions"],
                    list(PROFILE.DIMENSIONS),
                )

    def test_invalid_offsets_types_and_duplicate_evidence_block(self):
        for offsets in ([True, 2], [-1, 3], [0, 99999], [2, 2], [2, 1], "0:3", [0, 1.5]):
            report = evidence()
            report["checks"]["amount_currency"]["items"][0]["target_span"] = offsets
            with self.subTest(offsets=offsets), self.assertRaises(WORKER.LocalizationWorkerBlocked):
                self.run_worker(report)
        report = evidence()
        items = report["checks"]["amount_currency"]["items"]
        items.append(copy.deepcopy(items[0]))
        with self.assertRaises(WORKER.LocalizationWorkerBlocked):
            self.run_worker(report)

    def test_directional_evidence_represents_omissions_and_additions(self):
        source = "Standard costs €29 monthly. Cancel with 30 days' notice."
        target = "Standard kostar 29 € per månad. Priority kostar 99 € per månad."

        addition = evidence(source, target)
        added_start = target.index("Priority")
        addition["checks"]["amount_currency"] = dimension_evidence(
            "changed", [{
                "offer": "offer-1",
                "relation": "target_only",
                "source_span": None,
                "target_span": [added_start, len(target)],
                "explanation": "The target adds a price absent from the source.",
            }],
        )
        with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
            validate_commercial(addition, source, target, SCHEMA)
        self.assertEqual(error.exception.code, "review.commercial.changed")

        omission = evidence(source, target)
        omitted_start = source.index("Cancel")
        omission["checks"]["cancellation"] = dimension_evidence(
            "uncertain", [{
                "offer": "offer-1",
                "relation": "source_only",
                "source_span": [omitted_start, len(source)],
                "target_span": None,
                "explanation": "The source condition has no identified target counterpart.",
            }],
        )
        with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
            validate_commercial(omission, source, target, SCHEMA)
        self.assertEqual(
            error.exception.code, "review.commercial.independent_review_required",
        )

    def test_directional_evidence_rejects_impossible_span_combinations(self):
        malformed_items = (
            {**evidence_item(SOURCE, TARGET), "relation": "unknown"},
            {**evidence_item(SOURCE, TARGET), "source_span": None},
            {**evidence_item(SOURCE, TARGET, relation="source_only"), "target_span": [0, 1]},
            {**evidence_item(SOURCE, TARGET, relation="target_only"), "source_span": [0, 1]},
        )
        for item in malformed_items:
            report = evidence()
            report["checks"]["amount_currency"] = {
                "status": "changed", "items": [item],
            }
            with self.subTest(item=item), self.assertRaises(
                PROFILE.CommercialReviewBlocked,
            ) as error:
                validate_commercial(report, SOURCE, TARGET, SCHEMA)
            self.assertEqual(error.exception.code, "review.commercial.invalid")

        for status in ("changed", "uncertain"):
            report = evidence()
            report["checks"]["amount_currency"] = {"status": status, "items": []}
            with self.subTest(status=status), self.assertRaises(
                PROFILE.CommercialReviewBlocked,
            ) as error:
                validate_commercial(report, SOURCE, TARGET, SCHEMA)
            self.assertEqual(error.exception.code, "review.commercial.invalid")

        report = evidence()
        report["checks"]["amount_currency"] = {
            "status": "equivalent",
            "items": [evidence_item(SOURCE, TARGET, relation="source_only")],
        }
        with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
            validate_commercial(report, SOURCE, TARGET, SCHEMA)
        self.assertEqual(error.exception.code, "review.commercial.invalid")

    def test_conditions_require_reviewed_offer_association(self):
        report = evidence()
        report["checks"]["amount_currency"]["items"][0]["offer"] = "unreviewed-offer"
        with self.assertRaises(WORKER.LocalizationWorkerBlocked):
            self.run_worker(report)
        report = evidence()
        report["checks"]["offer_assignment"] = dimension_evidence(
            "not_present",
        )
        result, _ = self.run_worker(report)
        self.assertTrue(result["independent_review_required"])
        self.assertEqual(result["review_confidence"]["source_fidelity"], "low")

    def test_offer_registry_rejects_cross_offer_and_overlapping_regions(self):
        source = (
            "Basic: €10 monthly.\nPro: €20 monthly.\n"
            "* Basic offer terms apply."
        )
        target = (
            "Basic: 10 € per månad.\nPro: 20 € per månad.\n"
            "* Villkoren för Basic-erbjudandet gäller."
        )
        source_lines = source.splitlines()
        target_lines = target.splitlines()
        source_spans = (
            [0, len(source_lines[0])],
            [
                len(source_lines[0]) + 1,
                len(source_lines[0]) + 1 + len(source_lines[1]),
            ],
        )
        target_spans = (
            [0, len(target_lines[0])],
            [
                len(target_lines[0]) + 1,
                len(target_lines[0]) + 1 + len(target_lines[1]),
            ],
        )
        report = evidence(source, target)
        report["offers"] = [
            {
                "id": offer,
                "source_spans": [source_span] + (
                    [[len(source) - len(source_lines[2]), len(source)]]
                    if offer == "basic" else []
                ),
                "target_spans": [target_span] + (
                    [[len(target) - len(target_lines[2]), len(target)]]
                    if offer == "basic" else []
                ),
            }
            for offer, source_span, target_span in zip(
                ("basic", "pro"), source_spans, target_spans,
            )
        ]
        items = [
            {
                "offer": offer,
                "relation": "matched",
                "source_span": source_span,
                "target_span": target_span,
                "explanation": "Scripted offer-bound proposition fixture.",
            }
            for offer, source_span, target_span in zip(
                ("basic", "pro"), source_spans, target_spans,
            )
        ]
        for check in report["checks"].values():
            check["offer_statuses"] = [
                {"offer": offer, "status": check["status"]}
                for offer in ("basic", "pro")
            ]
            if check["status"] == "equivalent":
                check["items"] = copy.deepcopy(items)
        validate_commercial(report, source, target, SCHEMA)

        targeted = copy.deepcopy(report)
        targeted["checks"]["tax_status"]["status"] = "uncertain"
        targeted["checks"]["tax_status"]["offer_statuses"][1][
            "status"
        ] = "uncertain"
        summary = validate_commercial(
            targeted, source, target, SCHEMA, allow_uncertain=True,
        )
        self.assertEqual(summary["review_required_dimensions"], ["tax_status"])
        self.assertEqual(summary["offer_count"], 2)
        self.assertEqual(summary["review_required_offers"], [{
            "dimension": "tax_status", "offer_indexes": [1],
        }])
        routing = PROFILE.review_routing_context(
            targeted, source, target, summary, SCHEMA,
        )
        self.assertEqual(routing, {
            "schema": PROFILE.REVIEW_ROUTING_SCHEMA,
            "profile": SCHEMA,
            "contract_sha256": (
                PROFILE.public_review_routing_contract(SCHEMA)["sha256"]
            ),
            "offer_count": 2,
            "source_length": len(source),
            "target_length": len(target),
            "offers": [
                {
                    "offer_index": index,
                    "source_spans": targeted["offers"][index]["source_spans"],
                    "target_spans": targeted["offers"][index]["target_spans"],
                }
                for index in range(2)
            ],
        })
        serialized_routing = json.dumps(routing, ensure_ascii=False)
        for private_value in (
            '"basic"', '"pro"',
            "Scripted offer-bound proposition fixture.",
        ):
            self.assertNotIn(private_value, serialized_routing)

        for mutation in (
            lambda value: value.update(contract_sha256="0" * 64),
            lambda value: value["offers"].reverse(),
            lambda value: value["offers"][1]["source_spans"].__setitem__(
                0, [source_spans[0][1] - 1, source_spans[1][1]],
            ),
            lambda value: value["offers"][0].update(id="basic"),
        ):
            malformed_routing = copy.deepcopy(routing)
            mutation(malformed_routing)
            with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
                PROFILE.validate_review_routing_context(
                    malformed_routing, source, target, summary, SCHEMA,
                )
            self.assertEqual(
                error.exception.code, "review.commercial.routing_invalid",
            )

        malformed_scope = copy.deepcopy(summary)
        malformed_scope["review_required_offers"][0]["offer_indexes"] = [1, 0]
        with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
            PROFILE.validate_summary(
                malformed_scope, SCHEMA, review_required=True,
            )
        self.assertEqual(error.exception.code, "review.commercial.summary_invalid")

        missing_proposition = copy.deepcopy(report)
        missing_proposition["checks"]["amount_currency"]["items"].pop()
        with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
            validate_commercial(
                missing_proposition, source, target, SCHEMA,
            )
        self.assertEqual(error.exception.code, "review.commercial.invalid")

        mutations = (
            lambda value: value["checks"]["amount_currency"]["items"][0]
            .update(offer="pro"),
            lambda value: value["offers"][1]["source_spans"]
            .__setitem__(0, [source_spans[0][1] - 1, source_spans[1][1]]),
            lambda value: value["checks"]["offer_assignment"]["items"].pop(),
            lambda value: value["offers"][1].update(id="basic"),
            lambda value: value["checks"]["amount_currency"]
            ["offer_statuses"].reverse(),
            lambda value: value["checks"]["amount_currency"]
            ["offer_statuses"].pop(),
            lambda value: value["checks"]["amount_currency"].update(
                status="not_present",
            ),
            lambda value: value["checks"]["amount_currency"]
            ["offer_statuses"][1].update(status="not_present"),
        )
        for mutation in mutations:
            changed = copy.deepcopy(report)
            mutation(changed)
            with self.subTest(changed=changed), self.assertRaises(
                PROFILE.CommercialReviewBlocked,
            ) as error:
                validate_commercial(changed, source, target, SCHEMA)
            self.assertEqual(error.exception.code, "review.commercial.invalid")

    def test_no_numeric_regex_rejects_semantically_reviewed_native_forms(self):
        # Contract-level fixtures, not claims of independent native approval.
        for source, target in (
            ("4 tiers", "four tiers"), ("20%", "twenty percent"),
            ("12 months", "one year"), ("480", "٤٨٠"),
            ("1,234.50 €", "1.234,50 €"), ("€480", "480\u00a0€"),
        ):
            with self.subTest(target=target):
                validate_commercial(evidence(source, target), source, target, SCHEMA)

    def test_ambiguous_decimal_and_swapped_offer_prices_require_review(self):
        for source, target, dimension in (
            ("A: €10; B: €20", "A: €20; B: €10", "offer_assignment"),
            ("1,234", "1.234", "amount_currency"),
        ):
            report = evidence(source, target)
            report["checks"][dimension]["status"] = "uncertain"
            report["checks"][dimension]["offer_statuses"][0]["status"] = (
                "uncertain"
            )
            with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
                validate_commercial(report, source, target, SCHEMA)
            self.assertEqual(error.exception.code, "review.commercial.independent_review_required")

    def test_plain_pass_and_major_defect_cannot_bypass_commercial_contract(self):
        adapter = provider()
        del adapter.responses[-1]["commercial_review"]
        with self.assertRaises(WORKER.LocalizationWorkerBlocked):
            WORKER.run_localization_job(job(SOURCE, "commercial"), assets(), adapter)
        adapter = provider()
        adapter.responses[-1]["status"] = "FAIL"
        adapter.responses[-1]["major_defects"] = [{"class": "meaning", "excerpt": "480", "reason": "Wrong claim"}]
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
            WORKER.run_localization_job(job(SOURCE, "commercial"), assets(), adapter)
        self.assertEqual(error.exception.code, "review.source_fidelity.failed")

    def test_commercial_queue_worker_signing_cache_and_publication_path(self):
        plan = make_plan(targets=("sv-SE",), source_text=SOURCE, content_type="commercial")
        with sqlite3.connect(":memory:") as queue_db, sqlite3.connect(":memory:") as release_db:
            queue = RUNNER._QUEUE.LocalizationQueue(queue_db)
            queue.enqueue_plan(plan, now=100)
            queue.enqueue_plan(plan, now=101)  # replay is idempotent
            adapter = provider()
            outcome = RUNNER.run_next_localization_job(
                queue, "commercial-worker", lambda _: adapter,
                lambda _: RUNNER._WORKER.LocalizationAssets(
                    glossary_version="blun-glossary-3", policy_version="native-web-1",
                    audience="Swedish customers", tone_profile="Clear, natural and precise",
                ), clock=Clock(),
            )
            self.assertEqual(outcome.status, "succeeded")
            store = RELEASE.LocalizationReleaseStore(release_db, RELEASE._QUEUE.LocalizationQueue(queue_db))
            authority = HmacAuthority()
            self.assertFalse(store.readiness(plan, authority, now=300).ready)
            with self.assertRaises(RELEASE.LocalizationReleaseBlocked):
                store.approve(
                    plan, plan.jobs[0].job_id, "wrong",
                    ExactReceiptVerifier(), authority,
                    evidence_request_id=EVIDENCE_REQUEST_ID,
                    evidence_revision=EVIDENCE_REVISION,
                    now=300,
                )
            self.assertEqual(authority.sign_calls, 0)
            store.approve(
                plan, plan.jobs[0].job_id, "quality-receipt",
                ExactReceiptVerifier(), authority,
                evidence_request_id=EVIDENCE_REQUEST_ID,
                evidence_revision=EVIDENCE_REVISION,
                now=301,
            )
            self.assertTrue(store.readiness(plan, authority, now=302).ready)
            self.assertEqual(len(store.publication_bundle(plan, authority, now=302)), 1)
            cached = store.cached_result(plan.jobs[0].as_payload(), authority, now=302)
            self.assertEqual(cached["candidate"], TARGET)
            changed = make_plan(targets=("sv-SE",), source_text=SOURCE, content_type="commercial", policy_version="offer-2")
            self.assertIsNone(store.cached_result(changed.jobs[0].as_payload(), authority, now=302))
            self.assertFalse(store.readiness(changed, authority, now=302).ready)
            self.assertEqual(store.cached_result(plan.jobs[0].as_payload(), authority, now=302), cached)

    def test_stale_commercial_contract_is_terminal_before_queue_lease(self):
        plan = make_plan(
            targets=("sv-SE",),
            source_text=SOURCE,
            content_type="commercial",
            software_version="6.141.0",
        )
        with sqlite3.connect(":memory:") as connection:
            queue = RUNNER._QUEUE.LocalizationQueue(connection)
            queue.enqueue_plan(plan, now=100)
            planner = RUNNER._WORKER._PLANNER
            commercial = planner._COMMERCIAL
            contract = commercial.public_review_evidence_contract(
                planner.COMMERCIAL_PROFILE,
            )
            altered = copy.deepcopy(contract)
            altered["trust_boundary"]["publication_authority"] = True
            unsigned = dict(altered)
            unsigned.pop("sha256")
            altered["sha256"] = planner._digest(unsigned)
            public_profile = copy.deepcopy(commercial.public_profile(
                planner.COMMERCIAL_PROFILE,
            ))
            public_profile["review_evidence_contract"] = altered
            calls = []
            with patch.object(
                commercial,
                "public_review_evidence_contract",
                return_value=altered,
            ), patch.object(
                commercial,
                "public_profile",
                return_value=public_profile,
            ), self.assertRaises(RUNNER._QUEUE.LocalizationQueueBlocked):
                RUNNER.run_next_localization_job(
                    queue,
                    "commercial-worker",
                    lambda payload: calls.append("provider"),
                    lambda payload: calls.append("assets"),
                    clock=lambda: 110,
                )
            status = queue.status(plan.jobs[0].job_id)
            self.assertEqual(status.status, "failed")
            self.assertEqual(status.attempts, 0)
            self.assertEqual(status.last_error_code, "job_binding_invalid")
            self.assertEqual(calls, [])

    def test_stale_routing_contract_is_terminal_before_queue_lease(self):
        plan = make_plan(
            targets=("sv-SE",),
            source_text=SOURCE,
            content_type="commercial",
            software_version="6.148.0",
        )
        with sqlite3.connect(":memory:") as connection:
            queue = RUNNER._QUEUE.LocalizationQueue(connection)
            queue.enqueue_plan(plan, now=100)
            planner = RUNNER._WORKER._PLANNER
            commercial = planner._COMMERCIAL
            contract = commercial.public_review_routing_contract(
                planner.COMMERCIAL_PROFILE,
            )
            altered = copy.deepcopy(contract)
            altered["trust_boundary"]["publication_authority"] = True
            unsigned = dict(altered)
            unsigned.pop("sha256")
            altered["sha256"] = planner._digest(unsigned)
            public_profile = copy.deepcopy(commercial.public_profile(
                planner.COMMERCIAL_PROFILE,
            ))
            public_profile["review_routing_contract"] = altered
            calls = []
            with patch.object(
                commercial,
                "public_review_routing_contract",
                return_value=altered,
            ), patch.object(
                commercial,
                "public_profile",
                return_value=public_profile,
            ), self.assertRaises(RUNNER._QUEUE.LocalizationQueueBlocked):
                RUNNER.run_next_localization_job(
                    queue,
                    "commercial-worker",
                    lambda payload: calls.append("provider"),
                    lambda payload: calls.append("assets"),
                    clock=lambda: 110,
                )
            status = queue.status(plan.jobs[0].job_id)
            self.assertEqual(status.status, "failed")
            self.assertEqual(status.attempts, 0)
            self.assertEqual(status.last_error_code, "job_binding_invalid")
            self.assertEqual(calls, [])

    def test_stale_resolution_contract_is_terminal_before_queue_lease(self):
        plan = make_plan(
            targets=("sv-SE",),
            source_text=SOURCE,
            content_type="commercial",
            software_version="6.152.0",
        )
        with sqlite3.connect(":memory:") as connection:
            queue = RUNNER._QUEUE.LocalizationQueue(connection)
            queue.enqueue_plan(plan, now=100)
            planner = RUNNER._WORKER._PLANNER
            commercial = planner._COMMERCIAL
            contract = commercial.public_review_resolution_contract(
                planner.COMMERCIAL_PROFILE,
            )
            altered = copy.deepcopy(contract)
            altered["status"] = "verified"
            unsigned = dict(altered)
            unsigned.pop("sha256")
            altered["sha256"] = planner._digest(unsigned)
            public_profile = copy.deepcopy(commercial.public_profile(
                planner.COMMERCIAL_PROFILE,
            ))
            public_profile["review_resolution_contract"] = altered
            calls = []
            with patch.object(
                commercial,
                "public_review_resolution_contract",
                return_value=altered,
            ), patch.object(
                commercial,
                "public_profile",
                return_value=public_profile,
            ), self.assertRaises(RUNNER._QUEUE.LocalizationQueueBlocked):
                RUNNER.run_next_localization_job(
                    queue,
                    "commercial-worker",
                    lambda payload: calls.append("provider"),
                    lambda payload: calls.append("assets"),
                    clock=lambda: 110,
                )
            status = queue.status(plan.jobs[0].job_id)
            self.assertEqual(status.status, "failed")
            self.assertEqual(status.attempts, 0)
            self.assertEqual(status.last_error_code, "job_binding_invalid")
            self.assertEqual(calls, [])

    def test_uncertainty_survives_queue_but_requires_bound_independent_review(self):
        plan = make_plan(targets=("sv-SE",), source_text=SOURCE, content_type="commercial")
        report = evidence()
        report["checks"]["tax_status"]["status"] = "uncertain"
        report["checks"]["tax_status"]["offer_statuses"][0]["status"] = (
            "uncertain"
        )
        with sqlite3.connect(":memory:") as connection, sqlite3.connect(":memory:") as release_db:
            queue = RUNNER._QUEUE.LocalizationQueue(connection)
            queue.enqueue_plan(plan, now=100)
            outcome = RUNNER.run_next_localization_job(
                queue, "commercial-worker", lambda _: provider(report=report),
                lambda _: RUNNER._WORKER.LocalizationAssets(
                    glossary_version="blun-glossary-3", policy_version="native-web-1",
                    audience="Swedish customers", tone_profile="Clear and natural",
                ), clock=Clock(),
            )
            self.assertEqual(outcome.status, "succeeded")
            self.assertIsNotNone(outcome.result_sha256)
            self.assertIsNone(queue.claim("another-worker", now=1000))
            release_queue = RELEASE._QUEUE.LocalizationQueue(connection)
            store = RELEASE.LocalizationReleaseStore(release_db, release_queue)
            authority = HmacAuthority()
            with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as blocked:
                store.approve(
                    plan, plan.jobs[0].job_id, "quality-receipt",
                    ExactReceiptVerifier(), authority,
                    evidence_request_id=EVIDENCE_REQUEST_ID,
                    evidence_revision=EVIDENCE_REVISION,
                    now=300,
                )
            self.assertEqual(blocked.exception.code, "human.receipt.required")
            verifier = ExactReceiptVerifier("commercial-independent-receipt")
            review = {
                "schema": RELEASE.INDEPENDENT_MODEL_REVIEW_SCHEMA,
                "provider": {
                    "id": "independent-commercial-reviewer",
                    "model_id": "offer-fidelity-review",
                    "model_version": "2026-09-08",
                },
                "receipt": "commercial-independent-receipt",
            }
            approved = store.approve(
                plan, plan.jobs[0].job_id, "quality-receipt",
                ExactReceiptVerifier(), authority,
                evidence_request_id=EVIDENCE_REQUEST_ID,
                evidence_revision=EVIDENCE_REVISION,
                now=301,
                independent_model_review=review,
                independent_model_review_verifier=verifier,
            )
            self.assertTrue(store.readiness(plan, authority, now=302).ready)
            binding = verifier.calls[0]["binding"]
            self.assertEqual(binding["content_type"], "commercial")
            self.assertEqual(binding["commercial_profile"], SCHEMA)
            expected_routing_contract_sha256 = (
                PROFILE.public_review_routing_contract(SCHEMA)["sha256"]
            )
            self.assertEqual(
                binding["commercial_review_routing_contract_sha256"],
                expected_routing_contract_sha256,
            )
            self.assertEqual(
                binding["commercial_review"]["review_required_dimensions"],
                ["tax_status"],
            )
            self.assertEqual(binding["policy_version"], "native-web-1")
            self.assertEqual(
                binding["review_confidence"]["source_fidelity"], "low",
            )
            publication_evidence = approved.release_evidence
            self.assertEqual(
                publication_evidence["schema"],
                RELEASE.PUBLICATION_EVIDENCE_SCHEMA,
            )
            self.assertEqual(
                publication_evidence["release_evidence_contract_sha256"],
                RELEASE.publication_evidence_contract()["sha256"],
            )
            self.assertEqual(
                publication_evidence["evidence_request_id"], EVIDENCE_REQUEST_ID,
            )
            self.assertEqual(
                publication_evidence["evidence_revision"], EVIDENCE_REVISION,
            )
            self.assertEqual(
                publication_evidence[
                    "commercial_review_routing_contract_sha256"
                ],
                expected_routing_contract_sha256,
            )
            self.assertEqual(
                publication_evidence["commercial_review_resolution"]["schema"],
                PROFILE.REVIEW_RESOLUTION_SCHEMA,
            )
            resolution = publication_evidence["commercial_review_resolution"]
            self.assertEqual(resolution["profile"], SCHEMA)
            self.assertEqual(
                resolution["contract_sha256"],
                PROFILE.public_review_resolution_contract(SCHEMA)["sha256"],
            )
            self.assertEqual(
                resolution["primary_provider"],
                plan.jobs[0].as_payload()["provider"],
            )

    def test_release_rejects_tampered_commercial_routing_summary(self):
        result, _ = self.run_worker()
        payload = job(SOURCE, "commercial")
        RELEASE._validate_result(payload, result)
        for mutation in (
            lambda value: value["commercial_review"].update(
                review_required_dimensions=["tax_status"],
            ),
            lambda value: value["commercial_review"].update(
                evidence_sha256="not-a-digest",
            ),
            lambda value: value.update(commercial_review=None),
            lambda value: value.update(
                commercial_review_routing_contract_sha256="0" * 64,
            ),
            lambda value: value.update(
                commercial_review_routing_contract_sha256=None,
            ),
        ):
            changed = copy.deepcopy(result)
            mutation(changed)
            with self.subTest(changed=changed), self.assertRaises(
                RELEASE.LocalizationReleaseBlocked,
            ):
                RELEASE._validate_result(payload, changed)

    def test_many_offer_evidence_items_survive_without_price_bag_matching(self):
        source = "\n".join(f"Offer {i}: €{i + 10} a month, billed annually." for i in range(100))
        target = "\n".join(f"Paket {i}: {i + 10} € per månad, faktureras årsvis." for i in range(100))
        report = evidence(source, target)
        items = []
        source_offset = target_offset = 0
        for i, (src, tgt) in enumerate(zip(source.splitlines(), target.splitlines())):
            items.append({
                "offer": f"offer-{i}", "relation": "matched",
                "source_span": [source_offset, source_offset + len(src)],
                "target_span": [target_offset, target_offset + len(tgt)],
                "explanation": "Scripted monthly display / annual charge association.",
            })
            source_offset += len(src) + 1
            target_offset += len(tgt) + 1
        report["offers"] = [
            {
                "id": item["offer"],
                "source_spans": [item["source_span"]],
                "target_spans": [item["target_span"]],
            }
            for item in items
        ]
        for check in report["checks"].values():
            check["offer_statuses"] = [
                {"offer": item["offer"], "status": check["status"]}
                for item in items
            ]
            if check["status"] == "equivalent":
                check["items"] = copy.deepcopy(items)
        validate_commercial(report, source, target, SCHEMA)


if __name__ == "__main__":
    unittest.main()
