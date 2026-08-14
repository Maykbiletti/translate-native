from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "integrations" / "guard_service.py"
CLIENT_PATH = ROOT / "translate-native" / "scripts" / "guard_service_client.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SERVICE = load("blun_test_guard_service", SERVICE_PATH)
CLIENT = load("blun_test_guard_client", CLIENT_PATH)


class GuardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.key_path = root / "signing.key"
        self.audit_path = root / "audit.jsonl"
        self.service = SERVICE.GuardService(self.key_path, self.audit_path, "service-secret-with-at-least-32-characters")
        self.session_epoch = "a" * 64
        registered = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "register_session_epoch",
            "session_id": "session-one",
            "session_epoch": self.session_epoch,
        })
        self.assertTrue(registered["registered"], registered)

    def release_request(self, target: str = "Natürlich ist das möglich.") -> dict:
        return {
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "release",
            "task_kind": "response",
            "target_text": target,
            "language": "de-DE",
            "agent_id": "test-agent",
            "channel": "test",
            "attestations": {"nativeness": True, "orthography": True},
        }

    def test_release_and_verify_use_service_owned_key(self) -> None:
        released = self.service.handle(self.release_request())
        self.assertTrue(released["release_allowed"], released)
        verified = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "verify",
            "task_kind": "response",
            "source_text": "",
            "target_text": "Natürlich ist das möglich.",
            "language": "de-DE",
            "release_token": released["release_token"],
        })
        self.assertTrue(verified["valid"], verified)

    def test_tamper_and_wrong_service_token_block(self) -> None:
        released = self.service.handle(self.release_request())
        verified = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "verify",
            "task_kind": "response",
            "source_text": "",
            "target_text": "Nachträglich verändert.",
            "language": "de-DE",
            "release_token": released["release_token"],
        })
        self.assertFalse(verified["valid"])
        with self.assertRaises(SERVICE.GuardProtocolError):
            self.service.handle({"operation": "health", "service_token": "wrong"})

    def test_health_runs_release_signature_and_tamper_self_test_without_audit(self) -> None:
        health = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "health",
        })
        self.assertEqual(health["status"], "ok", health)
        self.assertTrue(health["isolated_key"])
        self.assertEqual(health["self_test"], {
            "release": True,
            "signature": True,
            "tamper_blocked": True,
        })
        self.assertFalse(self.audit_path.exists())

    def test_health_blocks_when_release_signing_path_is_broken(self) -> None:
        with mock.patch.object(
            SERVICE.GATEWAY, "gate",
            return_value={"status": "PASS", "release_allowed": True, "release_token": "forged"},
        ):
            health = self.service.handle({
                "service_token": "service-secret-with-at-least-32-characters",
                "operation": "health",
            })
        self.assertEqual(health["status"], "BLOCK", health)
        self.assertFalse(health["isolated_key"])
        self.assertFalse(health["self_test"]["signature"])

    def authorize_request(self, release_token: str, target: str = "Natürlich ist das möglich.") -> dict:
        return {
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "authorize_delivery",
            "task_kind": "response",
            "source_text": "",
            "target_text": target,
            "language": "de-DE",
            "release_token": release_token,
            "session_id": "session-one",
            "session_epoch": self.session_epoch,
            "agent_id": "test-agent",
            "channel": "claude-hook",
        }

    def consume_request(self, delivery_grant: str, target: str = "Natürlich ist das möglich.") -> dict:
        return {
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "consume_delivery",
            "delivery_grant": delivery_grant,
            "source_sha256": SERVICE.QUALITY.canonical_hash(""),
            "target_text": target,
            "language": "de-DE",
            "task_kind": "response",
            "content_type": "prose",
            "short_text_reviewed": False,
            "session_id": "session-one",
            "session_epoch": self.session_epoch,
            "agent_id": "test-agent",
            "channel": "claude-hook",
        }

    def test_delivery_grant_is_exact_one_time_and_service_boot_bound(self) -> None:
        released = self.service.handle(self.release_request())
        def fresh_grant() -> str:
            authorized = self.service.handle(self.authorize_request(released["release_token"]))
            self.assertTrue(authorized["valid"], authorized)
            return authorized["delivery_grant"]

        grant = fresh_grant()

        consumed = self.service.handle(self.consume_request(grant))
        self.assertTrue(consumed["valid"], consumed)
        replayed = self.service.handle(self.consume_request(grant))
        self.assertFalse(replayed["valid"], replayed)
        self.assertFalse(replayed["checks"]["one_time"])

        wrong_target_grant = fresh_grant()
        wrong_target = self.service.handle(self.consume_request(wrong_target_grant, "Geändert."))
        self.assertFalse(wrong_target["valid"], wrong_target)
        burned_after_edit = self.service.handle(self.consume_request(wrong_target_grant))
        self.assertFalse(burned_after_edit["valid"], burned_after_edit)
        self.assertFalse(burned_after_edit["checks"]["one_time"])

        session_grant = fresh_grant()
        wrong_session_request = self.consume_request(session_grant)
        wrong_session_request["session_id"] = "another-session"
        wrong_session = self.service.handle(wrong_session_request)
        self.assertFalse(wrong_session["valid"], wrong_session)
        self.assertFalse(wrong_session["checks"]["session"])

        session_epoch_grant = fresh_grant()
        wrong_epoch_request = self.consume_request(session_epoch_grant)
        wrong_epoch_request["session_epoch"] = "b" * 64
        wrong_epoch = self.service.handle(wrong_epoch_request)
        self.assertFalse(wrong_epoch["valid"], wrong_epoch)
        self.assertFalse(wrong_epoch["checks"]["session_epoch"])

        agent_grant = fresh_grant()
        wrong_agent_request = self.consume_request(agent_grant)
        wrong_agent_request["agent_id"] = "another-agent"
        wrong_agent = self.service.handle(wrong_agent_request)
        self.assertFalse(wrong_agent["valid"], wrong_agent)
        self.assertFalse(wrong_agent["checks"]["agent"])

        original_version = SERVICE.QUALITY.VERSION
        version_grant = fresh_grant()
        try:
            SERVICE.QUALITY.VERSION = "future-version"
            wrong_version = self.service.handle(self.consume_request(version_grant))
        finally:
            SERVICE.QUALITY.VERSION = original_version
        self.assertFalse(wrong_version["valid"], wrong_version)
        self.assertFalse(wrong_version["checks"]["version"])

        signed_grant = fresh_grant()
        forged = signed_grant[:-1] + ("A" if signed_grant[-1] != "A" else "B")
        forged_result = self.service.handle(self.consume_request(forged))
        self.assertFalse(forged_result["valid"], forged_result)

        restarted = SERVICE.GuardService(
            self.key_path, self.audit_path, "service-secret-with-at-least-32-characters"
        )
        restart_grant = fresh_grant()
        restarted_result = restarted.handle(self.consume_request(restart_grant))
        self.assertFalse(restarted_result["valid"], restarted_result)
        self.assertFalse(restarted_result["checks"]["service_boot"])

        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(grant, raw)

    def test_service_epoch_rotation_blocks_restored_local_state(self) -> None:
        released = self.service.handle(self.release_request())
        old_authorized = self.service.handle(self.authorize_request(released["release_token"]))
        self.assertTrue(old_authorized["valid"], old_authorized)
        old_grant = old_authorized["delivery_grant"]

        new_epoch = "b" * 64
        rotated = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "register_session_epoch",
            "session_id": "session-one",
            "session_epoch": new_epoch,
        })
        self.assertTrue(rotated["registered"], rotated)

        restored = self.service.handle(self.consume_request(old_grant))
        self.assertFalse(restored["valid"], restored)
        self.assertTrue(restored["checks"]["session_epoch"])
        self.assertFalse(restored["checks"]["session_epoch_current"])

        stale_authorization = self.service.handle(self.authorize_request(released["release_token"]))
        self.assertFalse(stale_authorization["valid"], stale_authorization)
        self.assertFalse(stale_authorization["checks"]["session_epoch_current"])

        replayed_registration = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "register_session_epoch",
            "session_id": "session-one",
            "session_epoch": self.session_epoch,
        })
        self.assertFalse(replayed_registration["registered"], replayed_registration)

        new_authorization_request = self.authorize_request(released["release_token"])
        new_authorization_request["session_epoch"] = new_epoch
        new_authorized = self.service.handle(new_authorization_request)
        self.assertTrue(new_authorized["valid"], new_authorized)
        new_consume_request = self.consume_request(new_authorized["delivery_grant"])
        new_consume_request["session_epoch"] = new_epoch
        self.assertTrue(self.service.handle(new_consume_request)["valid"])

        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(self.session_epoch, raw)
        self.assertNotIn(new_epoch, raw)

    def test_delivery_grant_binds_source_language_purpose_and_policy(self) -> None:
        source = "Die Größe des Gebäudes wird täglich geprüft."
        target = "Byggnadens storlek kontrolleras dagligen."
        released = self.service.handle({
            **self.release_request(target),
            "task_kind": "translation",
            "source_text": source,
            "language": "sv-SE",
            "attestations": {
                "meaning": True,
                "completeness": True,
                "precision": True,
                "nativeness": True,
                "locale_fit": True,
                "orthography": True,
                "integrity": True,
            },
        })
        self.assertTrue(released["release_allowed"], released)

        def fresh_grant() -> str:
            authorized = self.service.handle({
                **self.authorize_request(released["release_token"], target),
                "task_kind": "translation",
                "source_text": source,
                "language": "sv-SE",
            })
            self.assertTrue(authorized["valid"], authorized)
            grant = authorized["delivery_grant"]
            self.assertTrue(grant.startswith("blgd2."))
            payload = json.loads(SERVICE.QUALITY._b64decode(grant.split(".")[1]))
            self.assertEqual(payload["source_sha256"], SERVICE.QUALITY.canonical_hash(source))
            self.assertEqual(
                payload["session_epoch_sha256"],
                SERVICE.GuardService._identity_hash(self.session_epoch),
            )
            self.assertNotIn(source, grant)
            self.assertNotIn(self.session_epoch, grant)
            return grant

        base = {
            **self.consume_request(fresh_grant(), target),
            "source_sha256": SERVICE.QUALITY.canonical_hash(source),
            "language": "sv-SE",
            "task_kind": "translation",
        }
        self.assertTrue(self.service.handle(base)["valid"])

        mutations = {
            "source": {"source_sha256": SERVICE.QUALITY.canonical_hash(source + " Geändert.")},
            "language": {"language": "de-DE"},
            "purpose": {"task_kind": "response"},
            "content_type": {"content_type": "title"},
            "short_text_reviewed": {"short_text_reviewed": True},
            "channel": {"channel": "other-hook"},
        }
        for check, change in mutations.items():
            with self.subTest(check=check):
                request = {
                    **base,
                    "delivery_grant": fresh_grant(),
                    **change,
                }
                result = self.service.handle(request)
                self.assertFalse(result["valid"], result)
                self.assertFalse(result["checks"][check], result)

    def test_audit_contains_hashes_but_no_candidate_or_token(self) -> None:
        target = "Diese vertrauliche Antwort bleibt aus dem Audit heraus."
        released = self.service.handle(self.release_request(target))
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(target, raw)
        self.assertNotIn(released["release_token"], raw)
        record = json.loads(raw)
        self.assertEqual(record["target_sha256"], SERVICE.QUALITY.canonical_hash(target))
        self.assertEqual(record["agent_id"], "test-agent")

    def test_ascii_folded_response_is_blocked_and_audited(self) -> None:
        result = self.service.handle(self.release_request("Das waere falsch."))
        self.assertFalse(result["release_allowed"])
        record = json.loads(self.audit_path.read_text(encoding="utf-8"))
        self.assertIn("suspected-ascii-substitution", record["codes"])

    def test_loopback_service_round_trip(self) -> None:
        server = SERVICE._ThreadingTCPServer(("127.0.0.1", 0), SERVICE._RequestHandler)
        server.guard_service = self.service
        server.socket_path = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"tcp:127.0.0.1:{server.server_address[1]}"
            health = CLIENT.call_guard_service(
                endpoint,
                {"operation": "health"},
                auth_token="service-secret-with-at-least-32-characters",
            )
            self.assertEqual(health["status"], "ok")
            released = CLIENT.call_guard_service(
                endpoint,
                {key: value for key, value in self.release_request().items() if key != "service_token"},
                auth_token="service-secret-with-at-least-32-characters",
            )
            self.assertTrue(released["release_allowed"], released)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @unittest.skipIf(os.name == "nt" or not hasattr(SERVICE.socket, "AF_UNIX"), "Unix socket test")
    def test_unix_service_round_trip(self) -> None:
        socket_path = Path(self.temporary.name) / "guard.sock"
        try:
            server = SERVICE.build_server(f"unix:{socket_path}", self.service)
        except PermissionError:
            self.skipTest("sandbox does not allow Unix sockets")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            health = CLIENT.call_guard_service(
                f"unix:{socket_path}",
                {"operation": "health"},
                auth_token="service-secret-with-at-least-32-characters",
            )
            self.assertEqual(health["status"], "ok")
            released = CLIENT.call_guard_service(
                f"unix:{socket_path}",
                {key: value for key, value in self.release_request().items() if key != "service_token"},
                auth_token="service-secret-with-at-least-32-characters",
            )
            self.assertTrue(released["release_allowed"], released)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
