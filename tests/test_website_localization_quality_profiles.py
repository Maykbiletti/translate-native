from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROFILES = load(
    "blun_test_quality_profiles",
    ROOT / "integrations" / "website_localization_quality_profiles.py",
)
PLANNER = load(
    "blun_test_quality_profile_planner",
    ROOT / "integrations" / "website_localization.py",
)


class WebsiteLocalizationQualityProfileTests(unittest.TestCase):
    def test_all_eu_locales_have_distinct_versioned_canonical_profiles(self):
        payloads = [profile.as_payload() for profile in PROFILES.PROFILES]
        expected_locales = {profile.locale for profile in PLANNER.EU_OFFICIAL_LOCALES}
        self.assertEqual(len(payloads), 24)
        self.assertEqual({item["locale"] for item in payloads}, expected_locales)
        self.assertEqual(len({item["version"] for item in payloads}), 24)
        self.assertEqual(len({item["sha256"] for item in payloads}), 24)
        for item in payloads:
            with self.subTest(locale=item["locale"]):
                claimed = item.pop("sha256")
                canonical = json.dumps(
                    item, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(claimed, hashlib.sha256(canonical).hexdigest())
                self.assertTrue(unicodedata.is_normalized("NFC", canonical.decode("utf-8")))
                self.assertTrue(item["native_review_focus"])
                self.assertTrue(item["fidelity_review_focus"])
                self.assertTrue(item["adversarial_focus"])
                self.assertTrue(item["source_refs"])

    def test_every_locale_requires_the_complete_red_team_matrix(self):
        expected = list(PROFILES.REQUIRED_RED_TEAM_CHECKS)
        for profile in PROFILES.PROFILES:
            with self.subTest(locale=profile.locale):
                self.assertEqual(profile.as_payload()["required_red_team_checks"], expected)

    def test_maltese_and_finnish_profiles_cover_required_language_risks(self):
        maltese = PROFILES.quality_profile_for("mt-MT")
        finnish = PROFILES.quality_profile_for("fi-FI")
        mt_text = json.dumps(maltese, ensure_ascii=False).casefold()
        fi_text = json.dumps(finnish, ensure_ascii=False).casefold()
        for marker in ("ċ", "ġ", "għ", "ħ", "ż", "articles", "prepositions", "english", "italian"):
            self.assertIn(marker, mt_text)
        self.assertIn("kunsilltalmalti.gov.mt", mt_text)
        for marker in (
            "information structure", "case government", "agglutination",
            "possessive suffixes", "vowel harmony", "consonant gradation",
            "clitics", "compounds", "politeness", "ctas",
        ):
            self.assertIn(marker, fi_text)
        self.assertIn("unicode.org/cldr/charts/48", mt_text)
        self.assertIn("unicode.org/cldr/charts/48", fi_text)

    def test_lookup_is_fail_closed_and_returns_detached_payloads(self):
        first = PROFILES.quality_profile_for("fi-FI")
        first["native_review_focus"].append("tampered")
        self.assertNotIn("tampered", PROFILES.quality_profile_for("fi-FI")["native_review_focus"])
        for invalid in ("fi", "fi-fi", "sv-FI", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PROFILES.quality_profile_for(invalid)


if __name__ == "__main__":
    unittest.main()
