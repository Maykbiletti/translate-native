from __future__ import annotations

import copy
import hashlib
import importlib.util
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


PLANNER = load("blun_test_benchmark_planner", ROOT / "integrations" / "website_localization.py")
WORKER = load("blun_test_benchmark_worker", ROOT / "integrations" / "website_localization_worker.py")
BENCHMARK = load("blun_test_website_localization_benchmark", ROOT / "integrations" / "website_localization_benchmark.py")
SUITE = load("blun_test_website_localization_benchmark_suite", ROOT / "integrations" / "website_localization_benchmark_suite.py")
SUITE_MANIFEST = SUITE.manifest()


TARGETS = {
    "mt-MT": {
        "candidate": "Kabbar in-negozju tiegħek ma’ BLUN.",
        "baseline": "Ibni n-negozju tiegħek ma’ BLUN.",
        "audience": "Sidien ta’ negozji żgħar f’Malta",
    },
    "fi-FI": {
        "candidate": "Kasvata yritystäsi BLUNin avulla.",
        "baseline": "Rakenna yrityksesi BLUNin kanssa.",
        "audience": "Suomalaiset pienyrittäjät",
    },
}


def policy(**overrides):
    values = {
        "benchmark_version": "native-vs-baseline-1",
        "suite_version": SUITE_MANIFEST["version"],
        "suite_sha256": SUITE_MANIFEST["sha256"],
        "candidate_provider_id": "customer-llm",
        "candidate_model_id": "king",
        "candidate_model_version": "2026-08-30",
        "candidate_software_version": "6.43.0-dev",
        "candidate_worker_schema": WORKER.WORKER_SCHEMA,
        "candidate_glossary_version": "blun-glossary-3",
        "candidate_policy_version": "native-web-2",
        "baseline_id": "deepl-official-api",
        "baseline_version": "fixture-2026-08-30",
        "reviewer_id": "independent-native-panel",
        "reviewer_version": "2026-08-30",
        "required_locales": ("mt-MT", "fi-FI"),
        "minimum_cases_per_locale": len(SUITE.SOURCE_CASES),
        "minimum_decisive_rate": 0.75,
        "minimum_candidate_win_rate": 0.60,
        "maximum_one_sided_p": 0.05,
    }
    values.update(overrides)
    return BENCHMARK.BenchmarkPolicy(**values)


def job(locale="mt-MT", suffix="1"):
    final = str(suffix).rsplit("-", 1)[-1]
    case_index = int(final) if final.isdigit() else 0
    case = SUITE.SOURCE_CASES[case_index % len(SUITE.SOURCE_CASES)].as_payload()
    return PLANNER.plan_website_localization(
        source_id=case["source_id"],
        source_revision=case["source_revision"],
        source_text=case["source_text"],
        source_locale=case["source_locale"],
        content_type=case["content_type"],
        glossary_version="blun-glossary-3",
        policy_version="native-web-2",
        provider_id="customer-llm",
        model_id="king",
        model_version="2026-08-30",
        software_version="6.43.0-dev",
        target_locales=[locale],
    ).jobs[0].as_payload()


def assets(locale="mt-MT"):
    target = "negozju" if locale == "mt-MT" else "yritys"
    return WORKER.LocalizationAssets(
        glossary_version="blun-glossary-3",
        policy_version="native-web-2",
        audience=TARGETS[locale]["audience"],
        tone_profile="Natural, confident, warm, concise, and never inflated",
        glossary=(WORKER.GlossaryTerm("business", target),),
        protected_terms=("BLUN",),
    )


def candidate_result(payload, text=None):
    text = _target_fixture(payload, "candidate") if text is None else text
    phase = lambda name: {
        "phase": name,
        "request_sha256": hashlib.sha256((name + "-request").encode()).hexdigest(),
        "response_sha256": hashlib.sha256((name + "-response").encode()).hexdigest(),
        "status": "PASS",
    }
    return {
        "schema": WORKER.RESULT_SCHEMA,
        "worker_schema": WORKER.WORKER_SCHEMA,
        "job_id": payload["job_id"],
        "source_sha256": payload["source"]["sha256"],
        "target_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_locale": payload["source"]["locale"],
        "target_locale": payload["target"]["locale"],
        "content_type": payload["content_type"],
        "glossary_version": payload["glossary_version"],
        "policy_version": payload["policy_version"],
        "provider": payload["provider"],
        "software_version": payload["software_version"],
        "candidate": text,
        "quality_passes": [phase(name) for name in ("transcreation", "target_native", "source_fidelity")],
        "integrity": {"status": "PASS", "guard": "translate-native-structure-and-token-gate"},
        "review_confidence": {"target_native": "high", "source_fidelity": "high"},
        "quality_profile": {
            "locale": payload["target"]["locale"],
            "version": payload["target"]["quality_profile_version"],
            "sha256": payload["target"]["quality_profile_sha256"],
        },
        "human_review_required": payload["content_type"] == "legal",
        "independent_review_required": False,
        "release_required": True,
    }


def baseline(payload, text=None):
    text = _target_fixture(payload, "baseline") if text is None else text
    return {
        "schema": BENCHMARK.BASELINE_SCHEMA,
        "baseline_id": "deepl-official-api",
        "baseline_version": "fixture-2026-08-30",
        "source_sha256": payload["source"]["sha256"],
        "target_locale": payload["target"]["locale"],
        "content_type": payload["content_type"],
        "target_text": text,
        "target_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def _target_fixture(payload, variant):
    locale = payload["target"]["locale"]
    phrase = TARGETS[locale][variant]
    source_id = payload["source"]["id"]
    if source_id.endswith("travel-marketing"):
        return (
            f'<section><h2>{phrase}</h2><p>{phrase} {{{{season}}}}.</p>'
            '<a href="https://example.test/routes">Route</a></section>'
        )
    if source_id.endswith("payments-ui"):
        return json.dumps(
            {"error": phrase, "retry": phrase}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
    repetitions = max(
        1,
        (len(payload["source"]["text"]) + len(phrase) - 1) // len(phrase),
    )
    return " ".join((phrase,) * repetitions)


def review_response(request, preference, defects=None):
    defects = {} if defects is None else defects
    variants = {}
    for label in ("A", "B"):
        variants[label] = {
            "blocking_defects": defects.get(label, {}).get("blocking", []),
            "major_defects": defects.get(label, {}).get("major", []),
        }
    return {
        "schema": BENCHMARK.REVIEW_SCHEMA,
        "phase": request.phase,
        "target_locale": request.target_locale,
        "blind_id": request.input["blind_id"],
        "preference": preference,
        "variants": variants,
    }


class PreferenceReviewer:
    def __init__(self, preferred_text, *, prefer="preferred", defects=None):
        self.preferred_text = preferred_text
        self.prefer = prefer
        self.defects = defects
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        by_text = {item["text"]: item["label"] for item in request.input["variants"]}
        label = by_text[self.preferred_text]
        if self.prefer == "other":
            label = "B" if label == "A" else "A"
        elif self.prefer == "tie":
            label = "tie"
        return review_response(request, label, self.defects)


class WebsiteLocalizationBenchmarkTests(unittest.TestCase):
    key = b"benchmark-host-secret-key-material-32"

    def run_case(self, locale="mt-MT", suffix="1", *, prefer="preferred", baseline_text=None):
        payload = job(locale, suffix)
        result = candidate_result(payload)
        reviewer = PreferenceReviewer(result["candidate"], prefer=prefer)
        outcome = BENCHMARK.run_blind_benchmark_case(
            payload,
            result,
            baseline(payload, baseline_text),
            assets(locale),
            policy(),
            reviewer,
            blinding_key=self.key,
        )
        return outcome, reviewer

    def test_runs_two_ordered_origin_blind_reviews(self):
        outcome, reviewer = self.run_case()
        source_text = job()["source"]["text"]
        self.assertEqual([item.phase for item in reviewer.requests], ["target_native", "source_fidelity"])
        native = json.dumps(reviewer.requests[0].as_payload(), ensure_ascii=False)
        fidelity = json.dumps(reviewer.requests[1].as_payload(), ensure_ascii=False)
        self.assertNotIn(source_text, native)
        self.assertNotIn("customer-llm", native + fidelity)
        self.assertNotIn("deepl", (native + fidelity).lower())
        self.assertNotIn('"origin"', native + fidelity)
        self.assertNotIn('"case_key"', native)
        self.assertNotIn('"adversarial_tags"', native)
        self.assertIn('"case_key"', fidelity)
        self.assertIn('"adversarial_tags"', fidelity)
        self.assertIn(source_text, fidelity)
        self.assertEqual(
            reviewer.requests[0].input["benchmark_suite"]["sha256"],
            SUITE_MANIFEST["sha256"],
        )
        self.assertEqual(outcome["winner"], "candidate")

    def test_blinding_is_reproducible_and_keyed(self):
        payload = job()
        result = candidate_result(payload)
        base = baseline(payload)
        first_reviewer = PreferenceReviewer(result["candidate"])
        first = BENCHMARK.run_blind_benchmark_case(
            payload, result, base, assets(), policy(), first_reviewer, blinding_key=self.key,
        )
        second_reviewer = PreferenceReviewer(result["candidate"])
        second = BENCHMARK.run_blind_benchmark_case(
            payload, result, base, assets(), policy(), second_reviewer, blinding_key=self.key,
        )
        self.assertEqual(first, second)
        self.assertEqual(first_reviewer.requests[0].input["variants"], second_reviewer.requests[0].input["variants"])
        commitments = {first["blind_commitment_sha256"]}
        for index in range(1, 8):
            reviewer = PreferenceReviewer(result["candidate"])
            changed = BENCHMARK.run_blind_benchmark_case(
                payload, result, base, assets(), policy(), reviewer,
                blinding_key=(f"different-key-{index:02d}".encode() * 3)[:32],
            )
            commitments.add(changed["blind_commitment_sha256"])
        self.assertGreater(len(commitments), 1)

    def test_maltese_and_finnish_contexts_preserve_native_profiles(self):
        for locale in ("mt-MT", "fi-FI"):
            with self.subTest(locale=locale):
                outcome, reviewer = self.run_case(locale)
                request = reviewer.requests[0]
                self.assertEqual(request.target_locale, locale)
                self.assertEqual(request.input["target"]["native_name"], "Malti" if locale == "mt-MT" else "suomi")
                self.assertEqual(
                    request.input["quality_profile"],
                    PLANNER.quality_profile_for(locale),
                )
                marker = "għ" if locale == "mt-MT" else "ä"
                self.assertIn(marker, TARGETS[locale]["candidate"])
                self.assertEqual(outcome["winner"], "candidate")

    def test_wrong_baseline_binding_blocks_before_review(self):
        payload = job()
        base = baseline(payload)
        for field, value in (
            ("source_sha256", "0" * 64),
            ("target_locale", "fi-FI"),
            ("baseline_version", "stale"),
            ("target_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                changed = {**base, field: value}
                reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    BENCHMARK.run_blind_benchmark_case(
                        payload, candidate_result(payload), changed, assets(), policy(), reviewer,
                        blinding_key=self.key,
                    )
                self.assertEqual(caught.exception.code, "benchmark.baseline.binding_mismatch")
                self.assertEqual(reviewer.requests, [])

    def test_wrong_candidate_binding_blocks_before_review(self):
        payload = job()
        result = candidate_result(payload)
        result["target_sha256"] = "0" * 64
        reviewer = PreferenceReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.run_blind_benchmark_case(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.candidate.binding_mismatch")
        self.assertEqual(reviewer.requests, [])

    def test_candidate_policy_mismatch_blocks_before_review(self):
        payload = job()
        result = candidate_result(payload)
        mismatches = {
            "candidate_provider_id": "other-provider",
            "candidate_model_id": "other-model",
            "candidate_model_version": "other-version",
            "candidate_software_version": "other-software",
            "candidate_worker_schema": "other-worker-schema",
            "candidate_glossary_version": "other-glossary",
            "candidate_policy_version": "other-policy",
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                reviewer = PreferenceReviewer(result["candidate"])
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    BENCHMARK.run_blind_benchmark_case(
                        payload, result, baseline(payload), assets(),
                        policy(**{field: value}), reviewer, blinding_key=self.key,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "benchmark.candidate.policy_mismatch",
                )
                self.assertEqual(reviewer.requests, [])

    def test_substituted_candidate_quality_profile_blocks_before_review(self):
        payload = job()
        result = candidate_result(payload)
        result["quality_profile"]["version"] = "stale-profile"
        reviewer = PreferenceReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.run_blind_benchmark_case(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.candidate.binding_mismatch")
        self.assertEqual(reviewer.requests, [])

    def test_unverified_candidate_phases_and_invalid_policy_block(self):
        payload = job()
        result = candidate_result(payload)
        result["quality_passes"][1]["status"] = "FAIL"
        reviewer = PreferenceReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.run_blind_benchmark_case(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.candidate.invalid")
        self.assertEqual(reviewer.requests, [])

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.summarize_benchmark(policy(minimum_cases_per_locale="six"), [])
        self.assertEqual(caught.exception.code, "benchmark.policy.invalid")

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.summarize_benchmark(policy(suite_sha256="0" * 64), [])
        self.assertEqual(caught.exception.code, "benchmark.suite.version_mismatch")

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.summarize_benchmark(policy(required_locales=("mt-MT",)), [])
        self.assertEqual(caught.exception.code, "benchmark.policy.invalid")

    def test_preferred_variant_cannot_have_major_or_blocking_defect(self):
        payload = job()
        result = candidate_result(payload)

        class DefectiveReviewer(PreferenceReviewer):
            def review(self, request):
                self.requests.append(request)
                label = next(item["label"] for item in request.input["variants"] if item["text"] == self.preferred_text)
                finding = {"class": "nativeness", "excerpt": "test", "reason": "major defect"}
                return review_response(request, label, {label: {"major": [finding]}})

        reviewer = DefectiveReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.run_blind_benchmark_case(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.review.invalid")

    def test_split_or_tied_judgment_is_not_a_win(self):
        payload = job()
        result = candidate_result(payload)

        class SplitReviewer(PreferenceReviewer):
            def review(self, request):
                self.requests.append(request)
                by_text = {item["text"]: item["label"] for item in request.input["variants"]}
                label = by_text[self.preferred_text]
                if request.phase == "source_fidelity":
                    label = "B" if label == "A" else "A"
                return review_response(request, label)

        outcome = BENCHMARK.run_blind_benchmark_case(
            payload, result, baseline(payload), assets(), policy(), SplitReviewer(result["candidate"]),
            blinding_key=self.key,
        )
        self.assertEqual(outcome["winner"], "inconclusive")

        tied, _ = self.run_case(prefer="tie")
        self.assertEqual(tied["winner"], "inconclusive")

    def test_local_integrity_failure_cannot_be_a_candidate_win(self):
        payload = job("mt-MT", "2")
        broken = '<section><h2>Kabbar</h2><p>Kabbar.</p><a href="https://evil.example">Route</a></section>'
        result = candidate_result(payload, broken)
        base = baseline(payload)
        reviewer = PreferenceReviewer(broken)
        outcome = BENCHMARK.run_blind_benchmark_case(
            payload, result, base, assets(), policy(), reviewer, blinding_key=self.key,
        )
        self.assertEqual(outcome["integrity"]["candidate"]["status"], "FAIL")
        self.assertEqual(outcome["winner"], "inconclusive")

    def test_case_result_retains_no_text_or_reviewer_prose(self):
        outcome, _ = self.run_case()
        serialized = json.dumps(outcome, ensure_ascii=False)
        self.assertNotIn("Kabbar", serialized)
        self.assertNotIn("Ibni", serialized)
        self.assertNotIn(job()["source"]["text"], serialized)
        self.assertNotIn("excerpt", serialized)
        self.assertNotIn("reason", serialized)

    def test_report_requires_each_locale_to_win_with_significance(self):
        results = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES)):
                outcome, _ = self.run_case(locale, f"{locale}-{index}")
                results.append(outcome)
        report = BENCHMARK.summarize_benchmark(policy(), results)
        self.assertTrue(report["superiority_claim_allowed"])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["candidate"], {
            "provider": {
                "id": "customer-llm",
                "model_id": "king",
                "model_version": "2026-08-30",
            },
            "software_version": "6.43.0-dev",
            "glossary_version": "blun-glossary-3",
            "policy_version": "native-web-2",
            "worker_schema": WORKER.WORKER_SCHEMA,
        })
        self.assertEqual(
            report["quality_profiles"],
            [
                {
                    "locale": locale,
                    "version": PLANNER.quality_profile_for(locale)["version"],
                    "sha256": PLANNER.quality_profile_for(locale)["sha256"],
                }
                for locale in ("mt-MT", "fi-FI")
            ],
        )
        for item in report["locales"]:
            self.assertEqual(item["candidate_wins"], len(SUITE.SOURCE_CASES))
            self.assertEqual(item["one_sided_sign_p"], 0.00390625)
            self.assertTrue(item["suite_complete"])
            self.assertEqual(set(item["content_types"]), set(PLANNER.CONTENT_TYPES))
            self.assertEqual(item["long_form_cases"], 2)
            self.assertGreaterEqual(len(item["domains"]), 6)
            self.assertIn("marketing_calque", item["adversarial_tags"])

    def test_non_suite_job_blocks_before_review(self):
        payload = PLANNER.plan_website_localization(
            source_id="benchmark.unregistered", source_revision=SUITE.VERSION,
            source_text="A valid source that was never registered in the suite.",
            source_locale="en-US", content_type="headline",
            glossary_version="blun-glossary-3", policy_version="native-web-2",
            provider_id="customer-llm", model_id="king", model_version="2026-08-30",
            software_version="6.43.0-dev", target_locales=["mt-MT"],
        ).jobs[0].as_payload()
        reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.run_blind_benchmark_case(
                payload, candidate_result(payload), baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.suite.case_mismatch")
        self.assertEqual(reviewer.requests, [])

    def test_strong_maltese_average_cannot_hide_finnish_losses(self):
        results = []
        for index in range(len(SUITE.SOURCE_CASES)):
            outcome, _ = self.run_case("mt-MT", f"mt-{index}")
            results.append(outcome)
        for index in range(len(SUITE.SOURCE_CASES)):
            outcome, _ = self.run_case("fi-FI", f"fi-{index}", prefer="other")
            results.append(outcome)
        report = BENCHMARK.summarize_benchmark(policy(), results)
        self.assertFalse(report["superiority_claim_allowed"])
        by_locale = {item["locale"]: item for item in report["locales"]}
        self.assertEqual(by_locale["mt-MT"]["status"], "PASS")
        self.assertEqual(by_locale["fi-FI"]["status"], "BLOCK")

    def test_small_or_inconclusive_sample_never_claims_superiority(self):
        results = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES) - 1):
                outcome, _ = self.run_case(locale, f"small-{locale}-{index}")
                results.append(outcome)
        report = BENCHMARK.summarize_benchmark(policy(), results)
        self.assertFalse(report["superiority_claim_allowed"])

        tied = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES)):
                outcome, _ = self.run_case(locale, f"tie-{locale}-{index}", prefer="tie")
                tied.append(outcome)
        report = BENCHMARK.summarize_benchmark(policy(), tied)
        self.assertFalse(report["superiority_claim_allowed"])

    def test_duplicate_cases_and_mixed_versions_block(self):
        result, _ = self.run_case()
        with self.assertRaises(BENCHMARK.BenchmarkBlocked):
            BENCHMARK.summarize_benchmark(policy(), [result, result])
        disguised_duplicate = copy.deepcopy(result)
        disguised_duplicate["case_id"] = "benchmark-case-" + "0" * 64
        with self.assertRaises(BENCHMARK.BenchmarkBlocked):
            BENCHMARK.summarize_benchmark(policy(), [result, disguised_duplicate])
        changed = copy.deepcopy(result)
        changed["benchmark_version"] = "other"
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.summarize_benchmark(policy(), [changed])
        self.assertEqual(caught.exception.code, "benchmark.results.version_mismatch")

    def test_complete_suite_is_required_even_with_a_lower_case_threshold(self):
        results = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES) - 1):
                outcome, _ = self.run_case(locale, f"partial-{locale}-{index}")
                results.append(outcome)
        report = BENCHMARK.summarize_benchmark(
            policy(minimum_cases_per_locale=len(SUITE.SOURCE_CASES) - 1),
            results,
        )
        self.assertFalse(report["superiority_claim_allowed"])
        self.assertTrue(all(not item["suite_complete"] for item in report["locales"]))

    def test_report_rejects_tampered_winner_and_nested_shapes(self):
        result, _ = self.run_case()
        changed = copy.deepcopy(result)
        changed["winner"] = "baseline"
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.summarize_benchmark(policy(), [changed])
        self.assertEqual(caught.exception.code, "benchmark.results.invalid")

        for field, value in (
            ("job_id", "blun-l10n-" + "0" * 64),
            ("candidate", {**result["candidate"], "software_version": "stale"}),
            ("quality_profile", {**result["quality_profile"], "version": "stale"}),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(result)
                changed[field] = value
                with self.assertRaises(BENCHMARK.BenchmarkBlocked):
                    BENCHMARK.summarize_benchmark(policy(), [changed])

        changed = copy.deepcopy(result)
        changed["integrity"]["candidate"] = "PASS"
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.summarize_benchmark(policy(), [changed])
        self.assertEqual(caught.exception.code, "benchmark.results.invalid")


if __name__ == "__main__":
    unittest.main()
