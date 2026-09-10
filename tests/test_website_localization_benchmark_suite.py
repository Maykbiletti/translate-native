from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unicodedata
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


PLANNER = load("blun_test_suite_planner", ROOT / "integrations" / "website_localization.py")
SUITE = load(
    "blun_test_website_localization_benchmark_suite",
    ROOT / "integrations" / "website_localization_benchmark_suite.py",
)
COMMERCIAL = load(
    "blun_test_benchmark_commercial_profile",
    ROOT / "integrations" / "commercial_localization_profile.py",
)


class WebsiteLocalizationBenchmarkSuiteTests(unittest.TestCase):
    def test_manifest_is_canonical_complete_and_output_free(self):
        manifest = SUITE.manifest()
        claimed_hash = manifest.pop("sha256")
        encoded = json.dumps(
            manifest, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode()
        self.assertEqual(claimed_hash, hashlib.sha256(encoded).hexdigest())
        self.assertEqual(len(manifest["cases"]), 64)
        self.assertEqual(
            {case["content_type"] for case in manifest["cases"]},
            set(PLANNER.CONTENT_TYPES),
        )
        self.assertGreaterEqual(len({case["domain"] for case in manifest["cases"]}), 6)
        self.assertGreaterEqual(sum(case["long_form"] for case in manifest["cases"]), 6)
        commercial = [
            case for case in manifest["cases"]
            if case["content_type"] == "commercial"
        ]
        self.assertEqual(len(commercial), 8)
        self.assertGreaterEqual(len({case["domain"] for case in commercial}), 8)
        self.assertGreaterEqual(sum(case["long_form"] for case in commercial), 5)
        commercial_evaluation = manifest["commercial_evaluation"]
        self.assertEqual(
            commercial_evaluation["schema"],
            SUITE.COMMERCIAL_EVALUATION_SCHEMA,
        )
        self.assertEqual(
            commercial_evaluation["review_summary_schema"],
            COMMERCIAL.REVIEW_SUMMARY_SCHEMA,
        )
        self.assertEqual(
            commercial_evaluation["dimensions"], list(COMMERCIAL.DIMENSIONS),
        )
        self.assertEqual(
            commercial_evaluation["case_keys"],
            [case["key"] for case in commercial],
        )
        self.assertEqual(
            commercial_evaluation["cases_per_dimension"], len(commercial),
        )
        self.assertEqual(
            commercial_evaluation["source_blind_native_exposure"], "none",
        )
        self.assertEqual(
            commercial_evaluation["source_fidelity_scope"],
            "all-dimensions-every-commercial-case",
        )
        for case in manifest["cases"]:
            if case["content_type"] == "commercial":
                self.assertEqual(
                    case["commercial_dimensions"], list(COMMERCIAL.DIMENSIONS),
                )
            else:
                self.assertNotIn("commercial_dimensions", case)
        serialized = json.dumps(manifest, ensure_ascii=False).lower()
        for forbidden in ("target_text", "candidate_text", "baseline_text", "reference_translation"):
            self.assertNotIn(forbidden, serialized)

    def test_sources_are_nfc_hash_bound_and_adversarial(self):
        cases = SUITE.manifest()["cases"]
        for case in cases:
            with self.subTest(case=case["key"]):
                self.assertTrue(unicodedata.is_normalized("NFC", case["source_text"]))
                self.assertEqual(
                    case["source_sha256"],
                    hashlib.sha256(case["source_text"].encode()).hexdigest(),
                )
                self.assertTrue(case["adversarial_tags"])
                self.assertEqual(
                    case["long_form"],
                    len(case["source_text"]) >= SUITE.LONG_FORM_MINIMUM_CHARACTERS,
                )
        by_key = {case["key"]: case for case in cases}
        self.assertIn("html_integrity", by_key["travel-marketing"]["adversarial_tags"])
        self.assertIn("json_integrity", by_key["payments-ui"]["adversarial_tags"])
        offer_tags = set(by_key["offer-commercial-long"]["adversarial_tags"])
        self.assertTrue(
            {"amount", "currency", "discount_basis", "tax", "billing_interval",
             "contract_term", "renewal", "cancellation"}.issubset(offer_tags)
        )
        commercial_tags = {
            tag for case in cases if case["content_type"] == "commercial"
            for tag in case["adversarial_tags"]
        }
        self.assertTrue(
            {"qualifier", "deposit", "refund", "trial", "proration",
             "tiered_price", "surcharge_basis", "offer_assignment"}.issubset(
                commercial_tags
            )
        )

    def test_every_case_binds_to_every_eligible_eu_locale_profile(self):
        locales = tuple(
            profile.locale for profile in PLANNER.EU_OFFICIAL_LOCALES
            if profile.language != "en"
        )
        self.assertEqual(len(locales), 23)
        observed_profiles = set()
        for source_case in SUITE.SOURCE_CASES:
            case = source_case.as_payload()
            canonical_case = next(
                item for item in SUITE.manifest()["cases"] if item["key"] == case["key"]
            )
            for locale in locales:
                with self.subTest(case=case["key"], locale=locale):
                    job = PLANNER.plan_website_localization(
                        source_id=case["source_id"],
                        source_revision=case["source_revision"],
                        source_text=case["source_text"],
                        source_locale=case["source_locale"],
                        content_type=case["content_type"],
                        glossary_version="benchmark-glossary-1",
                        policy_version="benchmark-policy-1",
                        provider_id="customer-provider",
                        model_id="customer-model",
                        model_version="1",
                        software_version="test",
                        target_locales=[locale],
                    ).jobs[0].as_payload()
                    self.assertEqual(SUITE.case_for_job(job), canonical_case)
                    observed_profiles.add(
                        (locale, job["target"]["quality_profile_version"],
                         job["target"]["quality_profile_sha256"])
                    )
        self.assertEqual(len(observed_profiles), 23)

    def test_tampered_or_unregistered_job_is_rejected(self):
        case = SUITE.SOURCE_CASES[0].as_payload()
        job = PLANNER.plan_website_localization(
            source_id=case["source_id"], source_revision=case["source_revision"],
            source_text=case["source_text"], source_locale=case["source_locale"],
            content_type=case["content_type"], glossary_version="benchmark-glossary-1",
            policy_version="benchmark-policy-1", provider_id="customer-provider",
            model_id="customer-model", model_version="1", software_version="test",
            target_locales=["mt-MT"],
        ).jobs[0].as_payload()
        mutations = (
            ("text", "Different source"),
            ("locale", "en-MT"),
            ("id", "benchmark.unknown"),
            ("revision", "stale-suite"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                changed = json.loads(json.dumps(job))
                changed["source"][field] = value
                with self.assertRaises(ValueError):
                    SUITE.case_for_job(changed)


if __name__ == "__main__":
    unittest.main()
