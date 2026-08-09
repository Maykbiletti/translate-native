from __future__ import annotations

import json
import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "translate-native" / "SKILL.md"
AGENTS = ROOT / "AGENTS.md"
TRANSLATIONESE_REVIEW = (
    ROOT / "translate-native" / "references" / "translationese-review.md"
)
NATIVE_ORTHOGRAPHY = (
    ROOT / "translate-native" / "references" / "native-orthography.md"
)
REGRESSIONS = ROOT / "evals" / "regressions.jsonl"


class SkillContractTests(unittest.TestCase):
    def test_skill_blocks_grammatical_translationese(self) -> None:
        content = SKILL.read_text(encoding="utf-8")
        required_contract = (
            "Reject grammatical translationese",
            "grammatical-but-unnatural wording",
            "generic AI filler",
            "translationese-review.md",
            "major nativeness defect blocks delivery",
        )
        for requirement in required_contract:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, content)

    def test_skill_combines_nativeness_and_orthography_without_dependency(self) -> None:
        content = SKILL.read_text(encoding="utf-8")
        required_contract = (
            "Run one combined release gate",
            "Do not rely on a second skill",
            "Native language",
            "Native orthography",
            "native-orthography.md",
            "scripts/check_diacritics.py",
            "never delegate the orthography requirement",
        )
        for requirement in required_contract:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, content)

    def test_repository_agents_must_load_the_combined_skill(self) -> None:
        content = AGENTS.read_text(encoding="utf-8")
        for requirement in (
            "translate-native/SKILL.md",
            "untrusted draft",
            "native-language and native-orthography gates",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, content)

    def test_swedish_agent_copy_is_a_documented_regression_case(self) -> None:
        content = TRANSLATIONESE_REVIEW.read_text(encoding="utf-8")
        candidate_markers = (
            "desktop- och mobilprogramvara",
            "fördela uppgiften på ett smart sätt",
            "fördela uppgiften till den modell som passar bäst",
        )
        native_rewrite_markers = (
            "programvara för datorer och mobila enheter",
            "fördela arbetet mellan de modeller som passar bäst för uppgiften",
            "automatiskt välja den modell som passar bäst för uppgiften",
        )
        for marker in candidate_markers + native_rewrite_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, content)

    def test_new_instruction_files_are_unicode_nfc(self) -> None:
        for path in (
            SKILL,
            AGENTS,
            TRANSLATIONESE_REVIEW,
            NATIVE_ORTHOGRAPHY,
            REGRESSIONS,
        ):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertTrue(unicodedata.is_normalized("NFC", content))

    def test_regression_corpus_spans_languages_and_scripts(self) -> None:
        cases = [
            json.loads(line)
            for line in REGRESSIONS.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertGreaterEqual(len(cases), 10)
        self.assertTrue(all(case["expected_status"] == "FAIL" for case in cases))
        self.assertTrue(all(case["defects"] for case in cases))
        self.assertTrue(all(case["reference_target"] for case in cases))
        languages = {case["language"] for case in cases}
        self.assertTrue(
            {"sv-SE", "de-DE", "es-ES", "cs-CZ", "ca-ES", "zh-CN", "uk-UA", "vi-VN", "ar"}.issubset(languages)
        )


if __name__ == "__main__":
    unittest.main()
