from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
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

    def test_service_token_file_is_bounded_and_stable(self) -> None:
        root = Path(self.temporary.name)
        token_path = root / "service.token"
        token_path.write_text("s" * 64 + "\n", encoding="ascii")
        token_path.chmod(0o600)
        self.assertEqual(CLIENT.load_service_token(token_path), "s" * 64)

        details = token_path.stat()
        opened = SimpleNamespace(
            st_mode=details.st_mode,
            st_size=details.st_size,
            st_uid=details.st_uid,
            st_dev=details.st_dev,
            st_ino=details.st_ino,
            st_ctime_ns=details.st_ctime_ns,
            st_mtime_ns=details.st_mtime_ns,
        )
        changed = SimpleNamespace(**vars(opened))
        changed.st_mtime_ns += 1
        with mock.patch.object(CLIENT, "_token_fstat", side_effect=(opened, changed)):
            with self.assertRaisesRegex(CLIENT.GuardServiceError, "changed while reading"):
                CLIENT.load_service_token(token_path)

        token_path.write_bytes(b"x" * (CLIENT.MAX_SERVICE_TOKEN_BYTES + 1))
        with self.assertRaisesRegex(CLIENT.GuardServiceError, "invalid size"):
            CLIENT.load_service_token(token_path)

    @unittest.skipIf(os.name == "nt", "POSIX link and permission test")
    def test_service_token_file_rejects_links_and_open_permissions(self) -> None:
        root = Path(self.temporary.name)
        token_path = root / "service.token"
        token_path.write_text("s" * 64 + "\n", encoding="ascii")
        token_path.chmod(0o600)
        linked = root / "linked.token"
        linked.symlink_to(token_path)
        with self.assertRaisesRegex(CLIENT.GuardServiceError, "regular file"):
            CLIENT.load_service_token(linked)
        token_path.chmod(0o644)
        with self.assertRaisesRegex(CLIENT.GuardServiceError, "owner-only"):
            CLIENT.load_service_token(token_path)

    @unittest.skipIf(os.name == "nt", "POSIX service-token directory safety test")
    def test_service_token_runtime_rejects_unsafe_parent_directories(self) -> None:
        root = Path(self.temporary.name)
        redirected = root / "redirected"
        redirected.mkdir()
        redirected_token = redirected / "service.token"
        redirected_token.write_text("r" * 64 + "\n", encoding="ascii")
        redirected_token.chmod(0o600)
        linked = root / "linked"
        linked.symlink_to(redirected, target_is_directory=True)

        with self.assertRaisesRegex(CLIENT.GuardServiceError, "token directory"):
            CLIENT.load_service_token(linked / "service.token")

        writable = root / "writable"
        writable.mkdir()
        writable_token = writable / "service.token"
        writable_token.write_text("w" * 64 + "\n", encoding="ascii")
        writable_token.chmod(0o600)
        writable.chmod(0o777)
        try:
            with self.assertRaisesRegex(CLIENT.GuardServiceError, "writable outside"):
                CLIENT.load_service_token(writable_token)
        finally:
            writable.chmod(0o700)

    def test_service_token_runtime_does_not_create_missing_parent_directories(self) -> None:
        token_path = Path(self.temporary.name) / "missing" / "nested" / "service.token"

        with self.assertRaises(FileNotFoundError):
            CLIENT.load_service_token(token_path)

        self.assertFalse(token_path.parent.exists())

    @unittest.skipIf(os.name == "nt", "POSIX service-token directory identity test")
    def test_service_token_runtime_detects_parent_exchange_after_open(self) -> None:
        root = Path(self.temporary.name)
        trusted = root / "trusted"
        trusted.mkdir()
        token_path = trusted / "service.token"
        token_path.write_text("t" * 64 + "\n", encoding="ascii")
        token_path.chmod(0o600)
        replacement = root / "replacement"
        replacement.mkdir()
        replacement_token = replacement / "service.token"
        replacement_token.write_text("x" * 64 + "\n", encoding="ascii")
        replacement_token.chmod(0o600)
        displaced = root / "displaced"
        real_validate = CLIENT._validate_token_file
        calls = 0

        def exchange_after_open(details) -> None:
            nonlocal calls
            real_validate(details)
            calls += 1
            if calls == 2:
                trusted.rename(displaced)
                replacement.rename(trusted)

        with mock.patch.object(CLIENT, "_validate_token_file", side_effect=exchange_after_open):
            with self.assertRaisesRegex(CLIENT.GuardServiceError, "token directory changed"):
                CLIENT.load_service_token(token_path)

        self.assertEqual((trusted / "service.token").read_text(encoding="ascii").strip(), "x" * 64)
        self.assertEqual((displaced / "service.token").read_text(encoding="ascii").strip(), "t" * 64)

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
            "audit_paths": True,
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

    def test_session_retirement_is_epoch_bound_and_blocks_old_grants(self) -> None:
        released = self.service.handle(self.release_request())
        old_authorized = self.service.handle(self.authorize_request(released["release_token"]))
        self.assertTrue(old_authorized["valid"], old_authorized)

        retired = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "retire_session_epoch",
            "session_id": "session-one",
            "session_epoch": self.session_epoch,
        })
        self.assertEqual(retired, {"status": "PASS", "retired": True})
        self.assertNotIn(self.session_epoch, json.dumps(retired))

        restored = self.service.handle(
            self.consume_request(old_authorized["delivery_grant"])
        )
        self.assertFalse(restored["valid"], restored)
        self.assertFalse(restored["checks"]["session_epoch_current"])

        stale_authorization = self.service.handle(
            self.authorize_request(released["release_token"])
        )
        self.assertFalse(stale_authorization["valid"], stale_authorization)
        self.assertFalse(stale_authorization["checks"]["session_epoch_current"])

        new_epoch = "b" * 64
        registered = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "register_session_epoch",
            "session_id": "session-one",
            "session_epoch": new_epoch,
        })
        self.assertTrue(registered["registered"], registered)

        delayed_retirement = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "retire_session_epoch",
            "session_id": "session-one",
            "session_epoch": self.session_epoch,
        })
        self.assertEqual(delayed_retirement, {"status": "BLOCK", "retired": False})

        fresh_request = self.authorize_request(released["release_token"])
        fresh_request["session_epoch"] = new_epoch
        fresh = self.service.handle(fresh_request)
        self.assertTrue(fresh["valid"], fresh)

    def test_fresh_release_recovers_session_after_service_restart(self) -> None:
        old_release = self.service.handle(self.release_request())
        old_authorized = self.service.handle(self.authorize_request(old_release["release_token"]))
        self.assertTrue(old_authorized["valid"], old_authorized)

        restarted = SERVICE.GuardService(
            self.key_path, self.audit_path, "service-secret-with-at-least-32-characters"
        )
        old_result = restarted.handle(self.consume_request(old_authorized["delivery_grant"]))
        self.assertFalse(old_result["valid"], old_result)
        self.assertFalse(old_result["checks"]["service_boot"])
        self.assertFalse(old_result["checks"]["session_epoch_current"])

        forged = restarted.handle(self.authorize_request("forged-release-token"))
        self.assertFalse(forged["valid"], forged)
        session_hash = SERVICE.GuardService._identity_hash("session-one")
        self.assertNotIn(session_hash, restarted.session_epochs)

        fresh_release = restarted.handle(self.release_request())
        recovered = restarted.handle(self.authorize_request(fresh_release["release_token"]))
        self.assertTrue(recovered["valid"], recovered)
        self.assertEqual(
            restarted.session_epochs[session_hash],
            SERVICE.GuardService._identity_hash(self.session_epoch),
        )
        consumed = restarted.handle(self.consume_request(recovered["delivery_grant"]))
        self.assertTrue(consumed["valid"], consumed)

        restored_old_grant = restarted.handle(self.consume_request(old_authorized["delivery_grant"]))
        self.assertFalse(restored_old_grant["valid"], restored_old_grant)
        self.assertFalse(restored_old_grant["checks"]["service_boot"])

        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(self.session_epoch, raw)

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
        if os.name != "nt":
            self.assertEqual(self.audit_path.stat().st_mode & 0o077, 0)
            self.assertEqual(
                self.audit_path.with_suffix(".jsonl.lock").stat().st_mode & 0o077,
                0,
            )

    @unittest.skipIf(os.name == "nt", "POSIX link, FIFO, and permission test")
    def test_unsafe_audit_paths_block_without_altering_targets(self) -> None:
        root = Path(self.temporary.name)
        sentinel = root / "sentinel.txt"
        sentinel.write_text("do-not-append\n", encoding="utf-8")
        sentinel.chmod(0o600)

        self.audit_path.symlink_to(sentinel)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            self.service.handle(self.release_request())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-append\n")
        self.audit_path.unlink()

        os.link(sentinel, self.audit_path)
        with self.assertRaisesRegex(RuntimeError, "hard links"):
            self.service.handle(self.release_request())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-append\n")
        self.audit_path.unlink()

        lock_path = self.audit_path.with_suffix(".jsonl.lock")
        lock_path.unlink(missing_ok=True)
        lock_path.symlink_to(sentinel)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            self.service.handle(self.release_request())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-append\n")
        lock_path.unlink()

        os.mkfifo(self.audit_path)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            self.service.handle(self.release_request())
        self.audit_path.unlink()

        self.audit_path.write_text("", encoding="utf-8")
        self.audit_path.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, "writable outside"):
            self.service.handle(self.release_request())

    @unittest.skipIf(os.name == "nt", "POSIX audit-directory safety test")
    def test_unsafe_audit_parent_directories_block_without_writing(self) -> None:
        root = Path(self.temporary.name)
        redirected = root / "redirected"
        redirected.mkdir()
        linked = root / "linked"
        linked.symlink_to(redirected, target_is_directory=True)
        linked_audit = linked / "audit.jsonl"

        with self.assertRaisesRegex(RuntimeError, "audit directory"):
            SERVICE.AUDIT.append_audit(linked_audit, {"event": "linked-parent"})
        self.assertFalse((redirected / "audit.jsonl").exists())
        self.assertFalse((redirected / "audit.jsonl.lock").exists())
        self.assertFalse(SERVICE.AUDIT.audit_paths_healthy(linked_audit))

        writable = root / "writable"
        writable.mkdir()
        writable.chmod(0o777)
        try:
            writable_audit = writable / "audit.jsonl"
            with self.assertRaisesRegex(RuntimeError, "writable outside"):
                SERVICE.AUDIT.append_audit(writable_audit, {"event": "open-parent"})
            self.assertFalse(writable_audit.exists())
            self.assertFalse(SERVICE.AUDIT.audit_paths_healthy(writable_audit))
        finally:
            writable.chmod(0o700)

    def test_audit_health_does_not_create_missing_parent_directories(self) -> None:
        missing = Path(self.temporary.name) / "missing" / "nested" / "audit.jsonl"

        self.assertTrue(SERVICE.AUDIT.audit_paths_healthy(missing))
        self.assertFalse(missing.parent.exists())

    @unittest.skipIf(os.name == "nt", "POSIX audit-directory identity test")
    def test_audit_append_pins_parent_across_lock_and_log(self) -> None:
        root = Path(self.temporary.name)
        trusted = root / "trusted"
        trusted.mkdir()
        audit_path = trusted / "audit.jsonl"
        replacement = root / "replacement"
        replacement.mkdir()
        displaced = root / "displaced"
        real_lock = SERVICE.AUDIT._exclusive_lock

        @contextmanager
        def exchange_parent(handle):
            with real_lock(handle):
                trusted.rename(displaced)
                replacement.rename(trusted)
                yield

        with mock.patch.object(SERVICE.AUDIT, "_exclusive_lock", exchange_parent):
            with self.assertRaisesRegex(RuntimeError, "audit directory changed"):
                SERVICE.AUDIT.append_audit(audit_path, {"event": "parent-race"})

        self.assertFalse((trusted / "audit.jsonl").exists())
        self.assertTrue((displaced / "audit.jsonl").exists())

    @unittest.skipIf(os.name == "nt", "POSIX audit-path link test")
    def test_health_blocks_on_unsafe_audit_state_without_writing_it(self) -> None:
        sentinel = Path(self.temporary.name) / "health-sentinel.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        self.audit_path.symlink_to(sentinel)
        health = self.service.handle({
            "service_token": "service-secret-with-at-least-32-characters",
            "operation": "health",
        })
        self.assertEqual(health["status"], "BLOCK", health)
        self.assertFalse(health["isolated_key"])
        self.assertFalse(health["self_test"]["audit_paths"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")

    def test_audit_path_exchange_blocks_after_append(self) -> None:
        replacement = Path(self.temporary.name) / "replacement.jsonl"
        old_path = Path(self.temporary.name) / "old-audit.jsonl"
        real_fsync = SERVICE.AUDIT.os.fsync
        exchanged = False

        def exchange_after_write(descriptor: int) -> None:
            nonlocal exchanged
            real_fsync(descriptor)
            if exchanged:
                return
            exchanged = True
            self.audit_path.rename(old_path)
            replacement.write_text("replacement\n", encoding="utf-8")
            replacement.chmod(0o600)
            replacement.rename(self.audit_path)

        with mock.patch.object(SERVICE.AUDIT.os, "fsync", side_effect=exchange_after_write):
            with self.assertRaisesRegex(RuntimeError, "changed while in use"):
                SERVICE.AUDIT.append_audit(self.audit_path, {"event": "race-test"})
        self.assertEqual(self.audit_path.read_text(encoding="utf-8"), "replacement\n")

    def test_protected_audit_append_preserves_concurrent_records(self) -> None:
        errors: list[Exception] = []

        def append(index: int) -> None:
            try:
                SERVICE.AUDIT.append_audit(
                    self.audit_path,
                    {"event": f"parallel-{index}", "allowed": True},
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        workers = [threading.Thread(target=append, args=(index,)) for index in range(16)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(errors, [])
        records = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 16)
        self.assertEqual(
            {record["event"] for record in records},
            {f"parallel-{index}" for index in range(16)},
        )

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
