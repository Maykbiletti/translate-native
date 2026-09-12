from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from tests import test_website_localization_cms_client as cms_support
from tests import test_website_localization_cms_source_delivery as delivery_support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SUBMISSION = load(
    "blun_test_website_localization_cms_source_delivery_submission_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_runtime.py",
)
SERVER = load(
    "blun_test_website_localization_cms_source_delivery_submission_server",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_auth_runtime.py",
)


class Nonces:
    def __init__(self):
        self._lock = threading.Lock()
        self._value = 0

    def __call__(self):
        with self._lock:
            self._value += 1
            return f"{self._value:032d}"


class WSGITransport:
    def __init__(self, application):
        self.application = application
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        call = (method, url, dict(headers), body, timeout)
        self.calls.append(call)
        parsed = urlsplit(url)
        raw = b"" if body is None else body
        environ = {
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": method,
            "wsgi.url_scheme": parsed.scheme,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        for name, value in headers.items():
            normalized = name.upper().replace("-", "_")
            if normalized == "CONTENT_TYPE":
                environ[normalized] = value
            elif normalized != "CONTENT_LENGTH":
                environ["HTTP_" + normalized] = value
        captured = {}
        response_body = b"".join(self.application(
            environ,
            lambda status, response_headers: captured.update(
                status=status, headers=tuple(response_headers),
            ),
        ))
        return SUBMISSION._AUTH._CLIENT.HTTPResult(
            int(captured["status"].split(" ", 1)[0]),
            captured["headers"],
            response_body,
        )


class NoNetworkTransport:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("network access was not expected")


class SourceDeliverySubmissionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.website_database = root / "website.sqlite3"
        self.sidecar_database = root / "sidecar.sqlite3"
        self.replay_database = root / "replay.sqlite3"
        self.remote = delivery_support.ScriptedClient()
        self.sidecar_digest = (
            SUBMISSION._AUTH._HTTP._capabilities_payload()["sha256"]
        )
        self.remote_digest = self.remote.expected_capabilities_sha256
        self.nonces = Nonces()
        self.old_client_credential = self.client_credential("1")
        self.new_client_credential = self.client_credential("2")
        self.old_server_credential = self.server_credential("1")
        self.new_server_credential = self.server_credential("2")
        self.sidecar = SERVER.open_hosted_hmac_authenticated_cms_source_delivery(
            self.sidecar_database,
            self.replay_database,
            self.remote,
            (self.old_server_credential, self.new_server_credential),
            worker_id="sidecar-worker",
            origin="https://delivery.example",
            sidecar_capabilities_sha256=self.sidecar_digest,
            remote_capabilities_sha256=self.remote_digest,
            clock=lambda: self.now,
            lease_seconds=60,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
        )
        self.transport = WSGITransport(self.sidecar.http)
        self.runtimes = []

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            runtime._owner_pid = os.getpid()
            runtime._delivery._owner_pid = os.getpid()
            if self.website_database.exists() and not self.website_database.is_symlink():
                os.chmod(self.website_database, 0o600)
            try:
                runtime.close(worker_timeout_seconds=1)
            except Exception:
                pass
        self.sidecar.delivery._owner_pid = os.getpid()
        self.sidecar.authentication._owner_pid = os.getpid()
        for path in (self.sidecar_database, self.replay_database):
            if path.exists() and not path.is_symlink():
                os.chmod(path, 0o600)
        try:
            self.sidecar.close(worker_timeout_seconds=1)
        except Exception:
            pass
        self.directory.cleanup()

    @staticmethod
    def _scopes(auth_module):
        return tuple(sorted(auth_module._HTTP.SCOPES.values()))

    def client_credential(self, version, *, secret=None):
        return SUBMISSION.HMACCredential(
            principal_id="website-backend",
            credential_id="website-credential",
            credential_version=version,
            secret=(bytes([int(version)]) * 32 if secret is None else secret),
            scopes=self._scopes(SUBMISSION._AUTH),
            site_id="site-1",
        )

    def server_credential(self, version, *, secret=None):
        return SERVER.HMACCredential(
            principal_id="website-backend",
            credential_id="website-credential",
            credential_version=version,
            secret=(bytes([int(version)]) * 32 if secret is None else secret),
            scopes=self._scopes(SERVER._AUTH),
            site_id="site-1",
        )

    def open(self, credential=None, *, hosted=False, **overrides):
        options = {
            "worker_id": "website-worker",
            "origin": "https://delivery.example",
            "sidecar_capabilities_sha256": self.sidecar_digest,
            "remote_capabilities_sha256": self.remote_digest,
            "sidecar_delivery_max_attempts": 4,
            "clock": lambda: self.now,
            "nonce_factory": self.nonces,
            "transport": self.transport,
            "lease_seconds": 60,
            "base_delay_seconds": 5,
            "max_delay_seconds": 20,
        }
        options.update(overrides)
        factory = (
            SUBMISSION.open_hosted_hmac_cms_source_delivery_submission
            if hosted
            else SUBMISSION.open_durable_hmac_cms_source_delivery_submission
        )
        runtime = factory(
            self.website_database,
            self.old_client_credential if credential is None else credential,
            **options,
        )
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def payload_hash(value):
        return hashlib.sha256(
            SUBMISSION._AUTH._CLIENT._canonical(value)
        ).hexdigest()

    def test_durable_composition_preserves_three_budgets_and_two_states(self):
        runtime = self.open()
        change = cms_support.event()
        queued = runtime.enqueue_change(
            change, source_max_attempts=3, delivery_max_attempts=2,
        )

        outcome = runtime.run_once()
        local = runtime.status("change", change["event_id"])
        sidecar = runtime.sidecar_status(
            "change",
            change["event_id"],
            change["event_id"],
            change["site_id"],
            self.payload_hash(change),
        )["status"]

        self.assertEqual((queued.delivery_max_attempts, queued.source_max_attempts), (2, 3))
        self.assertEqual((outcome.status, local.status), ("succeeded", "succeeded"))
        self.assertEqual(sidecar["delivery_max_attempts"], 4)
        self.assertEqual(sidecar["source_max_attempts"], 3)
        self.assertIn(sidecar["status"], {"pending", "leased", "succeeded"})
        self.assertEqual(stat.S_IMODE(self.website_database.stat().st_mode), 0o600)
        self.assertEqual(runtime.health().status, "ok")

    def test_sidecar_lifecycle_uses_the_owned_authenticated_client(self):
        runtime = self.open()

        capabilities = runtime.sidecar_capabilities()["capabilities"]
        health = runtime.sidecar_health()["health"]
        readiness = runtime.sidecar_readiness()["readiness"]

        self.assertEqual(capabilities["sha256"], self.sidecar_digest)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(len(self.transport.calls), 5)

    def test_rotation_reaches_the_exact_signer_without_reopening_outbox(self):
        runtime = self.open()
        inode = self.website_database.stat().st_ino
        first = cms_support.event()
        runtime.enqueue_change(first)
        runtime.run_once()
        split = len(self.transport.calls)

        runtime.replace_credential(self.new_client_credential)
        second = cms_support.event(
            event_id="event-2",
            website_version="web-2",
            source_sequence=2,
            source_revision="cms-2",
        )
        runtime.enqueue_change(second)
        runtime.run_once()

        version_header = SUBMISSION._AUTH.HEADER_CREDENTIAL_VERSION
        self.assertEqual(self.website_database.stat().st_ino, inode)
        self.assertTrue(all(
            call[2][version_header] == "1" for call in self.transport.calls[:split]
        ))
        self.assertTrue(all(
            call[2][version_header] == "2" for call in self.transport.calls[split:]
        ))
        self.assertEqual(runtime.status("change", "event-2").status, "succeeded")

    def test_invalid_middle_policy_creates_no_database_and_makes_no_request(self):
        transport = NoNetworkTransport()

        with self.assertRaises(
            SUBMISSION._ADAPTER.CMSSourceDeliverySidecarAdapterBlocked,
        ) as caught:
            self.open(
                sidecar_delivery_max_attempts=0,
                transport=transport,
            )

        self.assertEqual(
            caught.exception.code,
            "source_delivery_sidecar_adapter.attempts_invalid",
        )
        self.assertFalse(self.website_database.exists())
        self.assertEqual(transport.calls, [])

    def test_invalid_hosted_delay_creates_no_database_and_makes_no_request(self):
        transport = NoNetworkTransport()

        with self.assertRaises(
            SUBMISSION._RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked,
        ) as caught:
            self.open(
                hosted=True,
                idle_delay_seconds=0,
                transport=transport,
            )

        self.assertEqual(
            caught.exception.code,
            "source_delivery_runtime.loop_invalid",
        )
        self.assertFalse(self.website_database.exists())
        self.assertEqual(transport.calls, [])

    def test_hosted_worker_is_owned_and_ready(self):
        runtime = self.open(
            hosted=True,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
        )

        self.assertEqual(runtime.worker_state, "running")
        self.assertEqual(runtime.worker_readiness()["status"], "ready")
        runtime.stop_worker(timeout_seconds=1)
        self.assertEqual(runtime.worker_state, "stopped")

    def test_process_close_and_representation_fail_closed_without_secrets(self):
        runtime = self.open()
        rendered = repr(runtime)
        self.assertNotIn("delivery.example", rendered)
        self.assertNotIn("website-credential", rendered)
        self.assertNotIn(self.old_client_credential.secret.hex(), rendered)

        runtime._owner_pid += 1
        self.assertEqual(runtime.state, "foreign-process")
        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as foreign:
            runtime.sidecar_health()
        self.assertEqual(
            foreign.exception.code,
            "source_delivery_submission_runtime.foreign_process",
        )
        runtime._owner_pid = os.getpid()
        runtime.close()
        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as closed:
            runtime.replace_credential(self.new_client_credential)
        self.assertEqual(
            closed.exception.code,
            "source_delivery_submission_runtime.closed",
        )


if __name__ == "__main__":
    unittest.main()
