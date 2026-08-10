from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "translate-native" / "scripts" / "language_quality.py"
SPEC = importlib.util.spec_from_file_location("language_quality", PATH)
assert SPEC and SPEC.loader
QUALITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = QUALITY
SPEC.loader.exec_module(QUALITY)


class ReceiptTests(unittest.TestCase):
    KEY = b"k" * 32

    def test_receipt_is_bound_to_every_input(self) -> None:
        token = QUALITY.issue_receipt("Hello", "Hej", "sv-SE", self.KEY)
        self.assertTrue(QUALITY.verify_receipt(token, "Hello", "Hej", "sv-SE", self.KEY)["valid"])
        for source, target, language in (("Changed", "Hej", "sv-SE"), ("Hello", "Hallå", "sv-SE"), ("Hello", "Hej", "da-DK")):
            self.assertFalse(QUALITY.verify_receipt(token, source, target, language, self.KEY)["valid"])

    def test_forged_receipt_fails(self) -> None:
        token = QUALITY.issue_receipt("Hello", "Hej", "sv-SE", self.KEY)
        forged = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertFalse(QUALITY.verify_receipt(forged, "Hello", "Hej", "sv-SE", self.KEY)["valid"])

    def test_transport_normalization_ignores_bom_and_line_endings(self) -> None:
        token = QUALITY.issue_receipt("First\nSecond", "Första\nAndra", "sv-SE", self.KEY)
        self.assertTrue(QUALITY.verify_receipt(token, "\ufeffFirst\r\nSecond", "Första\r\nAndra", "sv-SE", self.KEY)["valid"])

    def test_mojibake_is_not_normalized_away(self) -> None:
        token = QUALITY.issue_receipt("Hello", "Förstå", "sv-SE", self.KEY)
        self.assertFalse(QUALITY.verify_receipt(token, "Hello", "FÃ¶rstÃ¥", "sv-SE", self.KEY)["valid"])

    def test_receipt_is_bound_to_short_text_review_policy(self) -> None:
        token = QUALITY.issue_receipt(
            "Build faster", "Bygg snabbare", "sv-SE", self.KEY,
            content_type="title", short_text_reviewed=True,
        )
        self.assertTrue(QUALITY.verify_receipt(
            token, "Build faster", "Bygg snabbare", "sv-SE", self.KEY,
            "title", True,
        )["valid"])
        self.assertFalse(QUALITY.verify_receipt(
            token, "Build faster", "Bygg snabbare", "sv-SE", self.KEY,
            "prose", False,
        )["valid"])


class LanguageSafetyTests(unittest.TestCase):
    def test_balanced_isolates_pass_but_overrides_and_unpaired_fail(self) -> None:
        self.assertEqual([], QUALITY.bidi_findings("مرحبًا \u2066https://example.com\u2069"))
        self.assertTrue(QUALITY.bidi_findings("safe\u202eevil"))
        self.assertTrue(QUALITY.bidi_findings("text\u2069"))

    def test_script_identity(self) -> None:
        self.assertEqual("pass", QUALITY.script_report("Привіт, Україно!", "uk-UA")["status"])
        self.assertEqual("fail", QUALITY.script_report("Pryvit Ukraino", "uk-UA")["status"])
        self.assertEqual("not-evaluated", QUALITY.script_report("Kaixo", "eu-ES")["status"])

    def test_glossary_exact_and_regex(self) -> None:
        glossary = {"workspace": "Arbeitsbereich", "invoice": {"target": r"Rechnung(?:en)?", "regex": True}}
        self.assertEqual([], QUALITY.glossary_findings("Arbeitsbereich mit Rechnungen", glossary))
        self.assertEqual(2, len(QUALITY.glossary_findings("Bereich", glossary)))
