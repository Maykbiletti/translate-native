from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import json
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "translate-native" / "scripts" / "blun_language_guard.py"
SPEC = importlib.util.spec_from_file_location("blun_language_guard", SERVER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LanguageGuardMCPTests(unittest.TestCase):
    @staticmethod
    def _attestations() -> dict[str, bool]:
        return {name: True for name in (
            "meaning", "completeness", "precision", "nativeness",
            "locale_fit", "integrity", "orthography",
        )}
    def test_valid_swedish_passes_deterministic_gate(self) -> None:
        report = MODULE.validate_text(
            "Du väljer modell själv – eller låter BLUN automatiskt välja den modell som passar bäst för uppgiften.",
            "sv-SE",
        )
        self.assertEqual(report["status"], "PASS")

    def test_ascii_swedish_is_blocked(self) -> None:
        # Angel regression: real BLUN product copy, wholesale ASCII-folded,
        # deliberately avoiding the checker's tiny Swedish substitution list.
        report = MODULE.validate_text(
            ("BLUN samlar kraftfulla AI-modeller med kompletta arbetsytor for appar, "
             "webbplatser, mjukvaruutveckling, sprak, bilder och automatiserade floden. "
             "I stallet for att oppna ett nytt verktyg for varje steg samverkar modeller, "
             "agenter, API:er och MCP-servrar pa en gemensam europeisk plattform. ") * 2,
            "sv-SE",
        )
        self.assertEqual(report["status"], "BLOCK")
        self.assertIn("missing-language-character-profile", {finding["code"] for finding in report["findings"]})

    def test_long_page_release_completes_within_one_second(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        MODULE.KEY_PATH = Path(temporary.name) / "signing.key"
        target = ("BLUN samlar kraftfulla AI-modeller med kompletta arbetsytor för appar, "
                  "webbplatser, språk, bilder och automatiserade flöden. ") * 60
        started = time.perf_counter()
        report = MODULE.release_translation({
            "source_text": "BLUN combines powerful AI models and complete workspaces. " * 120,
            "target_text": target,
            "language": "sv-SE",
            "attestations": {name: True for name in (
                "meaning", "completeness", "precision", "nativeness",
                "locale_fit", "integrity", "orthography",
            )},
        })
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertTrue(report["release_allowed"])

    def test_mcp_accepts_utf8_bom(self) -> None:
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        result = subprocess.run(
            [sys.executable, str(SERVER), "serve"], input="\ufeff" + request,
            text=True, capture_output=True, check=False, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release_translation", result.stdout)

    def test_mcp_starts_from_isolated_installed_scripts_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installed_scripts = Path(directory) / "scripts"
            shutil.copytree(SERVER.parent, installed_scripts)
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
            result = subprocess.run(
                [sys.executable, str(installed_scripts / "blun_language_guard.py"), "serve"],
                input=request,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release_translation", result.stdout)

    def test_non_nfc_text_is_blocked(self) -> None:
        report = MODULE.validate_text("Cafe\u0301", "fr-FR")
        self.assertEqual(report["status"], "BLOCK")
        self.assertEqual(report["findings"][0]["code"], "unicode-not-nfc")

    def test_short_swedish_title_requires_native_review(self) -> None:
        report = MODULE.validate_text("Bygg appar snabbare", "sv-SE", content_type="title")
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertFalse(report["release_allowed"])

    def test_real_ascii_folded_blun_seo_copy_never_auto_passes(self) -> None:
        for candidate, content_type in (
            ("BLUN Imagine – bildgenerering och bildbearbetning", "title"),
            ("Skapa bilder, redigera material och bygg visuella arbetsfloden med BLUN Imagine.", "meta_description"),
        ):
            with self.subTest(candidate=candidate):
                report = MODULE.validate_text(candidate, "sv-SE", content_type=content_type)
                self.assertEqual(report["status"], "REVIEW_REQUIRED")
                self.assertFalse(report["release_allowed"])

    def test_short_title_with_native_character_still_requires_review(self) -> None:
        report = MODULE.validate_text("Bygg bättre appar", "sv-SE", content_type="title")
        self.assertEqual(report["status"], "REVIEW_REQUIRED")

    def test_self_attestation_cannot_bypass_ascii_folding_pressure(self) -> None:
        damaged = (
            "Haendler pruefen taeglich die Qualitaet im Buero. "
            "Jeder Kaeufer erhaelt Zugang zum Gebaeude."
        )
        cases = (
            {"content_type": "title", "short_text_reviewed": False},
            {"content_type": "title", "short_text_reviewed": True},
            {"content_type": "prose"},
        )
        for policy in cases:
            with self.subTest(policy=policy):
                report = MODULE.release_translation({
                    "source_text": "Dealers check quality every day.",
                    "target_text": damaged,
                    "language": "de-DE",
                    "attestations": self._attestations(),
                    **policy,
                })
                self.assertEqual(report["status"], "BLOCK")
                self.assertFalse(report["release_allowed"])
                self.assertIn("ascii-folding-pressure", {finding["code"] for finding in report["findings"]})

    def test_clean_reviewed_german_control_can_pass(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        MODULE.KEY_PATH = Path(temporary.name) / "signing.key"
        report = MODULE.release_translation({
            "source_text": "Dealers check quality every day.",
            "target_text": (
                "Händler prüfen täglich die Qualität im Büro. "
                "Jeder Käufer erhält Zugang zum Gebäude."
            ),
            "language": "de-DE",
            "content_type": "title",
            "short_text_reviewed": True,
            "attestations": self._attestations(),
        })
        self.assertTrue(report["release_allowed"], report)

    def test_correct_german_double_s_never_counts_as_ascii_folding(self) -> None:
        correct_ui_copy = (
            "Ihre Adresse wurde gespeichert.",
            "Das Passwort muss mindestens acht Zeichen haben.",
            "Sie müssen angemeldet sein, um fortzufahren.",
            "Der Prozess läuft, bitte schließen Sie das Fenster nicht.",
            "Wir wissen, dass Sie interessiert sind.",
            "Grösse und Gewicht eingeben",
            "Passwort zurücksetzen",
            "Datei erfolgreich hochgeladen",
        )
        for candidate in correct_ui_copy:
            with self.subTest(candidate=candidate):
                report = MODULE.validate_text(candidate, "de-DE", content_type="prose")
                codes = {finding["code"] for finding in report["findings"]}
                self.assertNotIn("ascii-folding-pressure", codes)

    def test_german_attack_still_blocks_without_double_s_counter(self) -> None:
        report = MODULE.validate_text(
            "Haendler pruefen taeglich die Qualitaet im Buero. Jeder Kaeufer erhaelt Zugang zum Gebaeude.",
            "de-DE",
            content_type="prose",
        )
        self.assertIn("ascii-folding-pressure", {finding["code"] for finding in report["findings"]})

    def test_independently_reviewed_short_title_can_pass(self) -> None:
        report = MODULE.validate_text(
            "Bygg appar snabbare", "sv-SE", content_type="title", short_text_reviewed=True,
        )
        self.assertEqual(report["status"], "PASS")

    def test_release_requires_every_attestation(self) -> None:
        report = MODULE.release_translation(
            {
                "source_text": "Hello",
                "target_text": "Hej",
                "language": "sv-SE",
                "attestations": {"meaning": True},
            }
        )
        self.assertFalse(report["release_allowed"])
        self.assertNotIn("release_token", report)

    def test_release_blocks_major_omission_despite_all_attestations(self) -> None:
        source = (
            "BLUN verbindet leistungsstarke KI-Modelle mit vollständigen Arbeitsbereichen für Apps, "
            "Websites, Softwareentwicklung, Sprachen, Bilder und automatisierte Abläufe. Statt für "
            "jeden Arbeitsschritt ein neues Werkzeug zu öffnen, arbeiten alle Komponenten zusammen."
        )
        report = MODULE.release_translation({
            "source_text": source,
            "target_text": source[:64],
            "language": "de-DE",
            "attestations": self._attestations(),
        })
        self.assertFalse(report["release_allowed"])
        self.assertIn(
            "translation-volume-integrity",
            {finding["code"] for finding in report["findings"]},
        )

    def test_release_auto_detects_html_and_ignores_preserved_script_bulk(self) -> None:
        source_copy = (
            "Vollständiger Inhalt für eine wichtige Produktseite mit Funktionen, Vorteilen und "
            "klaren nächsten Schritten für interessierte Kundinnen und Kunden."
        )
        script = "window.catalog = '" + ("technical payload " * 80) + "';"
        source = f"<main><p>{source_copy}</p><script>{script}</script></main>"
        target = f"<main><p>Kurz.</p><script>{script}</script></main>"
        report = MODULE.release_translation({
            "source_text": source,
            "target_text": target,
            "language": "de-DE",
            "attestations": self._attestations(),
        })
        self.assertFalse(report["release_allowed"])
        self.assertIn(
            "translation-volume-integrity",
            {finding["code"] for finding in report["findings"]},
        )

    def test_release_token_is_issued_after_all_gates_pass(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        MODULE.KEY_PATH = Path(temporary.name) / "signing.key"
        report = MODULE.release_translation(
            {
                "source_text": "If you have already paid, no further action is required.",
                "target_text": "Om du redan har betalat behöver du inte göra något mer.",
                "language": "sv-SE",
                "attestations": {
                    "meaning": True,
                    "completeness": True,
                    "precision": True,
                    "nativeness": True,
                    "locale_fit": True,
                    "integrity": True,
                    "orthography": True,
                },
            }
        )
        self.assertTrue(report["release_allowed"])
        self.assertRegex(report["release_token"], r"^blg5\.")

    def test_mcp_lists_mandatory_release_tool(self) -> None:
        response = MODULE.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {"validate_text", "release_translation", "verify_release_token"})


if __name__ == "__main__":
    unittest.main()
