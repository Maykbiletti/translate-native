from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from test_website_localization_worker import WORKER, PLANNER, ScriptedProvider, assets, candidate, job, review
from test_website_localization_fallback import RUNNER, Clock
from test_website_localization_release import RELEASE, HmacAuthority, ExactReceiptVerifier, make_plan


PROFILE = WORKER._COMMERCIAL
SCHEMA = PLANNER.COMMERCIAL_PROFILE
SOURCE = "Save up to €480 a year. All prices exclude VAT."
TARGET = "Spara upp till 480 € per år. Alla priser är exklusive moms."


def evidence(source=SOURCE, target=TARGET):
    checks = {name: {"status": "not_present", "items": []} for name in PROFILE.DIMENSIONS}
    for name in ("amount_currency", "discount_basis", "qualifiers", "tax_status", "offer_assignment"):
        checks[name] = {"status": "equivalent", "items": [{
            "offer": "offer-1",
            "source_span": [0, len(source)],
            "target_span": [0, len(target)],
            "explanation": "Scripted protocol fixture; not independent linguistic evidence.",
        }]}
    return {"schema": SCHEMA, "coverage": "complete", "checks": checks}


def provider(source=SOURCE, target=TARGET, locale="sv-SE", report=None):
    fidelity = review("source_fidelity", locale=locale)
    fidelity["commercial_review"] = evidence(source, target) if report is None else report
    return ScriptedProvider([candidate(target, locale), review("target_native", locale=locale), fidelity])


class CommercialLocalizationTests(unittest.TestCase):
    def run_worker(self, report=None):
        adapter = provider(report=report)
        return WORKER.run_localization_job(job(SOURCE, "commercial"), assets(), adapter), adapter

    def test_ordered_review_preserves_source_blindness_and_hashes_full_evidence(self):
        result, adapter = self.run_worker()
        self.assertEqual([r.phase for r in adapter.requests], list(WORKER.PHASES))
        native = json.dumps(adapter.requests[1].as_payload())
        self.assertNotIn(SOURCE, native)
        self.assertNotIn('"source_span"', native)
        self.assertNotIn('"commercial_review"', native)
        fidelity = adapter.requests[2]
        self.assertEqual(fidelity.input["source"]["text"], SOURCE)
        self.assertEqual(set(fidelity.input["response_schema"]["commercial_review"]["checks"]), set(PROFILE.DIMENSIONS))
        response = review("source_fidelity")
        response["commercial_review"] = evidence()
        self.assertEqual(result["quality_passes"][2]["response_sha256"], WORKER._hash_json(response))
        self.assertTrue(result["release_required"])

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

    def test_profile_changes_invalidate_plan_and_job_ids(self):
        before = job(SOURCE, "commercial")
        with patch.object(PLANNER, "COMMERCIAL_PROFILE", "translate-native.commercial.v2"):
            after = job(SOURCE, "commercial")
        self.assertNotEqual(before["job_id"], after["job_id"])
        self.assertNotEqual(before["commercial_profile"], after["commercial_profile"])
        with self.assertRaises(WORKER.LocalizationWorkerBlocked):
            WORKER._validated_job(after)
        self.assertNotIn("commercial_profile", job(SOURCE, "marketing"))

    def test_each_dimension_blocks_known_changes_and_routes_uncertainty(self):
        for dimension in PROFILE.DIMENSIONS:
            report = evidence()
            report["checks"][dimension] = {"status": "changed", "items": []}
            with self.subTest(dimension=dimension, verdict="changed"):
                with self.assertRaises(WORKER.LocalizationWorkerBlocked) as error:
                    self.run_worker(report)
                self.assertEqual(error.exception.code, "review.commercial.changed")
                self.assertFalse(error.exception.retryable)

            report = evidence()
            report["checks"][dimension] = {"status": "uncertain", "items": []}
            with self.subTest(dimension=dimension, verdict="uncertain"):
                result, _ = self.run_worker(report)
                self.assertEqual(result["review_confidence"]["source_fidelity"], "low")
                self.assertTrue(result["independent_review_required"])

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
                "checks": {
                    name: {"status": "not_present", "items": []}
                    for name in PROFILE.DIMENSIONS
                },
            },
        ):
            with self.subTest(report=report):
                result, _ = self.run_worker(report)
                self.assertTrue(result["independent_review_required"])
                self.assertEqual(result["review_confidence"]["source_fidelity"], "low")

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

    def test_conditions_require_reviewed_offer_association(self):
        report = evidence()
        report["checks"]["amount_currency"]["items"][0]["offer"] = "unreviewed-offer"
        with self.assertRaises(WORKER.LocalizationWorkerBlocked):
            self.run_worker(report)
        report = evidence()
        report["checks"]["offer_assignment"] = {"status": "not_present", "items": []}
        result, _ = self.run_worker(report)
        self.assertTrue(result["independent_review_required"])
        self.assertEqual(result["review_confidence"]["source_fidelity"], "low")

    def test_no_numeric_regex_rejects_semantically_reviewed_native_forms(self):
        # Contract-level fixtures, not claims of independent native approval.
        for source, target in (
            ("4 tiers", "four tiers"), ("20%", "twenty percent"),
            ("12 months", "one year"), ("480", "٤٨٠"),
            ("1,234.50 €", "1.234,50 €"), ("€480", "480\u00a0€"),
        ):
            with self.subTest(target=target):
                PROFILE.validate_review(evidence(source, target), source, target, SCHEMA)

    def test_ambiguous_decimal_and_swapped_offer_prices_require_review(self):
        for source, target, dimension in (
            ("A: €10; B: €20", "A: €20; B: €10", "offer_assignment"),
            ("1,234", "1.234", "amount_currency"),
        ):
            report = evidence(source, target)
            report["checks"][dimension] = {"status": "uncertain", "items": []}
            with self.assertRaises(PROFILE.CommercialReviewBlocked) as error:
                PROFILE.validate_review(report, source, target, SCHEMA)
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
                store.approve(plan, plan.jobs[0].job_id, "wrong", ExactReceiptVerifier(), authority, now=300)
            self.assertEqual(authority.sign_calls, 0)
            store.approve(plan, plan.jobs[0].job_id, "quality-receipt", ExactReceiptVerifier(), authority, now=301)
            self.assertTrue(store.readiness(plan, authority, now=302).ready)
            self.assertEqual(len(store.publication_bundle(plan, authority, now=302)), 1)
            cached = store.cached_result(plan.jobs[0].as_payload(), authority, now=302)
            self.assertEqual(cached["candidate"], TARGET)
            changed = make_plan(targets=("sv-SE",), source_text=SOURCE, content_type="commercial", policy_version="offer-2")
            self.assertIsNone(store.cached_result(changed.jobs[0].as_payload(), authority, now=302))
            self.assertFalse(store.readiness(changed, authority, now=302).ready)
            self.assertEqual(store.cached_result(plan.jobs[0].as_payload(), authority, now=302), cached)

    def test_uncertainty_survives_queue_but_requires_bound_independent_review(self):
        plan = make_plan(targets=("sv-SE",), source_text=SOURCE, content_type="commercial")
        report = evidence()
        report["checks"]["tax_status"] = {"status": "uncertain", "items": []}
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
                    ExactReceiptVerifier(), authority, now=300,
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
            store.approve(
                plan, plan.jobs[0].job_id, "quality-receipt",
                ExactReceiptVerifier(), authority, now=301,
                independent_model_review=review,
                independent_model_review_verifier=verifier,
            )
            self.assertTrue(store.readiness(plan, authority, now=302).ready)
            self.assertEqual(verifier.calls[0]["content_type"], "commercial")
            self.assertEqual(verifier.calls[0]["commercial_profile"], SCHEMA)
            self.assertEqual(verifier.calls[0]["policy_version"], "native-web-1")
            self.assertEqual(
                verifier.calls[0]["review_confidence"]["source_fidelity"], "low",
            )

    def test_many_offer_evidence_items_survive_without_price_bag_matching(self):
        source = "\n".join(f"Offer {i}: €{i + 10} a month, billed annually." for i in range(100))
        target = "\n".join(f"Paket {i}: {i + 10} € per månad, faktureras årsvis." for i in range(100))
        report = evidence(source, target)
        items = []
        source_offset = target_offset = 0
        for i, (src, tgt) in enumerate(zip(source.splitlines(), target.splitlines())):
            items.append({
                "offer": f"offer-{i}", "source_span": [source_offset, source_offset + len(src)],
                "target_span": [target_offset, target_offset + len(tgt)],
                "explanation": "Scripted monthly display / annual charge association.",
            })
            source_offset += len(src) + 1
            target_offset += len(tgt) + 1
        report["checks"]["offer_assignment"]["items"] = items
        PROFILE.validate_review(report, source, target, SCHEMA)


if __name__ == "__main__":
    unittest.main()
