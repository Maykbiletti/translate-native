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

    def test_all_eu_locales_have_distinct_bound_commercial_profiles(self):
        payloads = [
            PROFILES.commercial_quality_profile_for(
                profile.locale,
                PLANNER.COMMERCIAL_PROFILE,
            )
            for profile in PROFILES.PROFILES
        ]
        self.assertEqual(len(payloads), 24)
        self.assertEqual(len({item["version"] for item in payloads}), 24)
        self.assertEqual(len({item["sha256"] for item in payloads}), 24)
        for item in payloads:
            with self.subTest(locale=item["locale"]):
                general = PROFILES.quality_profile_for(item["locale"])
                claimed = item.pop("sha256")
                canonical = json.dumps(
                    item, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(claimed, hashlib.sha256(canonical).hexdigest())
                self.assertEqual(item["schema"], PROFILES.COMMERCIAL_SCHEMA)
                self.assertEqual(
                    item["commercial_profile"], PLANNER.COMMERCIAL_PROFILE,
                )
                self.assertEqual(
                    item["quality_profile_sha256"], general["sha256"],
                )
                self.assertEqual(
                    item["required_commercial_checks"],
                    list(PROFILES.COMMERCIAL_REVIEW_CHECKS),
                )
                self.assertEqual(item["source_refs"], general["source_refs"])
                rendering = item["rendering_reference"]
                self.assertEqual(
                    rendering["schema"], PROFILES.COMMERCIAL_RENDERING_SCHEMA,
                )
                self.assertEqual(rendering["locale"], item["locale"])
                self.assertEqual(rendering["source"]["authority"], "Unicode CLDR")
                self.assertEqual(rendering["source"]["version"], "48")
                self.assertIn("/48.0.0/", rendering["source"]["url"])
                self.assertEqual(rendering["numbering_system"], "latn")
                self.assertEqual(rendering["native_numbering_system"], "latn")
                self.assertFalse(rendering["application"]["semantic_proof"])
                self.assertTrue(
                    rendering["application"]["allow_equivalent_number_words"]
                )
                self.assertEqual(
                    rendering["application"]["ambiguous_values"],
                    "targeted-review",
                )
                for field in (
                    "creation_focus", "native_review_focus",
                    "fidelity_review_focus", "adversarial_focus",
                ):
                    self.assertGreaterEqual(len(item[field]), 2)
                self.assertTrue(
                    unicodedata.is_normalized("NFC", canonical.decode("utf-8"))
                )

    def test_cldr_48_commercial_patterns_preserve_exact_locale_overrides(self):
        maltese = PROFILES.commercial_quality_profile_for(
            "mt-MT", PLANNER.COMMERCIAL_PROFILE,
        )["rendering_reference"]
        finnish = PROFILES.commercial_quality_profile_for(
            "fi-FI", PLANNER.COMMERCIAL_PROFILE,
        )["rendering_reference"]
        austrian = PROFILES.commercial_quality_profile_for(
            "de-AT", PLANNER.COMMERCIAL_PROFILE,
        )["rendering_reference"]
        portuguese = PROFILES.commercial_quality_profile_for(
            "pt-PT", PLANNER.COMMERCIAL_PROFILE,
        )["rendering_reference"]

        self.assertEqual(maltese["source"]["locale"], "mt")
        self.assertEqual(maltese["symbols"], {"decimal": ".", "group": ","})
        self.assertEqual(maltese["patterns"]["currency"], "¤#,##0.00")
        self.assertEqual(maltese["patterns"]["percent"], "#,##0%")

        self.assertEqual(finnish["source"]["locale"], "fi")
        self.assertEqual(finnish["symbols"]["group"], "\N{NO-BREAK SPACE}")
        self.assertEqual(finnish["patterns"]["percent"], "#,##0\N{NO-BREAK SPACE}%")
        self.assertEqual(finnish["patterns"]["at_least"], "vähintään {0}")

        self.assertEqual(austrian["source"]["locale"], "de-AT")
        self.assertEqual(austrian["symbols"]["group"], "\N{NO-BREAK SPACE}")
        self.assertEqual(
            austrian["patterns"]["currency"],
            "¤\N{NO-BREAK SPACE}#,##0.00",
        )
        self.assertEqual(portuguese["source"]["locale"], "pt-PT")
        self.assertEqual(portuguese["minimum_grouping_digits"], 2)
        self.assertEqual(portuguese["symbols"]["group"], "\N{NO-BREAK SPACE}")

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

        commercial = PROFILES.commercial_quality_profile_for(
            "mt-MT", PLANNER.COMMERCIAL_PROFILE,
        )
        commercial["native_review_focus"].append("tampered")
        fresh = PROFILES.commercial_quality_profile_for(
            "mt-MT", PLANNER.COMMERCIAL_PROFILE,
        )
        self.assertNotIn("tampered", fresh["native_review_focus"])
        commercial["rendering_reference"]["patterns"]["currency"] = "tampered"
        self.assertNotEqual(
            commercial["rendering_reference"],
            PROFILES.commercial_quality_profile_for(
                "mt-MT", PLANNER.COMMERCIAL_PROFILE,
            )["rendering_reference"],
        )
        maltese = json.dumps(fresh, ensure_ascii=False).casefold()
        for marker in ("ċ", "ġ", "għ", "ħ", "ż", "english", "italian"):
            self.assertIn(marker, maltese)
        for invalid in ("mt", "mt-mt", "it-MT", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PROFILES.commercial_quality_profile_for(
                    invalid, PLANNER.COMMERCIAL_PROFILE,
                )


if __name__ == "__main__":
    unittest.main()
