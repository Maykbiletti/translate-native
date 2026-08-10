from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "integrations" / "language_gateway.py"
SPEC = importlib.util.spec_from_file_location("blun_test_gateway", PATH)
assert SPEC and SPEC.loader
GATEWAY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATEWAY
SPEC.loader.exec_module(GATEWAY)


class LanguageGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        GATEWAY.GUARD.KEY_PATH = Path(self.temporary.name) / "signing.key"

    def test_missing_contract_blocks(self) -> None:
        self.assertFalse(GATEWAY.gate({"target_text": "Hej"})["release_allowed"])

    def test_unattested_translation_blocks(self) -> None:
        result = GATEWAY.gate({"source_text": "Hello", "target_text": "Hej", "language": "sv-SE"})
        self.assertFalse(result["release_allowed"])

    def test_fully_attested_swedish_translation_is_released(self) -> None:
        result = GATEWAY.gate({
            "source_text": "If you have already paid, no further action is required.",
            "target_text": "Om du redan har betalat behöver du inte göra något mer.",
            "language": "sv-SE",
            "attestations": {name: True for name in (
                "meaning", "completeness", "precision", "nativeness",
                "locale_fit", "integrity", "orthography",
            )},
        })
        self.assertTrue(result["release_allowed"])
        self.assertTrue(result["release_token"].startswith("blg5."))
        self.assertEqual(result["gateway"], f"blun-language-gateway/{GATEWAY.GUARD.VERSION}")


if __name__ == "__main__":
    unittest.main()
