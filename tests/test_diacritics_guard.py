from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "translate-native" / "scripts" / "check_diacritics.py"


def run_checker(text: str, language: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.md"
        path.write_text(text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(CHECKER), "--language", language, str(path)],
            capture_output=True,
            check=False,
            text=True,
        )


class DiacriticsCheckerTests(unittest.TestCase):
    def test_correct_multilingual_text_passes(self) -> None:
        result = run_checker(
            "Schöne Grüße. Göteborg. Español. Čeština. São Paulo.", "all"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_german_transliterations_fail(self) -> None:
        result = run_checker("Schoene Gruesse und vollstaendig geprueft.", "de")
        self.assertEqual(result.returncode, 1)
        self.assertIn("schoen", result.stdout.casefold())
        self.assertIn("vollstaendig", result.stdout.casefold())

    def test_short_german_response_folding_fails(self) -> None:
        result = run_checker("Das waere richtig und zuverlaessig.", "de-DE")
        self.assertEqual(result.returncode, 1)
        self.assertIn("waere", result.stdout.casefold())

    def test_short_swedish_response_folding_fails(self) -> None:
        result = run_checker("Det ar bra for dig pa en vanlig dag.", "sv-SE")
        self.assertEqual(result.returncode, 1)
        self.assertIn("det ar", result.stdout.casefold())
        self.assertIn("for dig", result.stdout.casefold())

    def test_legitimate_swedish_flod_is_not_mistaken_for_flode(self) -> None:
        result = run_checker("Nilen är en flod.", "sv-SE")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_spanish_transliterations_fail(self) -> None:
        result = run_checker("Informacion para el senor en esta pagina.", "es")
        self.assertEqual(result.returncode, 1)
        self.assertIn("informacion", result.stdout.casefold())
        self.assertIn("pagina", result.stdout.casefold())

    def test_czech_transliterations_fail(self) -> None:
        result = run_checker("Cestina a Dvorak.", "cs")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cestina", result.stdout.casefold())
        self.assertIn("dvorak", result.stdout.casefold())

    def test_technical_spans_are_ignored(self) -> None:
        result = run_checker(
            "Der Bezeichner `schoen_fuer_api` und "
            "https://example.com/fuer-dich bleiben exakt.",
            "de",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_language_without_diacritics_rules_is_explained_and_passes(self) -> None:
        result = run_checker("Plain English text without special marks.", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no deterministic diacritics rules", result.stdout)
        self.assertIn("native-orthography review is still required", result.stdout)

    def test_unknown_bcp47_tag_still_runs_unicode_check(self) -> None:
        result = run_checker("Clean text.", "x-test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("x-test", result.stdout)


if __name__ == "__main__":
    unittest.main()
