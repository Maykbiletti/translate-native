from __future__ import annotations

import importlib.util
import hashlib
import json
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
    "blun_test_worker_planner",
    ROOT / "integrations" / "website_localization.py",
)
WORKER = load(
    "blun_test_website_localization_worker",
    ROOT / "integrations" / "website_localization_worker.py",
)


def job(source_text="Build your business with BLUN.", content_type="headline"):
    plan = PLANNER.plan_website_localization(
        source_id="homepage.hero",
        source_revision="cms-184",
        source_text=source_text,
        source_locale="en-IE",
        content_type=content_type,
        glossary_version="blun-glossary-3",
        policy_version="native-web-1",
        provider_id="customer-llm",
        model_id="king",
        model_version="2026-08-29",
        software_version="6.43.0-dev",
        target_locales=["sv-SE"],
    )
    return plan.jobs[0].as_payload()


def assets(**overrides):
    value = {
        "glossary_version": "blun-glossary-3",
        "policy_version": "native-web-1",
        "audience": "Swedish small-business owners",
        "tone_profile": "Confident, warm, concise, and never inflated",
        "glossary": (
            WORKER.GlossaryTerm("business", "företag", "Use the established product term."),
        ),
        "protected_terms": ("BLUN",),
    }
    value.update(overrides)
    return WORKER.LocalizationAssets(**value)


def candidate(text="Bygg ditt företag med BLUN.", locale="sv-SE"):
    return {
        "schema": WORKER.CANDIDATE_SCHEMA,
        "phase": "transcreation",
        "locale": locale,
        "candidate": text,
    }


def review(phase, status="PASS", locale="sv-SE", findings=None):
    findings = [] if findings is None else findings
    return {
        "schema": WORKER.REVIEW_SCHEMA,
        "phase": phase,
        "locale": locale,
        "status": status,
        "blocking_defects": findings,
        "major_defects": [],
    }


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class WebsiteLocalizationWorkerTests(unittest.TestCase):
    def successful_provider(self, text="Bygg ditt företag med BLUN."):
        return ScriptedProvider([
            candidate(text),
            review("target_native"),
            review("source_fidelity"),
        ])

    def test_runs_three_ordered_provider_neutral_passes(self):
        provider = self.successful_provider()
        result = WORKER.run_localization_job(job(), assets(), provider)
        self.assertEqual(
            [request.phase for request in provider.requests],
            ["transcreation", "target_native", "source_fidelity"],
        )
        self.assertEqual(result["target_locale"], "sv-SE")
        self.assertEqual(result["candidate"], "Bygg ditt företag med BLUN.")
        self.assertEqual(
            result["target_sha256"],
            hashlib.sha256(result["candidate"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(len(result["quality_passes"]), 3)
        self.assertTrue(result["release_required"])

    def test_progress_callback_follows_only_validated_phase_boundaries(self):
        progress = []
        WORKER.run_localization_job(
            job(),
            assets(),
            self.successful_provider(),
            progress_callback=progress.append,
        )
        self.assertEqual(progress, [
            "transcreation",
            "target_native",
            "source_fidelity",
            "integrity",
        ])

    def test_target_only_review_never_receives_source_or_source_glossary_terms(self):
        provider = self.successful_provider()
        WORKER.run_localization_job(job(), assets(), provider)
        native_input = provider.requests[1].input
        serialized = json.dumps(native_input, ensure_ascii=False)
        self.assertNotIn("Build your business", serialized)
        self.assertNotIn('"source"', serialized)
        self.assertNotIn('"source_locale"', serialized)
        self.assertNotIn('"business"', serialized)
        self.assertIn("företag", serialized)

    def test_fidelity_review_receives_complete_source_and_versions(self):
        provider = self.successful_provider()
        WORKER.run_localization_job(job(), assets(), provider)
        fidelity = provider.requests[2].input
        self.assertEqual(fidelity["source"]["text"], "Build your business with BLUN.")
        self.assertEqual(fidelity["glossary_version"], "blun-glossary-3")
        self.assertEqual(fidelity["policy_version"], "native-web-1")
        self.assertEqual(fidelity["glossary"][0]["target"], "företag")

    def test_every_content_type_has_distinct_creation_guidance(self):
        self.assertEqual(set(WORKER._CONTENT_GUIDANCE), set(PLANNER.CONTENT_TYPES))
        self.assertEqual(
            len(set(WORKER._CONTENT_GUIDANCE.values())),
            len(PLANNER.CONTENT_TYPES),
        )
        for content_type in PLANNER.CONTENT_TYPES:
            with self.subTest(content_type=content_type):
                provider = self.successful_provider()
                WORKER.run_localization_job(job(content_type=content_type), assets(), provider)
                self.assertEqual(
                    provider.requests[0].input["content_guidance"],
                    WORKER._CONTENT_GUIDANCE[content_type],
                )

    def test_every_eu_locale_profile_routes_as_one_exact_worker_target(self):
        for profile in PLANNER.EU_OFFICIAL_LOCALES:
            with self.subTest(locale=profile.locale):
                payload = PLANNER.plan_website_localization(
                    source_id="homepage.hero",
                    source_revision="cms-184",
                    source_text="Build the BLUN website.",
                    source_locale="uk-UA",
                    content_type="headline",
                    glossary_version="blun-glossary-3",
                    policy_version="native-web-1",
                    provider_id="customer-llm",
                    model_id="king",
                    model_version="2026-08-29",
                    software_version="6.43.0-dev",
                    target_locales=[profile.locale],
                ).jobs[0].as_payload()
                localized = f"{profile.native_name} — BLUN."
                provider = ScriptedProvider([
                    candidate(localized, profile.locale),
                    review("target_native", locale=profile.locale),
                    review("source_fidelity", locale=profile.locale),
                ])
                result = WORKER.run_localization_job(
                    payload,
                    assets(audience="EU website visitors", glossary=()),
                    provider,
                )
                self.assertEqual(result["target_locale"], profile.locale)
                self.assertEqual(provider.requests[0].input["target"]["script"], profile.script)

    def test_wrong_locale_or_malformed_provider_response_blocks(self):
        for response in (
            candidate(locale="de-AT"),
            candidate("Fo\u0308retag med BLUN."),
            {**candidate(), "commentary": "looks good"},
            "not an object",
        ):
            with self.subTest(response=response):
                provider = ScriptedProvider([response])
                with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
                    WORKER.run_localization_job(job(), assets(), provider)
                self.assertEqual(caught.exception.code, "provider.response.invalid")

    def test_native_failure_blocks_before_source_is_disclosed(self):
        provider = ScriptedProvider([
            candidate(),
            review("target_native", "FAIL", findings=[{
                "class": "nativeness",
                "excerpt": "Bygg ditt företag",
                "reason": "Unnatural in this context",
            }]),
        ])
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(job(), assets(), provider)
        self.assertEqual(caught.exception.code, "review.target_native.failed")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(caught.exception.finding_hashes), 1)
        self.assertEqual(len(provider.requests), 2)

    def test_fidelity_failure_blocks_and_exposes_only_finding_hashes(self):
        provider = ScriptedProvider([
            candidate(),
            review("target_native"),
            review("source_fidelity", "FAIL", findings=[{
                "class": "completeness",
                "excerpt": "Bygg ditt företag",
                "reason": "The relationship changed",
            }]),
        ])
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(job(), assets(), provider)
        self.assertEqual(caught.exception.code, "review.source_fidelity.failed")
        self.assertNotIn("relationship", str(caught.exception))
        self.assertEqual(len(caught.exception.finding_hashes), 1)

    def test_local_integrity_gate_blocks_placeholder_or_html_structure_changes(self):
        source = '<p>Welcome, {{name}}. Visit <a href="https://blun.ai">BLUN</a>.</p>'
        broken = '<p>Välkommen! Besök <a href="https://evil.example">BLUN</a>.</p>'
        provider = self.successful_provider(broken)
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(job(source, "marketing"), assets(), provider)
        self.assertEqual(caught.exception.code, "integrity.failed")
        self.assertTrue(caught.exception.finding_hashes)

    def test_asset_version_mismatch_blocks_before_provider_call(self):
        provider = self.successful_provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(
                job(),
                assets(policy_version="stale-policy"),
                provider,
            )
        self.assertEqual(caught.exception.code, "assets.version_mismatch")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(provider.requests, [])

    def test_job_binding_tamper_blocks_before_provider_call(self):
        payload = job()
        payload["target"]["native_name"] = "Deutsch"
        provider = self.successful_provider()
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(payload, assets(), provider)
        self.assertEqual(caught.exception.code, "job.binding_mismatch")
        self.assertEqual(provider.requests, [])

    def test_provider_failure_is_content_free_and_preserves_retryability(self):
        provider = ScriptedProvider([
            WORKER.ProviderCallFailed("timeout", retryable=True),
        ])
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(job(), assets(), provider)
        self.assertEqual(caught.exception.code, "provider.timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), "provider.timeout")

    def test_missing_provider_blocks_without_inventing_a_translation(self):
        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(job(), assets(), object())
        self.assertEqual(caught.exception.code, "provider.adapter.invalid")
        self.assertFalse(caught.exception.retryable)

    def test_provider_cannot_mutate_a_hashed_request(self):
        class MutatingProvider:
            def invoke(self, request):
                request.input["target"]["locale"] = "de-AT"
                return candidate()

        with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
            WORKER.run_localization_job(job(), assets(), MutatingProvider())
        self.assertEqual(caught.exception.code, "provider.adapter.mutated_request")
        self.assertFalse(caught.exception.retryable)

    def test_legal_content_requires_human_review_even_after_all_passes(self):
        provider = self.successful_provider("Genom att fortsätta godkänner du villkoren.")
        result = WORKER.run_localization_job(job("By continuing, you accept the terms.", "legal"), assets(), provider)
        self.assertTrue(result["human_review_required"])

    def test_pass_result_retains_no_reviewer_prose(self):
        provider = self.successful_provider()
        result = WORKER.run_localization_job(job(), assets(), provider)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("reason", serialized)
        self.assertNotIn("excerpt", serialized)
        for item in result["quality_passes"]:
            self.assertEqual(set(item), {"phase", "request_sha256", "response_sha256", "status"})


if __name__ == "__main__":
    unittest.main()
