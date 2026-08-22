from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "integrations" / "enforced_delivery.py"
SPEC = importlib.util.spec_from_file_location("blun_test_delivery", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EnforcedDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.key_path = Path(self.temporary.name) / "signing.key"
        self.key = MODULE.QUALITY.load_or_create_key(self.key_path)

    def envelope(self, target: str, *, source: str = "", language: str = "de-DE", purpose: str = "response") -> str:
        token = MODULE.QUALITY.issue_receipt(
            source, target, language, self.key, purpose=purpose,
        )
        return json.dumps({"target_text": target, "release_token": token}, ensure_ascii=False)

    def run_delivery(self, raw: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PATH), "--key-file", str(self.key_path), *arguments],
            input=raw,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_signed_response_is_the_only_stdout(self) -> None:
        target = "Natürlich ist das möglich."
        result = self.run_delivery(
            self.envelope(target), "--task-kind", "response", "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, target)

    def test_raw_or_unsigned_output_is_blocked_without_leaking_candidate(self) -> None:
        target = "Das waere falsch."
        result = self.run_delivery(
            target, "--task-kind", "response", "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(target, result.stderr)

    def test_agent_cannot_override_host_task_or_language(self) -> None:
        target = "Hej världen."
        envelope = json.loads(self.envelope(target, language="sv-SE"))
        envelope.update({"task_kind": "response", "language": "sv-SE"})
        result = self.run_delivery(
            json.dumps(envelope, ensure_ascii=False),
            "--task-kind", "response", "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("host-owned fields", result.stderr)

    def test_response_receipt_cannot_release_translation(self) -> None:
        source = "This is the source."
        target = "Das ist der Ausgangstext."
        source_path = Path(self.temporary.name) / "source.txt"
        source_path.write_text(source, encoding="utf-8")
        result = self.run_delivery(
            self.envelope(target, source=source, purpose="response"),
            "--task-kind", "translation", "--language", "de-DE",
            "--source-file", str(source_path),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_translation_requires_complete_trusted_source_and_translation_receipt(self) -> None:
        source = "This is the complete source."
        target = "Dies ist der vollständige Ausgangstext."
        source_path = Path(self.temporary.name) / "source.txt"
        source_path.write_text("\ufeff" + source, encoding="utf-8")
        result = self.run_delivery(
            self.envelope(target, source=source, purpose="translation"),
            "--task-kind", "translation", "--language", "de-DE",
            "--source-file", str(source_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, target)

    def test_edit_after_release_is_blocked(self) -> None:
        signed = "Natürlich ist das möglich."
        envelope = json.loads(self.envelope(signed))
        envelope["target_text"] = signed + " Wirklich."
        result = self.run_delivery(
            json.dumps(envelope, ensure_ascii=False),
            "--task-kind", "response", "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("target", result.stderr)

    def test_child_process_does_not_receive_signing_environment(self) -> None:
        helper = Path(self.temporary.name) / "helper.py"
        helper.write_text(
            "import json, os\n"
            "print(json.dumps({'target_text': os.getenv('BLUN_LANGUAGE_GUARD_KEY', 'hidden'), 'release_token': 'none'}))\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["BLUN_LANGUAGE_GUARD_KEY"] = "secret-value"
        environment["BLUN_LANGUAGE_GUARD_SERVICE_TOKEN"] = "service-secret-value"
        result = subprocess.run(
            [
                sys.executable, str(PATH), "--key-file", str(self.key_path),
                "--task-kind", "response", "--language", "de-DE", "--",
                sys.executable, str(helper),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("secret-value", result.stdout + result.stderr)

    def test_isolated_service_verifies_without_exposing_local_key(self) -> None:
        target = "Natürlich ist das möglich."
        envelope = json.loads(self.envelope(target))
        policy = MODULE.HostPolicy("response", "de-DE")
        with mock.patch.object(
            MODULE.SERVICE_CLIENT,
            "call_guard_service",
            return_value={"valid": True, "checks": {"target": True}},
        ) as service_call:
            result = MODULE.verify_envelope_with_service(
                envelope,
                policy,
                "unix:/guard.sock",
                service_token="service-token-with-at-least-32-characters",
            )
        self.assertEqual(result, target)
        request = service_call.call_args.args[1]
        self.assertEqual(request["task_kind"], "response")
        self.assertEqual(request["target_text"], target)

    def test_require_service_blocks_local_key_fallback(self) -> None:
        target = "Natürlich ist das möglich."
        result = self.run_delivery(
            self.envelope(target),
            "--task-kind", "response", "--language", "de-DE", "--require-service",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("isolated guard service is required", result.stderr)

    def test_installed_policy_requires_isolated_service(self) -> None:
        policy_path = Path(self.temporary.name) / "delivery-policy.json"
        policy_path.write_text(json.dumps({
            "mandatory": True,
            "isolated_service": {
                "required": True,
                "endpoint": "tcp:127.0.0.1:47631",
                "token_file": str(Path(self.temporary.name) / "service.token"),
            },
        }), encoding="utf-8")
        policy_path.chmod(0o600)
        isolated = MODULE.load_installed_service_policy(policy_path)
        self.assertTrue(isolated["required"])
        self.assertEqual(isolated["endpoint"], "tcp:127.0.0.1:47631")

    def test_invalid_installed_policy_blocks(self) -> None:
        policy_path = Path(self.temporary.name) / "delivery-policy.json"
        policy_path.write_text("{}", encoding="utf-8")
        policy_path.chmod(0o600)
        with self.assertRaises(MODULE.DeliveryBlocked):
            MODULE.load_installed_service_policy(policy_path)

    @unittest.skipIf(os.name == "nt", "POSIX policy security test")
    def test_installed_policy_rejects_links_and_broad_permissions(self) -> None:
        root = Path(self.temporary.name)
        policy_path = root / "delivery-policy.json"
        policy_path.write_text(json.dumps({
            "mandatory": True,
            "isolated_service": {
                "required": True,
                "endpoint": "tcp:127.0.0.1:47631",
                "token_file": str(root / "service.token"),
            },
        }), encoding="utf-8")
        policy_path.chmod(0o600)
        linked = root / "linked-policy.json"
        linked.symlink_to(policy_path)
        with self.assertRaisesRegex(MODULE.DeliveryBlocked, "regular file"):
            MODULE.load_installed_service_policy(linked)
        policy_path.chmod(0o644)
        with self.assertRaisesRegex(MODULE.DeliveryBlocked, "owner-only"):
            MODULE.load_installed_service_policy(policy_path)

    def test_installed_policy_rejects_oversized_and_unsafe_fields(self) -> None:
        root = Path(self.temporary.name)
        policy_path = root / "delivery-policy.json"
        policy_path.write_bytes(b"{" + b" " * MODULE.MAX_POLICY_BYTES + b"}")
        policy_path.chmod(0o600)
        with self.assertRaisesRegex(MODULE.DeliveryBlocked, "size limit"):
            MODULE.load_installed_service_policy(policy_path)
        policy_path.write_text(json.dumps({
            "mandatory": True,
            "direct_delivery_allowed": True,
            "isolated_service": {
                "required": True,
                "endpoint": "tcp:127.0.0.1:47631",
                "token_file": str(root / "service.token"),
            },
        }), encoding="utf-8")
        policy_path.chmod(0o600)
        with self.assertRaisesRegex(MODULE.DeliveryBlocked, "invalid"):
            MODULE.load_installed_service_policy(policy_path)

    def test_installed_policy_rejects_path_exchange_during_read(self) -> None:
        root = Path(self.temporary.name)
        policy_path = root / "delivery-policy.json"
        policy_path.write_text(json.dumps({
            "mandatory": True,
            "isolated_service": {
                "required": True,
                "endpoint": "tcp:127.0.0.1:47631",
                "token_file": str(root / "service.token"),
            },
        }), encoding="utf-8")
        policy_path.chmod(0o600)
        before = policy_path.lstat()
        exchanged = mock.Mock(
            st_dev=before.st_dev,
            st_ino=before.st_ino,
            st_size=before.st_size,
            st_ctime_ns=before.st_ctime_ns,
            st_mtime_ns=before.st_mtime_ns + 1,
        )
        with mock.patch.object(Path, "lstat", side_effect=[before, exchanged]):
            with self.assertRaisesRegex(MODULE.DeliveryBlocked, "changed while reading"):
                MODULE.load_installed_service_policy(policy_path)

    def test_installed_service_policy_prevents_local_key_fallback(self) -> None:
        token_path = Path(self.temporary.name) / "service.token"
        token_path.write_text("service-token-with-at-least-32-characters\n", encoding="ascii")
        policy_path = Path(self.temporary.name) / "delivery-policy.json"
        policy_path.write_text(json.dumps({
            "mandatory": True,
            "isolated_service": {
                "required": True,
                "endpoint": "tcp:127.0.0.1:1",
                "token_file": str(token_path),
            },
        }), encoding="utf-8")
        policy_path.chmod(0o600)
        target = "Natürlich ist das möglich."
        result = self.run_delivery(
            self.envelope(target),
            "--policy-file", str(policy_path),
            "--task-kind", "response",
            "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(target, result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_linked_service_token_fails_closed_before_delivery(self) -> None:
        root = Path(self.temporary.name)
        token_path = root / "service.token"
        token_path.write_text("s" * 64 + "\n", encoding="ascii")
        token_path.chmod(0o600)
        linked = root / "linked-service.token"
        linked.symlink_to(token_path)
        policy_path = root / "delivery-policy.json"
        policy_path.write_text(json.dumps({
            "mandatory": True,
            "isolated_service": {
                "required": True,
                "endpoint": "tcp:127.0.0.1:1",
                "token_file": str(linked),
            },
        }), encoding="utf-8")
        policy_path.chmod(0o600)
        target = "Natürlich bleibt die Zustellung geschlossen."
        result = self.run_delivery(
            self.envelope(target),
            "--policy-file", str(policy_path),
            "--task-kind", "response",
            "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("regular file", result.stderr)
        self.assertNotIn(target, result.stderr)

    def test_missing_key_fails_closed_instead_of_creating_one(self) -> None:
        missing = Path(self.temporary.name) / "missing.key"
        result = subprocess.run(
            [
                sys.executable, str(PATH), "--key-file", str(missing),
                "--task-kind", "response", "--language", "de-DE",
            ],
            input=self.envelope("Natürlich ist das möglich."),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertFalse(missing.exists())
        self.assertEqual(result.stdout, "")

    @unittest.skipIf(os.name == "nt", "POSIX permission test")
    def test_insecure_key_permissions_fail_closed(self) -> None:
        os.chmod(self.key_path, 0o644)
        result = self.run_delivery(
            self.envelope("Natürlich ist das möglich."),
            "--task-kind", "response", "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("permissions", result.stderr)
        self.assertEqual(result.stdout, "")

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_linked_key_fails_closed(self) -> None:
        linked = Path(self.temporary.name) / "linked.key"
        linked.symlink_to(self.key_path)
        result = self.run_delivery(
            self.envelope("Natürlich ist das möglich."),
            "--key-file", str(linked),
            "--task-kind", "response", "--language", "de-DE",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("signing key is invalid", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_sync_sender_is_never_called_for_invalid_receipt(self) -> None:
        calls: list[str] = []
        policy = MODULE.HostPolicy("response", "de-DE")
        envelope = json.loads(self.envelope("Natürlich ist das möglich."))
        envelope["target_text"] = "Manipulierter Text"
        with self.assertRaises(MODULE.DeliveryBlocked):
            MODULE.guarded_send(json.dumps(envelope), policy, self.key, calls.append)
        self.assertEqual(calls, [])

    def test_async_sender_receives_only_verified_text(self) -> None:
        calls: list[str] = []
        target = "Hej världen."

        async def sender(text: str) -> str:
            calls.append(text)
            return "sent"

        result = asyncio.run(MODULE.guarded_send_async(
            self.envelope(target, language="sv-SE"),
            MODULE.HostPolicy("response", "sv-SE"),
            self.key,
            sender,
        ))
        self.assertEqual(result, "sent")
        self.assertEqual(calls, [target])


if __name__ == "__main__":
    unittest.main()
