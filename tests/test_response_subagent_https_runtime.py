"""Synthetic deployment tests; not evidence of native-language quality."""

from __future__ import annotations

import hashlib
import hmac
import base64
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_guard_service as GUARD
import test_website_localization_subagent_http as HTTPS


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = GUARD.SERVICE.RESPONSE_REVIEW_RUNTIME


SECRET = "cHJvZHVjdGlvbi1maXh0dXJlLWF0dGVzdGF0aW9uLWtleS0wMDE="
TOKEN = "fixture-review-host-token-with-more-than-32-characters"
KEY_ID = "fixture-runtime-key-1"
TARGETS = {
    "fi-FI": "Löydä yrityksellesi luonteva seuraava askel.",
    "mt-MT": "Agħżel il-pass li jmiss għan-negozju tiegħek.",
}


class RuntimeAuthority:
    algorithm = "hmac-sha256"
    key_id = KEY_ID

    def __init__(self, secret: str = SECRET):
        self.secret = secret.encode("ascii")

    def sign(self, payload):
        return {
            "schema": HTTPS.HTTP.ATTESTATION_SCHEMA,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "signature": hmac.new(self.secret, payload, hashlib.sha256).hexdigest(),
        }


class RuntimeFixtureTransport(HTTPS.FixtureHostTransport):
    def post(self, url, headers, body, *, timeout):
        result = super().post(url, headers, body, timeout=timeout)
        return RUNTIME.HTTP.HTTPResult(result.status, result.headers, result.body)


class BlockingTransport(RuntimeFixtureTransport):
    def __init__(self, authority):
        super().__init__(authority)
        self.entered = threading.Event()
        self.release = threading.Event()

    def post(self, url, headers, body, *, timeout):
        self.entered.set()
        if not self.release.wait(2):
            raise TimeoutError
        return super().post(url, headers, body, timeout=timeout)


class ResponseSubagentHTTPSRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token_path = self.root / "review.token"
        self.secret_path = self.root / "attestation.secret"
        self.config_path = self.root / "response-review.json"
        self.token_path.write_text(TOKEN + "\n", encoding="ascii")
        self.secret_path.write_text(SECRET + "\n", encoding="ascii")
        self.token_path.chmod(0o600)
        self.secret_path.chmod(0o600)
        self.write_config()

    def configuration(self):
        return {
            "schema": RUNTIME.CONFIG_SCHEMA,
            "endpoint": "https://review-host.example/v1/subagent-reviews",
            "host_id": "trusted-runtime-host-1",
            "allow_loopback_http": False,
            "authentication": {
                "scheme": "bearer", "token_file": str(self.token_path),
            },
            "attestation": {
                "algorithm": "hmac-sha256", "key_id": KEY_ID,
                "secret_file": str(self.secret_path),
            },
            "review": {
                "model_id": "review-model", "model_version": "model-1",
                "host_policy_version": "isolated-host-1",
                "quality_profile_version": "eu-native-1",
                "prompt_version": "native-prompt-1", "software_version": "6.186.0",
                "timeout_seconds": 60, "max_output_tokens": 4096,
                "max_concurrent_reviews": 1,
                "native_brief": {
                    "audience": "Website users",
                    "tone_profile": "Natural and clear",
                    "target_terms": ["BLUN"],
                },
            },
        }

    def write_config(self, mutate=None):
        value = self.configuration()
        if mutate:
            mutate(value)
        self.config_path.write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8",
        )
        self.config_path.chmod(0o600)

    def reviewer(self, transport):
        return RUNTIME.build_response_reviewer_from_config(
            self.config_path, transport=transport,
            response_module=GUARD.SERVICE.RESPONSE_REVIEW,
        )

    def test_guard_uses_bundled_factory_for_finnish_and_maltese_end_to_end(self):
        for locale, target in TARGETS.items():
            with self.subTest(locale=locale):
                authority = RuntimeAuthority()
                transport = RuntimeFixtureTransport(authority)
                with mock.patch.object(
                    RUNTIME, "DeadlineURLTransport", return_value=transport,
                ):
                    reviewer = GUARD.SERVICE._response_reviewer_from_config(self.config_path)
                service = GUARD.SERVICE.GuardService(
                    self.root / f"{locale}.signing.key",
                    self.root / f"{locale}.audit.jsonl",
                    "service-secret-with-at-least-32-characters", reviewer,
                )
                epoch = "a" * 64
                service.handle({
                    "service_token": "service-secret-with-at-least-32-characters",
                    "operation": "register_session_epoch", "session_id": "session-one",
                    "session_epoch": epoch,
                })
                prepared = service.handle({
                    "service_token": "service-secret-with-at-least-32-characters",
                    "operation": "prepare_response_review", "task_kind": "response",
                    "target_text": target, "language": locale, "content_type": "prose",
                    "session_id": "session-one", "session_epoch": epoch,
                    "agent_id": "creator-main", "channel": "test",
                })
                released = service.handle({
                    "service_token": "service-secret-with-at-least-32-characters",
                    "operation": "release", "task_kind": "response",
                    "target_text": target, "language": locale, "content_type": "prose",
                    "agent_id": "creator-main", "channel": "test",
                    "review_context_token": prepared["review_context_token"],
                })
                self.assertTrue(released["release_allowed"], released)
                request = json.loads(transport.calls[0][2].decode("utf-8"))
                self.assertEqual(set(request["task"]["input"]), HTTPS.HTTP.NATIVE_INPUT_FIELDS)
                serialized = transport.calls[0][2].decode("utf-8")
                self.assertNotIn("source_text", serialized)
                self.assertNotIn("creator-main", serialized)
                self.assertNotIn(TOKEN, serialized)
                self.assertNotIn(SECRET, serialized)
                self.assertEqual(
                    transport.calls[0][1]["Authorization"], "Bearer " + TOKEN,
                )

    def test_wrong_attestation_key_and_changed_config_fail_closed(self):
        transport = RuntimeFixtureTransport(RuntimeAuthority("e" * 64))
        reviewer = self.reviewer(transport)
        with self.assertRaisesRegex(
            GUARD.SERVICE.RESPONSE_REVIEW.ResponseReviewBlocked,
            "response_review.http.attestation_rejected",
        ):
            reviewer.review(
                TARGETS["fi-FI"], "fi-FI", "prose",
                creator_id_sha256=hashlib.sha256(b"creator").hexdigest(),
                creator_session_id_sha256=hashlib.sha256(b"session").hexdigest(),
            )

        self.write_config(lambda value: value["review"].update(
            {"max_concurrent_reviews": True},
        ))
        with self.assertRaisesRegex(RUNTIME.ResponseReviewRuntimeError, "budgets"):
            RUNTIME.load_runtime_config(self.config_path)
        self.write_config(lambda value: value["review"].update(
            {"timeout_seconds": 61},
        ))
        with self.assertRaisesRegex(RUNTIME.ResponseReviewRuntimeError, "budgets"):
            RUNTIME.load_runtime_config(self.config_path)

    def test_configuration_and_secrets_are_protected_regular_files(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions and links")
        self.config_path.chmod(0o644)
        with self.assertRaisesRegex(RUNTIME.ResponseReviewRuntimeError, "owner-only"):
            RUNTIME.load_runtime_config(self.config_path)
        self.config_path.unlink()
        self.config_path.symlink_to(self.secret_path)
        with self.assertRaisesRegex(RUNTIME.ResponseReviewRuntimeError, "regular file"):
            RUNTIME.load_runtime_config(self.config_path)

        self.config_path.unlink()
        self.write_config()
        hardlink = self.root / "secret-copy"
        os.link(self.secret_path, hardlink)
        with self.assertRaisesRegex(RUNTIME.ResponseReviewRuntimeError, "hard links"):
            self.reviewer(RuntimeFixtureTransport(RuntimeAuthority()))

        trusted = self.root / "trusted"
        trusted.mkdir()
        nested = trusted / "nested.json"
        nested.write_text("{}", encoding="utf-8")
        nested.chmod(0o600)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(trusted, target_is_directory=True)
        with self.assertRaisesRegex(
            RUNTIME.ResponseReviewRuntimeError, "opened safely",
        ):
            RUNTIME.load_runtime_config(linked_parent / "nested.json")

    def test_capacity_is_bounded_without_an_internal_retry_loop(self):
        transport = BlockingTransport(RuntimeAuthority())
        reviewer = self.reviewer(transport)
        outcome = []

        def first():
            try:
                outcome.append(reviewer.review(
                    TARGETS["mt-MT"], "mt-MT", "prose",
                    creator_id_sha256=hashlib.sha256(b"creator-one").hexdigest(),
                    creator_session_id_sha256=hashlib.sha256(b"session-one").hexdigest(),
                ))
            except Exception as error:  # pragma: no cover - assertion reports it
                outcome.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(transport.entered.wait(1))
        with self.assertRaisesRegex(
            GUARD.SERVICE.RESPONSE_REVIEW.ResponseReviewBlocked,
            "response_review.host_capacity",
        ) as blocked:
            reviewer.review(
                TARGETS["fi-FI"], "fi-FI", "prose",
                creator_id_sha256=hashlib.sha256(b"creator-two").hexdigest(),
                creator_session_id_sha256=hashlib.sha256(b"session-two").hexdigest(),
            )
        self.assertTrue(blocked.exception.retryable)
        self.assertEqual(len(transport.calls), 0)
        transport.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], dict)
        self.assertEqual(len(transport.calls), 1)

    def test_pinned_verifier_never_exposes_its_secret(self):
        verifier = RUNTIME.PinnedHMACAttestationVerifier(SECRET, KEY_ID)
        self.assertNotIn(SECRET, repr(verifier))
        payload = b"host-bound-payload"
        valid = RuntimeAuthority().sign(payload)
        self.assertTrue(verifier.verify(payload, valid))
        self.assertFalse(verifier.verify(payload + b"!", valid))
        self.assertFalse(verifier.verify(payload, {**valid, "key_id": "other-key"}))

    def test_deadline_transport_kills_and_reaps_a_timed_out_exchange(self):
        process = mock.Mock()
        process.pid = 12345
        process.poll.return_value = None
        process.communicate.side_effect = (
            RUNTIME.subprocess.TimeoutExpired(["worker"], 1),
            (b"", b""),
        )
        kill_patch = (
            mock.patch.object(RUNTIME.os, "killpg")
            if hasattr(RUNTIME.os, "killpg") else nullcontext(None)
        )
        with mock.patch.object(RUNTIME.subprocess, "Popen", return_value=process), \
                kill_patch as kill_group:
            with self.assertRaisesRegex(
                RUNTIME.HTTP.HTTPReviewHostFailed, "http.timeout",
            ) as blocked:
                RUNTIME.DeadlineURLTransport().post(
                    "https://review-host.example/v1/subagent-reviews",
                    {"Authorization": "Bearer " + TOKEN}, b"{}", timeout=1,
                )
        self.assertTrue(blocked.exception.retryable)
        if os.name == "nt":
            process.kill.assert_called_once_with()
        else:
            kill_group.assert_called_once_with(12345, RUNTIME.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    @unittest.skipIf(os.name == "nt", "POSIX process-group regression")
    def test_deadline_does_not_wait_for_descendant_holding_console_pipes(self):
        real_popen = subprocess.Popen
        fixture = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)']);"
            "time.sleep(3)"
        )

        def spawn_fixture(*_args, **kwargs):
            return real_popen([sys.executable, "-c", fixture], **kwargs)

        started = time.monotonic()
        with mock.patch.object(
                RUNTIME.subprocess, "Popen", side_effect=spawn_fixture):
            with self.assertRaisesRegex(
                    RUNTIME.HTTP.HTTPReviewHostFailed, "http.timeout"):
                RUNTIME.DeadlineURLTransport().post(
                    "https://review-host.example/v1/subagent-reviews",
                    {"Authorization": "Bearer " + TOKEN}, b"{}", timeout=0.05,
                )
        self.assertLess(time.monotonic() - started, 1.25)

    def test_transport_worker_uses_closed_json_protocol(self):
        incoming = json.dumps({
            "url": "https://review-host.example/v1/subagent-reviews",
            "headers": {"Authorization": "Bearer " + TOKEN},
            "body": base64.b64encode(b"request-body").decode("ascii"),
            "timeout": 1,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        output = io.BytesIO()
        result = RUNTIME.HTTP.HTTPResult(
            200, (("Content-Type", "application/json"),), b"response-body",
        )
        with mock.patch.object(RUNTIME.sys, "stdin", SimpleNamespace(
                buffer=io.BytesIO(incoming))), \
                mock.patch.object(RUNTIME.sys, "stdout", SimpleNamespace(
                    buffer=output)), \
                mock.patch.object(RUNTIME.HTTP, "URLTransport") as transport:
            transport.return_value.post.return_value = result
            self.assertEqual(RUNTIME._transport_worker(), 0)
        reply = json.loads(output.getvalue().decode("utf-8"))
        self.assertEqual(reply["result"]["status"], 200)
        self.assertEqual(
            base64.b64decode(reply["result"]["body"]), b"response-body",
        )
        call = transport.return_value.post.call_args
        self.assertEqual(call.args[2], b"request-body")
        self.assertEqual(call.kwargs["timeout"], 1.0)

    def test_deadline_transport_runs_the_real_one_request_worker(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(inner):
                length = int(inner.headers.get("Content-Length", "0"))
                received.append(inner.rfile.read(length))
                body = b'{"worker":true}'
                inner.send_response(200)
                inner.send_header("Content-Type", "application/json")
                inner.send_header("Content-Length", str(len(body)))
                inner.end_headers()
                inner.wfile.write(body)

            def log_message(self, _format, *_arguments):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            result = RUNTIME.DeadlineURLTransport().post(
                f"http://127.0.0.1:{server.server_port}/review",
                {"Authorization": "Bearer " + TOKEN}, b"request-body", timeout=2,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b'{"worker":true}')
        self.assertEqual(received, [b"request-body"])

    def test_bundled_factory_uses_the_wall_deadline_transport_by_default(self):
        reviewer = RUNTIME.build_response_reviewer_from_config(
            self.config_path, response_module=GUARD.SERVICE.RESPONSE_REVIEW,
        )
        self.assertIsInstance(
            reviewer._host._host.transport, RUNTIME.DeadlineURLTransport,
        )


if __name__ == "__main__":
    unittest.main()
