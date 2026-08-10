from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import json
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

    def test_non_nfc_text_is_blocked(self) -> None:
        report = MODULE.validate_text("Cafe\u0301", "fr-FR")
        self.assertEqual(report["status"], "BLOCK")
        self.assertEqual(report["findings"][0]["code"], "unicode-not-nfc")

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
