from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLANNER = load(
    "blun_test_release_planner",
    ROOT / "integrations" / "website_localization.py",
)
RELEASE = load(
    "blun_test_website_localization_release",
    ROOT / "integrations" / "website_localization_release.py",
)
QUEUE = RELEASE._QUEUE
WORKER = RELEASE._WORKER


def make_plan(targets=("de-AT", "sv-SE"), **overrides):
    values = {
        "source_id": "homepage.hero",
        "source_revision": "cms-184",
        "source_text": "Build your business with BLUN.",
        "source_locale": "en-IE",
        "content_type": "headline",
        "glossary_version": "blun-glossary-3",
        "policy_version": "native-web-1",
        "provider_id": "customer-llm",
        "model_id": "king",
        "model_version": "2026-08-29",
        "software_version": "6.43.0-dev",
        "target_locales": list(targets),
    }
    values.update(overrides)
    return PLANNER.plan_website_localization(**values)


def completed_result(job, candidate, *, review_confidence=None):
    payload = job.as_payload()
    review_confidence = review_confidence or {
        "target_native": "high",
        "source_fidelity": "high",
    }
    target_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    phases = []
    for index, phase in enumerate(WORKER.PHASES):
        phases.append({
            "phase": phase,
            "request_sha256": hashlib.sha256(f"request-{index}".encode()).hexdigest(),
            "response_sha256": hashlib.sha256(f"response-{index}".encode()).hexdigest(),
            "status": "PASS",
        })
    return {
        "schema": WORKER.RESULT_SCHEMA,
        "worker_schema": WORKER.WORKER_SCHEMA,
        "job_id": payload["job_id"],
        "source_sha256": payload["source"]["sha256"],
        "target_sha256": target_hash,
        "source_locale": payload["source"]["locale"],
        "target_locale": payload["target"]["locale"],
        "content_type": payload["content_type"],
        "glossary_version": payload["glossary_version"],
        "policy_version": payload["policy_version"],
        "provider": payload["provider"],
        "software_version": payload["software_version"],
        "candidate": candidate,
        "quality_passes": phases,
        "integrity": {
            "status": "PASS",
            "guard": "translate-native-structure-and-token-gate",
        },
        "review_confidence": review_confidence,
        "quality_profile": {
            "locale": payload["target"]["locale"],
            "version": payload["target"]["quality_profile_version"],
            "sha256": payload["target"]["quality_profile_sha256"],
        },
        "human_review_required": payload["content_type"] == "legal",
        "independent_review_required": (
            payload["content_type"] != "legal" and "low" in review_confidence.values()
        ),
        "release_required": True,
    }


class HmacAuthority:
    def __init__(self, key=b"isolated-host-key"):
        self.key = key
        self.sign_calls = 0

    def sign(self, payload):
        self.sign_calls += 1
        return RELEASE.ApprovalSignature(
            "hmac-sha256-test",
            "test-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return signature.algorithm == "hmac-sha256-test" and hmac.compare_digest(
            signature.signature,
            expected,
        )


class ExactReceiptVerifier:
    def __init__(self, accepted="quality-receipt"):
        self.accepted = accepted
        self.calls = []

    def verify(self, **values):
        self.calls.append(values)
        return values["receipt"] == self.accepted


class BoundReceiptVerifier:
    def __init__(self, key=b"isolated-evidence-key"):
        self.key = key
        self.calls = []

    def issue(self, binding):
        payload = json.dumps(
            binding,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "bound:" + hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def verify(self, **values):
        self.calls.append(values)
        return hmac.compare_digest(
            values["receipt"],
            self.issue(values["binding"]),
        )


class ReceiptVerifierUnavailable(RuntimeError):
    localization_receipt_verification_failure = True

    def __init__(self, code="network", *, retryable=True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class WebsiteLocalizationReleaseTests(unittest.TestCase):
    def setUp(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.store = RELEASE.LocalizationReleaseStore(
            self.release_connection,
            self.queue,
        )
        self.authority = HmacAuthority()
        self.verifier = ExactReceiptVerifier()

    def tearDown(self):
        self.release_connection.close()
        self.queue_connection.close()

    def complete(self, plan, translations=None, *, review_confidence=None):
        translations = translations or {
            "de-AT": "Baue dein Unternehmen mit BLUN auf.",
            "sv-SE": "Bygg ditt företag med BLUN.",
        }
        self.queue.enqueue_plan(plan, now=90)
        completed = {}
        for index, job in enumerate(sorted(plan.jobs, key=lambda item: item.as_payload()["target"]["locale"])):
            claim = self.queue.claim("worker", now=100 + index, lease_seconds=30)
            result = completed_result(
                job,
                translations[claim.target_locale],
                review_confidence=review_confidence,
            )
            self.queue.complete(claim, result, now=101 + index)
            completed[job.job_id] = result
        return completed

    def approve(self, plan, job, **overrides):
        values = {
            "now": 200,
            "ttl_seconds": 100,
        }
        values.update(overrides)
        return self.store.approve(
            plan,
            job.job_id,
            "quality-receipt",
            self.verifier,
            self.authority,
            **values,
        )

    def test_signed_approval_binds_every_release_dimension(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan)
        approved = self.approve(plan, plan.jobs[0])
        self.assertEqual(approved.target_locale, "sv-SE")
        row = self.release_connection.execute(
            "SELECT approval_json FROM localization_approvals"
        ).fetchone()
        payload = json.loads(row[0])
        self.assertEqual(payload["source_sha256"], plan.jobs[0].as_payload()["source"]["sha256"])
        self.assertEqual(payload["target_sha256"], approved.target_sha256)
        self.assertEqual(payload["glossary_version"], "blun-glossary-3")
        self.assertEqual(payload["policy_version"], "native-web-1")
        self.assertEqual(payload["provider"]["model_id"], "king")
        self.assertEqual(payload["software_version"], "6.43.0-dev")
        self.assertEqual(payload["quality_profile"], {
            "locale": "sv-SE",
            "version": plan.jobs[0].target.quality_profile_version,
            "sha256": plan.jobs[0].target.quality_profile_sha256,
        })
        self.assertNotIn("quality-receipt", row[0])
        receipt_binding = self.verifier.calls[0]["binding"]
        self.assertEqual(receipt_binding["target_text"], approved.candidate)
        self.assertEqual(receipt_binding["schema"], RELEASE.RECEIPT_BINDING_SCHEMA)
        self.assertEqual(receipt_binding["review_kind"], "quality")
        self.assertEqual(receipt_binding["job_id"], plan.jobs[0].job_id)
        self.assertEqual(receipt_binding["result_sha256"], payload["result_sha256"])
        self.assertEqual(receipt_binding["source_sha256"], payload["source_sha256"])
        self.assertEqual(receipt_binding["target_sha256"], payload["target_sha256"])
        self.assertEqual(receipt_binding["source_locale"], "en-IE")
        self.assertEqual(receipt_binding["target_locale"], "sv-SE")
        self.assertEqual(receipt_binding["content_type"], "headline")
        self.assertEqual(receipt_binding["glossary_version"], "blun-glossary-3")
        self.assertEqual(receipt_binding["policy_version"], "native-web-1")
        self.assertEqual(receipt_binding["primary_provider"], payload["provider"])
        self.assertEqual(receipt_binding["software_version"], "6.43.0-dev")
        self.assertEqual(receipt_binding["quality_profile"], payload["quality_profile"])
        self.assertIsNone(receipt_binding["review_provider"])

    def test_translation_memory_reuses_one_job_across_plan_compositions(self):
        single = make_plan(("sv-SE",))
        combined = make_plan(("de-AT", "sv-SE"))
        combined_swedish = next(
            job for job in combined.jobs
            if job.as_payload()["target"]["locale"] == "sv-SE"
        )
        self.assertEqual(single.jobs[0].job_id, combined_swedish.job_id)
        self.complete(single)
        approved = self.approve(single, single.jobs[0])
        reused = self.store.lookup(combined, combined_swedish.job_id, self.authority, now=201)
        self.assertEqual(reused.approval_id, approved.approval_id)

    def test_every_release_binding_change_invalidates_lookup(self):
        original = make_plan(("sv-SE",))
        self.complete(original)
        self.approve(original, original.jobs[0])
        mutations = (
            {"source_revision": "cms-185"},
            {"source_text": "Grow your business with BLUN."},
            {"source_locale": "en-US"},
            {"content_type": "marketing"},
            {"glossary_version": "blun-glossary-4"},
            {"policy_version": "native-web-2"},
            {"provider_id": "second-customer-llm"},
            {"model_id": "king-next"},
            {"model_version": "2026-08-30"},
            {"software_version": "6.43.1-dev"},
        )
        for values in mutations:
            with self.subTest(values=values):
                changed = make_plan(("sv-SE",), **values)
                with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
                    self.store.lookup(changed, changed.jobs[0].job_id, self.authority, now=201)
                self.assertEqual(caught.exception.code, "approval.missing")

    def test_quality_receipt_cannot_be_replayed_after_policy_change(self):
        original = make_plan(("sv-SE",))
        self.complete(original)
        original_result = self.store.validated_result(
            original,
            original.jobs[0].job_id,
        )
        original_result_sha256 = hashlib.sha256(
            json.dumps(
                original_result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        verifier = BoundReceiptVerifier()
        receipt = verifier.issue(RELEASE._receipt_binding(
            original.jobs[0].job_id,
            original.jobs[0].as_payload(),
            original_result,
            original_result_sha256,
            review_kind="quality",
        ))
        self.store.approve(
            original,
            original.jobs[0].job_id,
            receipt,
            verifier,
            self.authority,
            now=200,
        )

        changed = make_plan(("sv-SE",), policy_version="native-web-2")
        self.complete(changed)
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.store.approve(
                changed,
                changed.jobs[0].job_id,
                receipt,
                verifier,
                self.authority,
                now=201,
            )
        self.assertEqual(caught.exception.code, "quality.receipt.rejected")
        self.assertEqual(self.authority.sign_calls, 1)

    def test_quality_receipt_cannot_satisfy_qualified_human_review(self):
        plan = make_plan(
            ("sv-SE",),
            content_type="legal",
            source_text="By continuing, you accept the terms.",
        )
        self.complete(plan, {
            "sv-SE": "Genom att fortsätta godkänner du villkoren.",
        })
        result = self.store.validated_result(plan, plan.jobs[0].job_id)
        result_sha256 = hashlib.sha256(
            json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        verifier = BoundReceiptVerifier()
        quality_receipt = verifier.issue(RELEASE._receipt_binding(
            plan.jobs[0].job_id,
            plan.jobs[0].as_payload(),
            result,
            result_sha256,
            review_kind="quality",
        ))

        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.store.approve(
                plan,
                plan.jobs[0].job_id,
                quality_receipt,
                verifier,
                self.authority,
                now=200,
                human_review_receipt=quality_receipt,
                human_review_verifier=verifier,
            )
        self.assertEqual(caught.exception.code, "human.receipt.rejected")
        self.assertEqual(self.authority.sign_calls, 0)

    def test_all_24_eu_locales_are_required_before_readiness(self):
        locales = tuple(profile.locale for profile in PLANNER.EU_OFFICIAL_LOCALES)
        plan = make_plan(
            locales,
            source_locale="uk-UA",
            source_text="Build the BLUN website.",
        )
        translations = {
            profile.locale: f"{profile.native_name} — BLUN."
            for profile in PLANNER.EU_OFFICIAL_LOCALES
        }
        self.complete(plan, translations)
        for job in plan.jobs[:-1]:
            self.approve(plan, job)
        partial = self.store.readiness(plan, self.authority, now=201)
        self.assertFalse(partial.ready)
        self.assertEqual(len(partial.required_locales), 24)
        self.assertEqual(len(partial.approved_locales), 23)
        self.approve(plan, plan.jobs[-1])
        self.assertTrue(self.store.readiness(plan, self.authority, now=201).ready)

    def test_whole_site_is_ready_only_after_every_required_locale_is_approved(self):
        plan = make_plan()
        self.complete(plan)
        self.approve(plan, plan.jobs[0])
        partial = self.store.readiness(plan, self.authority, now=201)
        self.assertFalse(partial.ready)
        self.assertEqual(len(partial.approved_locales), 1)
        self.assertEqual(len(partial.blocked), 1)
        self.approve(plan, plan.jobs[1])
        ready = self.store.readiness(plan, self.authority, now=201)
        self.assertTrue(ready.ready)
        bundle = self.store.publication_bundle(plan, self.authority, now=201)
        self.assertEqual(tuple(item.target_locale for item in bundle), ("de-AT", "sv-SE"))

    def test_partial_or_failed_site_has_no_publication_bundle(self):
        plan = make_plan()
        self.complete(plan)
        self.approve(plan, plan.jobs[0])
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.store.publication_bundle(plan, self.authority, now=201)
        self.assertEqual(caught.exception.code, "website.not_ready")

    def test_quality_receipt_is_required_and_rejected_fail_closed(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan)
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.store.approve(
                plan,
                plan.jobs[0].job_id,
                "wrong-receipt",
                self.verifier,
                self.authority,
                now=200,
            )
        self.assertEqual(caught.exception.code, "quality.receipt.rejected")
        self.assertEqual(
            self.release_connection.execute("SELECT COUNT(*) FROM localization_approvals").fetchone()[0],
            0,
        )

    def test_declared_verifier_unavailability_preserves_retryability(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan)

        class UnavailableVerifier:
            def verify(self, **values):
                raise ReceiptVerifierUnavailable()

        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.store.approve(
                plan,
                plan.jobs[0].job_id,
                "quality-receipt",
                UnavailableVerifier(),
                self.authority,
                now=200,
            )
        self.assertEqual(caught.exception.code, "quality.verifier.network")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.authority.sign_calls, 0)

    def test_legal_result_requires_separate_human_review_receipt(self):
        plan = make_plan(
            ("sv-SE",),
            content_type="legal",
            source_text="By continuing, you accept the terms.",
        )
        self.complete(plan, {"sv-SE": "Genom att fortsätta godkänner du villkoren."})
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.approve(plan, plan.jobs[0])
        self.assertEqual(caught.exception.code, "human.receipt.required")
        human = ExactReceiptVerifier("human-review-receipt")
        approved = self.approve(
            plan,
            plan.jobs[0],
            human_review_receipt="human-review-receipt",
            human_review_verifier=human,
        )
        self.assertEqual(approved.target_locale, "sv-SE")

    def test_low_confidence_result_requires_separate_human_review_receipt(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan, review_confidence={
            "target_native": "low",
            "source_fidelity": "high",
        })
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.approve(plan, plan.jobs[0])
        self.assertEqual(caught.exception.code, "human.receipt.required")

        human = ExactReceiptVerifier("qualified-native-review")
        approved = self.approve(
            plan,
            plan.jobs[0],
            human_review_receipt="qualified-native-review",
            human_review_verifier=human,
        )
        self.assertEqual(approved.target_locale, "sv-SE")

    def test_low_confidence_accepts_a_bound_independent_model_adapter(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan, review_confidence={
            "target_native": "low",
            "source_fidelity": "high",
        })
        review = {
            "schema": RELEASE.INDEPENDENT_MODEL_REVIEW_SCHEMA,
            "provider": {
                "id": "second-provider",
                "model_id": "native-reviewer",
                "model_version": "2026-09-08",
            },
            "receipt": "independent-model-receipt",
        }
        verifier = ExactReceiptVerifier("independent-model-receipt")
        approved = self.approve(
            plan,
            plan.jobs[0],
            independent_model_review=review,
            independent_model_review_verifier=verifier,
        )
        stored = json.loads(self.release_connection.execute(
            "SELECT approval_json FROM localization_approvals",
        ).fetchone()[0])
        self.assertEqual(approved.target_locale, "sv-SE")
        self.assertEqual(
            stored["independent_model_review"]["provider"], review["provider"],
        )
        self.assertIsNone(stored["human_review_receipt_sha256"])
        binding = verifier.calls[0]["binding"]
        self.assertEqual(binding["primary_provider"], {
            "id": "customer-llm",
            "model_id": "king",
            "model_version": "2026-08-29",
        })
        self.assertEqual(binding["review_provider"], review["provider"])
        self.assertEqual(binding["review_kind"], "independent_model")
        self.assertEqual(binding["content_type"], "headline")
        self.assertEqual(binding["policy_version"], "native-web-1")
        self.assertEqual(binding["review_confidence"], {
            "target_native": "low",
            "source_fidelity": "high",
        })
        self.assertEqual(binding["quality_profile"]["locale"], "sv-SE")
        self.assertIsNone(binding["commercial_profile"])
        self.store.lookup(plan, plan.jobs[0].job_id, self.authority, now=201)

    def test_same_provider_is_not_an_independent_model_adapter(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan, review_confidence={
            "target_native": "high",
            "source_fidelity": "low",
        })
        review = {
            "schema": RELEASE.INDEPENDENT_MODEL_REVIEW_SCHEMA,
            "provider": {
                "id": "customer-llm",
                "model_id": "different-model",
                "model_version": "2026-09-08",
            },
            "receipt": "same-provider-receipt",
        }
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.approve(
                plan,
                plan.jobs[0],
                independent_model_review=review,
                independent_model_review_verifier=ExactReceiptVerifier(
                    "same-provider-receipt",
                ),
            )
        self.assertEqual(caught.exception.code, "independent_model_review.not_independent")
        self.assertEqual(self.authority.sign_calls, 0)

    def test_low_confidence_rejects_ambiguous_or_unverified_escalation(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan, review_confidence={
            "target_native": "low",
            "source_fidelity": "high",
        })
        review = {
            "schema": RELEASE.INDEPENDENT_MODEL_REVIEW_SCHEMA,
            "provider": {
                "id": "second-provider",
                "model_id": "native-reviewer",
                "model_version": "2026-09-08",
            },
            "receipt": "independent-model-receipt",
        }
        cases = (
            ({
                "human_review_receipt": "qualified-native-review",
                "human_review_verifier": ExactReceiptVerifier(
                    "qualified-native-review",
                ),
                "independent_model_review": review,
                "independent_model_review_verifier": ExactReceiptVerifier(
                    "independent-model-receipt",
                ),
            }, "review.escalation.ambiguous"),
            ({
                "independent_model_review": review,
                "independent_model_review_verifier": ExactReceiptVerifier(
                    "wrong-receipt",
                ),
            }, "independent_model_review.receipt.rejected"),
        )
        for values, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
                    self.approve(plan, plan.jobs[0], **values)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.authority.sign_calls, 0)

    def test_legal_content_cannot_replace_human_review_with_a_model(self):
        plan = make_plan(
            ("sv-SE",), content_type="legal",
            source_text="By continuing, you accept the terms.",
        )
        self.complete(plan, {"sv-SE": "Genom att fortsätta godkänner du villkoren."})
        review = {
            "schema": RELEASE.INDEPENDENT_MODEL_REVIEW_SCHEMA,
            "provider": {
                "id": "second-provider",
                "model_id": "legal-reviewer",
                "model_version": "2026-09-08",
            },
            "receipt": "model-legal-receipt",
        }
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.approve(
                plan,
                plan.jobs[0],
                independent_model_review=review,
                independent_model_review_verifier=ExactReceiptVerifier(
                    "model-legal-receipt",
                ),
            )
        self.assertEqual(
            caught.exception.code, "independent_model_review.legal_forbidden",
        )

    def test_low_confidence_cannot_drop_independent_review_requirement(self):
        plan = make_plan(("sv-SE",))
        job = plan.jobs[0]
        result = completed_result(job, "Bygg ditt företag med BLUN.", review_confidence={
            "target_native": "high",
            "source_fidelity": "low",
        })
        result["independent_review_required"] = False
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            RELEASE._validate_result(job.as_payload(), result)
        self.assertEqual(caught.exception.code, "result.independent_review.invalid")

    def test_substituted_quality_profile_cannot_reach_signing(self):
        plan = make_plan(("sv-SE",))
        result = completed_result(plan.jobs[0], "Bygg ditt företag med BLUN.")
        result["quality_profile"]["sha256"] = "0" * 64
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            RELEASE._validate_result(plan.jobs[0].as_payload(), result)
        self.assertEqual(caught.exception.code, "result.quality_profile.invalid")

    def test_tampered_result_payload_or_signature_blocks_lookup(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan)
        self.approve(plan, plan.jobs[0])
        for column, value, code in (
            ("result_json", "{}", "translation_memory.result_tampered"),
            ("approval_json", "{}", "approval.payload.tampered"),
            ("signature", "0" * 64, "approval.signature.invalid"),
        ):
            with self.subTest(column=column):
                self.release_connection.execute(
                    f"UPDATE localization_approvals SET {column} = ?",
                    (value,),
                )
                self.release_connection.commit()
                with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
                    self.store.lookup(plan, plan.jobs[0].job_id, self.authority, now=201)
                self.assertEqual(caught.exception.code, code)
                self.release_connection.close()
                self.release_connection = sqlite3.connect(":memory:")
                self.store = RELEASE.LocalizationReleaseStore(self.release_connection, self.queue)
                self.approve(plan, plan.jobs[0])

    def test_queue_result_tampering_blocks_before_signing(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan)
        self.queue_connection.execute(
            "UPDATE localization_jobs SET result_json = '{}' WHERE job_id = ?",
            (plan.jobs[0].job_id,),
        )
        self.queue_connection.commit()
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.approve(plan, plan.jobs[0])

    def test_approved_target_is_immutable_for_the_same_job(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan)
        self.approve(plan, plan.jobs[0])
        changed = completed_result(plan.jobs[0], "Utveckla ditt företag med BLUN.")
        encoded = json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.queue_connection.execute("""
            UPDATE localization_jobs SET result_json = ?, result_sha256 = ?
            WHERE job_id = ?
        """, (
            encoded,
            hashlib.sha256(encoded.encode()).hexdigest(),
            plan.jobs[0].job_id,
        ))
        self.queue_connection.commit()
        with self.assertRaises(RELEASE.LocalizationReleaseBlocked) as caught:
            self.approve(plan, plan.jobs[0])
        self.assertEqual(caught.exception.code, "translation_memory.immutable")
        self.assertEqual(self.authority.sign_calls, 1)

    def test_expired_approval_blocks_readiness(self):
        plan = make_plan(("sv-SE",))
        self.complete(plan)
        self.approve(plan, plan.jobs[0], ttl_seconds=5)
        readiness = self.store.readiness(plan, self.authority, now=206)
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.blocked, (("sv-SE", "approval.expired"),))


if __name__ == "__main__":
    unittest.main()
