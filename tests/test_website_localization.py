from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "integrations" / "website_localization.py"
SPEC = importlib.util.spec_from_file_location("blun_test_website_localization", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def request(**overrides):
    value = {
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
    }
    value.update(overrides)
    return value


class WebsiteLocalizationPlannerTests(unittest.TestCase):
    def test_official_registry_has_24_unique_language_profiles(self) -> None:
        profiles = MODULE.EU_OFFICIAL_LOCALES
        self.assertEqual(len(profiles), 24)
        self.assertEqual(len({profile.language for profile in profiles}), 24)
        self.assertEqual(len({profile.locale for profile in profiles}), 24)
        self.assertEqual({profile.script for profile in profiles}, {"Latn", "Cyrl", "Grek"})
        for profile in profiles:
            self.assertTrue(unicodedata.is_normalized("NFC", profile.native_name))

    def test_eu_source_creates_one_job_for_each_other_language(self) -> None:
        plan = MODULE.plan_website_localization(**request())
        self.assertEqual(len(plan.jobs), 23)
        self.assertNotIn("en-IE", {job.target.locale for job in plan.jobs})
        self.assertEqual(len({job.job_id for job in plan.jobs}), 23)
        for job in plan.jobs:
            payload = job.as_payload()
            self.assertEqual(payload["idempotency_key"], job.job_id)
            self.assertEqual(payload["quality_passes"], ["target_native", "source_fidelity"])
            self.assertTrue(payload["release_required"])

    def test_non_eu_source_creates_all_24_jobs(self) -> None:
        plan = MODULE.plan_website_localization(**request(source_locale="uk-UA"))
        self.assertEqual(len(plan.jobs), 24)

    def test_source_language_is_excluded_across_regional_tags(self) -> None:
        plan = MODULE.plan_website_localization(**request(source_locale="de-DE"))
        self.assertNotIn("de-AT", {job.target.locale for job in plan.jobs})
        self.assertEqual(len(plan.jobs), 23)

    def test_custom_targets_are_validated_deduplicated_and_canonically_ordered(self) -> None:
        plan = MODULE.plan_website_localization(
            **request(target_locales=["sv-se", "DE-at", "es-ES"]),
        )
        self.assertEqual(
            [job.target.locale for job in plan.jobs],
            ["de-AT", "es-ES", "sv-SE"],
        )
        for invalid in (["sv-SE", "sv-se"], ["en-IE"], ["uk-UA"], [], "sv-SE"):
            with self.subTest(invalid=invalid), self.assertRaises(MODULE.LocalizationPlanBlocked):
                MODULE.plan_website_localization(**request(target_locales=invalid))

    def test_retries_are_deterministic_but_every_release_input_invalidates_keys(self) -> None:
        baseline = MODULE.plan_website_localization(
            **request(target_locales=["sv-SE"]),
        )
        repeated = MODULE.plan_website_localization(
            **request(target_locales=["sv-SE"]),
        )
        self.assertEqual(baseline, repeated)
        mutations = {
            "source_id": "homepage.hero.secondary",
            "source_revision": "cms-185",
            "source_text": "Build a better business with BLUN.",
            "source_locale": "en-MT",
            "content_type": "cta",
            "glossary_version": "blun-glossary-4",
            "policy_version": "native-web-2",
            "provider_id": "another-provider",
            "model_id": "queen",
            "model_version": "2026-08-30",
            "software_version": "6.43.1-dev",
        }
        for field, changed in mutations.items():
            with self.subTest(field=field):
                mutated = MODULE.plan_website_localization(
                    **request(target_locales=["sv-SE"], **{field: changed}),
                )
                self.assertNotEqual(baseline.jobs[0].job_id, mutated.jobs[0].job_id)
                self.assertNotEqual(baseline.plan_id, mutated.plan_id)

    def test_source_hash_binds_exact_nfc_text(self) -> None:
        plan = MODULE.plan_website_localization(
            **request(source_text="Natürlich für Österreich.", target_locales=["sv-SE"]),
        )
        self.assertEqual(
            plan.source_hash,
            "4621d1676b3fe30e9f0fa1c0ce357060f0a20ddaa2d28efcfcee1954bda24d63",
        )
        with self.assertRaises(MODULE.LocalizationPlanBlocked):
            MODULE.plan_website_localization(
                **request(source_text="Natu\u0308rlich für Österreich.", target_locales=["sv-SE"]),
            )

    def test_wrong_types_nuls_unknown_fields_and_unsupported_content_block(self) -> None:
        invalid = (
            {"source_id": 7},
            {"source_revision": "bad\x00revision"},
            {"source_text": ["not", "text"]},
            {"source_locale": "auto"},
            {"content_type": "anything"},
            {"model_version": " "},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(MODULE.LocalizationPlanBlocked):
                MODULE.plan_website_localization(**request(**change))
        with self.assertRaises(MODULE.LocalizationPlanBlocked):
            MODULE.plan_from_mapping({**request(), "agent_override": True})

    def test_cli_emits_strict_manifest_and_fails_closed(self) -> None:
        passed = subprocess.run(
            [sys.executable, str(PATH)],
            input=json.dumps(request(target_locales=["sv-SE"]), ensure_ascii=False),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)
        payload = json.loads(passed.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["job_count"], 1)
        self.assertEqual(payload["jobs"][0]["target"]["locale"], "sv-SE")

        blocked = subprocess.run(
            [sys.executable, str(PATH)],
            input="{}",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(json.loads(blocked.stdout)["status"], "BLOCK")
        self.assertNotIn("Build your business", blocked.stdout)


if __name__ == "__main__":
    unittest.main()
