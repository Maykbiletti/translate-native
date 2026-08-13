from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path


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
            "agent_id": "test-agent",
            "channel": "claude-hook",
        }

    def consume_request(self, delivery_grant: str, target: str = "Natürlich ist das möglich.") -> dict:
        return {
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "consume_delivery",
            "delivery_grant": delivery_grant,
            "target_text": target,
            "session_id": "session-one",
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
