from __future__ import annotations

import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "translate-native" / "SKILL.md"
TRANSLATIONESE_REVIEW = (
    ROOT / "translate-native" / "references" / "translationese-review.md"
)


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

    def test_swedish_agent_copy_is_a_documented_regression_case(self) -> None:
        content = TRANSLATIONESE_REVIEW.read_text(encoding="utf-8")
        candidate_markers = (
            "desktop- och mobilprogramvara",
            "fördela uppgiften på ett smart sätt",
        )
        native_rewrite_markers = (
            "programvara för datorer och mobila enheter",
            "fördela arbetet mellan de modeller som passar bäst för uppgiften",
        )
        for marker in candidate_markers + native_rewrite_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, content)

    def test_new_instruction_files_are_unicode_nfc(self) -> None:
        for path in (SKILL, TRANSLATIONESE_REVIEW):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertTrue(unicodedata.is_normalized("NFC", content))


if __name__ == "__main__":
    unittest.main()
