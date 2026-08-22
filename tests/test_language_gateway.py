from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "integrations" / "language_gateway.py"
PRE_OUTPUT = ROOT / "integrations" / "pre_output_guard.py"
INSTALLED_PRE_OUTPUT = ROOT / "translate-native" / "scripts" / "pre_output_guard.py"
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

    def test_ambiguous_task_kind_blocks(self) -> None:
        result = GATEWAY.gate({"task_kind": "other", "target_text": "Hej", "language": "sv-SE"})
        self.assertFalse(result["release_allowed"])
        self.assertEqual(result["reason"], "invalid-task-kind")

    def test_translation_cannot_claim_response_mode(self) -> None:
        result = GATEWAY.gate({
            "task_kind": "response",
            "source_text": "Hello",
            "target_text": "Hej",
            "language": "sv-SE",
            "attestations": {"nativeness": True, "orthography": True},
        })
        self.assertFalse(result["release_allowed"])
        self.assertEqual(result["reason"], "response-cannot-carry-source")

    def test_unattested_translation_blocks(self) -> None:
        result = GATEWAY.gate({
            "task_kind": "translation", "source_text": "Hello",
            "target_text": "Hej", "language": "sv-SE",
        })
        self.assertFalse(result["release_allowed"])

    def test_translation_requires_exact_host_language(self) -> None:
        result = GATEWAY.gate({
            "task_kind": "translation",
            "source_text": "Hello",
            "target_text": "Hej",
            "language": "auto",
            "attestations": {name: True for name in (
                "meaning", "completeness", "precision", "nativeness",
                "locale_fit", "integrity", "orthography",
            )},
        })
        self.assertFalse(result["release_allowed"])
        self.assertIn("exact-language-required", {item["code"] for item in result["findings"]})

    def test_clean_agent_response_is_released(self) -> None:
        result = GATEWAY.gate({
            "task_kind": "response",
            "target_text": "Natürlich können wir das zuverlässig prüfen.",
            "language": "de-DE",
            "attestations": {"nativeness": True, "orthography": True},
        })
        self.assertTrue(result["release_allowed"], result)
        self.assertEqual(result["task_kind"], "response")

    def test_damaged_agent_response_is_blocked(self) -> None:
        result = GATEWAY.gate({
            "task_kind": "response",
            "target_text": "Haendler pruefen taeglich die Qualitaet im Buero.",
            "language": "de-DE",
            "attestations": {"nativeness": True, "orthography": True},
        })
        self.assertFalse(result["release_allowed"])
        self.assertIn("ascii-folding-pressure", {item["code"] for item in result["findings"]})

    def test_pre_output_hook_accepts_exact_response_and_rejects_edits(self) -> None:
        target = "Natürlich können wir das zuverlässig prüfen."
        released = GATEWAY.gate({
            "task_kind": "response",
            "target_text": target,
            "language": "de-DE",
            "attestations": {"nativeness": True, "orthography": True},
        })
        self.assertTrue(released["release_allowed"], released)
        base_request = {
            "task_kind": "response",
            "target_text": target,
            "language": "de-DE",
            "release_token": released["release_token"],
        }
        environment = dict(os.environ)
        environment["BLUN_LANGUAGE_GUARD_KEY_FILE"] = str(GATEWAY.GUARD.KEY_PATH)
        accepted = subprocess.run(
            [sys.executable, str(PRE_OUTPUT)],
            input="\ufeff" + json.dumps(base_request, ensure_ascii=False),
            text=True, capture_output=True, check=False, env=environment,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        edited = dict(base_request, target_text=target + " Wirklich.")
        rejected = subprocess.run(
            [sys.executable, str(PRE_OUTPUT)],
            input=json.dumps(edited, ensure_ascii=False),
            text=True, capture_output=True, check=False, env=environment,
        )
        self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)

    def test_pre_output_hooks_never_create_a_missing_verifier_key(self) -> None:
        request = {
            "task_kind": "response",
            "target_text": "Natürlich bleibt die Prüfung geschlossen.",
            "language": "de-DE",
            "release_token": "blg6.invalid",
        }
        for hook in (PRE_OUTPUT, INSTALLED_PRE_OUTPUT):
            with self.subTest(hook=hook):
                missing = Path(self.temporary.name) / hook.parent.name / "missing.key"
                environment = dict(os.environ)
                environment.pop("BLUN_LANGUAGE_GUARD_KEY", None)
                environment["BLUN_LANGUAGE_GUARD_KEY_FILE"] = str(missing)
                result = subprocess.run(
                    [sys.executable, str(hook)],
                    input=json.dumps(request, ensure_ascii=False),
                    text=True, capture_output=True, check=False, env=environment,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(missing.exists())
                self.assertIn("verification fails closed", result.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not authoritative on Windows")
    def test_pre_output_hooks_reject_a_broadly_readable_verifier_key(self) -> None:
        request = {
            "task_kind": "response",
            "target_text": "Natürlich bleibt die Prüfung geschlossen.",
            "language": "de-DE",
            "release_token": "blg6.invalid",
        }
        for hook in (PRE_OUTPUT, INSTALLED_PRE_OUTPUT):
            with self.subTest(hook=hook):
                key = Path(self.temporary.name) / hook.parent.name / "broad.key"
                key.parent.mkdir(parents=True, exist_ok=True)
                key.write_bytes(b"k" * 32)
                key.chmod(0o644)
                environment = dict(os.environ)
                environment.pop("BLUN_LANGUAGE_GUARD_KEY", None)
                environment["BLUN_LANGUAGE_GUARD_KEY_FILE"] = str(key)
                result = subprocess.run(
                    [sys.executable, str(hook)],
                    input=json.dumps(request, ensure_ascii=False),
                    text=True, capture_output=True, check=False, env=environment,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("broader than owner-only", result.stdout)

    def test_pre_output_hooks_reject_a_short_verifier_key(self) -> None:
        request = {
            "task_kind": "response",
            "target_text": "Natürlich bleibt die Prüfung geschlossen.",
            "language": "de-DE",
            "release_token": "blg6.invalid",
        }
        for hook in (PRE_OUTPUT, INSTALLED_PRE_OUTPUT):
            with self.subTest(hook=hook):
                key = Path(self.temporary.name) / hook.parent.name / "short.key"
                key.parent.mkdir(parents=True, exist_ok=True)
                key.write_bytes(b"too-short")
                key.chmod(0o600)
                environment = dict(os.environ)
                environment.pop("BLUN_LANGUAGE_GUARD_KEY", None)
                environment["BLUN_LANGUAGE_GUARD_KEY_FILE"] = str(key)
                result = subprocess.run(
                    [sys.executable, str(hook)],
                    input=json.dumps(request, ensure_ascii=False),
                    text=True, capture_output=True, check=False, env=environment,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("signing key is invalid", result.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_pre_output_hooks_reject_a_linked_verifier_key(self) -> None:
        request = {
            "task_kind": "response",
            "target_text": "Natürlich bleibt die Prüfung geschlossen.",
            "language": "de-DE",
            "release_token": "blg6.invalid",
        }
        for hook in (PRE_OUTPUT, INSTALLED_PRE_OUTPUT):
            with self.subTest(hook=hook):
                root = Path(self.temporary.name) / hook.parent.name
                root.mkdir(parents=True, exist_ok=True)
                target = root / "known.key"
                target.write_bytes(b"k" * 32)
                target.chmod(0o600)
                linked = root / "linked.key"
                linked.symlink_to(target)
                environment = dict(os.environ)
                environment.pop("BLUN_LANGUAGE_GUARD_KEY", None)
                environment["BLUN_LANGUAGE_GUARD_KEY_FILE"] = str(linked)
                result = subprocess.run(
                    [sys.executable, str(hook)],
                    input=json.dumps(request, ensure_ascii=False),
                    text=True, capture_output=True, check=False, env=environment,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("signing key is invalid", result.stdout)

    def test_fully_attested_swedish_translation_is_released(self) -> None:
        result = GATEWAY.gate({
            "task_kind": "translation",
            "source_text": "If you have already paid, no further action is required.",
            "target_text": "Om du redan har betalat behöver du inte göra något mer.",
            "language": "sv-SE",
            "attestations": {name: True for name in (
                "meaning", "completeness", "precision", "nativeness",
                "locale_fit", "integrity", "orthography",
            )},
        })
        self.assertTrue(result["release_allowed"])
        self.assertTrue(result["release_token"].startswith("blg6."))
        self.assertEqual(result["gateway"], f"blun-language-gateway/{GATEWAY.GUARD.VERSION}")


if __name__ == "__main__":
    unittest.main()
