from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
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


RUNTIME = load(
    "blun_test_website_localization_cms_source_delivery_auth_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_auth_runtime.py",
)
AUTH = RUNTIME._AUTH
CLIENT = AUTH._CLIENT
HTTP = AUTH._HTTP


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
        return CLIENT.HTTPResult(
            int(captured["status"].split(" ", 1)[0]),
            captured["headers"],
            response_body,
        )


class BlockingClient(delivery_support.ScriptedClient):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def submit_change(self, value, *, max_attempts):
        self.entered.set()
        self.release.wait(5)
        return super().submit_change(value, max_attempts=max_attempts)


class SourceDeliveryHMACRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.outbox = Path(self.directory.name) / "delivery.sqlite3"
        self.replay = Path(self.directory.name) / "replay.sqlite3"
        self.remote_client = delivery_support.ScriptedClient()
        self.sidecar_digest = HTTP._capabilities_payload()["sha256"]
        self.remote_digest = self.remote_client.expected_capabilities_sha256
        self.nonces = Nonces()
        self.credential = self.make_credential("1")
        self.runtimes = []

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            runtime.delivery._owner_pid = os.getpid()
            runtime.authentication._owner_pid = os.getpid()
            for path in (self.outbox, self.replay):
                if path.exists() and not path.is_symlink():
                    os.chmod(path, 0o600)
            try:
                runtime.close(worker_timeout_seconds=1)
            except Exception:
                pass
        self.directory.cleanup()

    def make_credential(self, version, *, secret=None, site="site-1"):
        return RUNTIME.HMACCredential(
            principal_id="website-backend",
            credential_id="website-credential",
            credential_version=version,
            secret=(bytes([int(version)]) * 32 if secret is None else secret),
            scopes=tuple(sorted(HTTP.SCOPES.values())),
            site_id=site,
        )

    def open(self, *, remote_client=None, credentials=None, **overrides):
        options = {
            "worker_id": "website-source-worker",
            "origin": "https://delivery.example",
            "sidecar_capabilities_sha256": self.sidecar_digest,
            "remote_capabilities_sha256": self.remote_digest,
            "clock": lambda: self.now,
            "lease_seconds": 60,
            "active_delay_seconds": 10,
            "idle_delay_seconds": 10,
            "blocked_delay_seconds": 10,
        }
        options.update(overrides)
        runtime = RUNTIME.open_hosted_hmac_authenticated_cms_source_delivery(
            self.outbox,
            self.replay,
            self.remote_client if remote_client is None else remote_client,
            (self.credential,) if credentials is None else credentials,
            **options,
        )
        self.runtimes.append(runtime)
        return runtime

    def signer(self, credential=None):
        return AUTH.SourceDeliveryHMACSigner(
            self.credential if credential is None else credential,
            origin="https://delivery.example",
            sidecar_capabilities_sha256=self.sidecar_digest,
            remote_capabilities_sha256=self.remote_digest,
            clock=lambda: self.now,
            nonce_factory=self.nonces,
        )

    def rotating_signer(self, credential=None, *, nonce_factory=None):
        return RUNTIME.RotatingSourceDeliveryHMACSigner(
            self.credential if credential is None else credential,
            origin="https://delivery.example",
            sidecar_capabilities_sha256=self.sidecar_digest,
            remote_capabilities_sha256=self.remote_digest,
            clock=lambda: self.now,
            nonce_factory=self.nonces if nonce_factory is None else nonce_factory,
        )

    def http_client(self, runtime, credential=None):
        transport = WSGITransport(runtime.http)
        client = CLIENT.CMSSourceDeliverySidecarHTTPClient(
            "https://delivery.example",
            self.sidecar_digest,
            self.remote_digest,
            self.signer(credential),
            transport=transport,
        )
        return client, transport

    def test_hosted_composition_is_private_ready_and_end_to_end(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)

        capabilities = client.capabilities()
        queued = client.submit_change(cms_support.event())["queue"]
        health = client.health()["health"]
        auth_health = runtime.authentication_health()

        self.assertEqual(capabilities["capabilities"]["sha256"], self.sidecar_digest)
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(runtime.worker_readiness()["status"], "ready")
        self.assertEqual(auth_health["status"], "ok")
        self.assertEqual(auth_health["consumed_nonces"], 5)
        self.assertEqual(stat.S_IMODE(self.outbox.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.replay.stat().st_mode), 0o600)

    def test_restart_preserves_replay_rejection(self):
        first = self.open()
        client, transport = self.http_client(first)
        client.capabilities()
        captured = transport.calls[0]
        first.close()

        resumed = self.open()
        replay_transport = WSGITransport(resumed.http)
        response = replay_transport.request(
            captured[0], captured[1], captured[2], captured[3],
            timeout=captured[4],
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(
            resumed.authentication_health()["consumed_nonces"], 1,
        )

    def test_live_rotation_overlaps_then_retires_one_generation(self):
        runtime = self.open()
        first_client, _first_transport = self.http_client(runtime)
        second_credential = self.make_credential("2")
        second_client, _second_transport = self.http_client(
            runtime, second_credential,
        )

        first_client.capabilities()
        runtime.replace_credentials((self.credential, second_credential))
        first_client.capabilities()
        second_client.capabilities()
        runtime.replace_credentials((second_credential,))

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as retired:
            first_client.capabilities()
        self.assertEqual(retired.exception.code, "source_delivery_client.http_status")
        self.assertFalse(retired.exception.retryable)
        self.assertEqual(
            second_client.capabilities()["capabilities"]["sha256"],
            self.sidecar_digest,
        )
        self.assertEqual(
            runtime.authentication_health()["consumed_nonces"], 4,
        )

    def test_failed_rotation_preserves_the_last_valid_generation(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        private = "must-not-escape"

        def broken_credentials():
            yield self.make_credential("2")
            raise RuntimeError(private)

        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked) as caught:
            runtime.replace_credentials(broken_credentials())

        self.assertEqual(
            caught.exception.code,
            "source_delivery_hmac_runtime.configuration_invalid",
        )
        self.assertNotIn(private, str(caught.exception))
        self.assertEqual(
            client.capabilities()["capabilities"]["sha256"],
            self.sidecar_digest,
        )

    def test_rotation_keeps_consumed_nonces_across_remove_and_readd(self):
        runtime = self.open()
        client, transport = self.http_client(runtime)
        client.capabilities()
        captured = transport.calls[0]
        second_credential = self.make_credential("2")

        runtime.replace_credentials((second_credential,))
        runtime.replace_credentials((self.credential, second_credential))
        replay_transport = WSGITransport(runtime.http)
        response = replay_transport.request(
            captured[0], captured[1], captured[2], captured[3],
            timeout=captured[4],
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(
            runtime.authentication_health()["consumed_nonces"], 1,
        )

    def test_rotation_waits_for_an_inflight_authentication(self):
        runtime = self.open()
        first_client, _first_transport = self.http_client(runtime)
        second_credential = self.make_credential("2")
        second_client, _second_transport = self.http_client(
            runtime, second_credential,
        )
        entered = threading.Event()
        release = threading.Event()
        original = runtime.authentication._verifier

        def blocking_verifier(request):
            entered.set()
            release.wait(2)
            return original(request)

        runtime.authentication._verifier = blocking_verifier
        with ThreadPoolExecutor(max_workers=2) as pool:
            inflight = pool.submit(first_client.capabilities)
            self.assertTrue(entered.wait(1))
            rotation = pool.submit(
                runtime.replace_credentials, (second_credential,),
            )
            time.sleep(0.02)
            self.assertFalse(rotation.done())
            release.set()
            self.assertEqual(
                inflight.result()["capabilities"]["sha256"],
                self.sidecar_digest,
            )
            self.assertIsNone(rotation.result())

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked):
            first_client.capabilities()
        self.assertEqual(
            second_client.capabilities()["capabilities"]["sha256"],
            self.sidecar_digest,
        )

    def test_rotation_obeys_storage_process_and_lifecycle_guards(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        second_credential = self.make_credential("2")
        os.chmod(self.replay, 0o644)

        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked) as unsafe:
            runtime.replace_credentials((second_credential,))
        self.assertEqual(
            unsafe.exception.code,
            "source_delivery_hmac_runtime.database_unsafe",
        )
        os.chmod(self.replay, 0o600)
        self.assertEqual(
            client.capabilities()["capabilities"]["sha256"],
            self.sidecar_digest,
        )

        runtime.authentication._owner_pid -= 1
        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked) as foreign:
            runtime.replace_credentials((second_credential,))
        self.assertEqual(
            foreign.exception.code,
            "source_delivery_hmac_runtime.foreign_process",
        )
        runtime.authentication._owner_pid = os.getpid()
        runtime.close()
        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked) as closed:
            runtime.replace_credentials((second_credential,))
        self.assertEqual(
            closed.exception.code,
            "source_delivery_hmac_runtime.closed",
        )

    def test_client_and_server_rotate_without_restarting_the_worker(self):
        runtime = self.open()
        rotating = self.rotating_signer()
        transport = WSGITransport(runtime.http)
        client = CLIENT.CMSSourceDeliverySidecarHTTPClient(
            "https://delivery.example",
            self.sidecar_digest,
            self.remote_digest,
            rotating,
            transport=transport,
        )
        second_credential = self.make_credential("2")
        worker = runtime.delivery._worker_thread

        client.capabilities()
        runtime.replace_credentials((self.credential, second_credential))
        rotating.replace_credential(second_credential)
        self.assertIs(runtime.delivery._worker_thread, worker)
        runtime.replace_credentials((second_credential,))

        self.assertEqual(
            client.capabilities()["capabilities"]["sha256"],
            self.sidecar_digest,
        )
        self.assertIs(runtime.delivery._worker_thread, worker)

    def test_failed_client_rotation_keeps_the_last_valid_signer(self):
        runtime = self.open()
        rotating = self.rotating_signer()
        transport = WSGITransport(runtime.http)
        client = CLIENT.CMSSourceDeliverySidecarHTTPClient(
            "https://delivery.example",
            self.sidecar_digest,
            self.remote_digest,
            rotating,
            transport=transport,
        )

        with self.assertRaises(
            AUTH.SourceDeliveryHMACAuthenticationUnavailable,
        ) as caught:
            rotating.replace_credential(object())

        self.assertEqual(
            caught.exception.code,
            "source_delivery_hmac.signer_configuration_invalid",
        )
        self.assertEqual(
            client.capabilities()["capabilities"]["sha256"],
            self.sidecar_digest,
        )

    def test_client_rotation_waits_for_inflight_proof_generation(self):
        runtime = self.open()
        second_credential = self.make_credential("2")
        runtime.replace_credentials((self.credential, second_credential))
        entered = threading.Event()
        release = threading.Event()
        generated = Nonces()

        def blocking_nonce():
            entered.set()
            release.wait(2)
            return generated()

        rotating = self.rotating_signer(nonce_factory=blocking_nonce)
        transport = WSGITransport(runtime.http)
        client = CLIENT.CMSSourceDeliverySidecarHTTPClient(
            "https://delivery.example",
            self.sidecar_digest,
            self.remote_digest,
            rotating,
            transport=transport,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            inflight = pool.submit(client.capabilities)
            self.assertTrue(entered.wait(1))
            rotation = pool.submit(
                rotating.replace_credential, second_credential,
            )
            time.sleep(0.02)
            self.assertFalse(rotation.done())
            release.set()
            self.assertEqual(
                inflight.result()["capabilities"]["sha256"],
                self.sidecar_digest,
            )
            self.assertIsNone(rotation.result())

        runtime.replace_credentials((second_credential,))
        self.assertEqual(
            client.capabilities()["capabilities"]["sha256"],
            self.sidecar_digest,
        )

    def test_rotating_client_signer_is_process_bound_and_secret_free(self):
        rotating = self.rotating_signer()
        rendered = repr(rotating)

        self.assertNotIn(self.credential.secret.hex(), rendered)
        self.assertNotIn("site-1", rendered)
        self.assertIs(
            RUNTIME.RotatingSourceDeliveryHMACSigner,
            AUTH.RotatingSourceDeliveryHMACSigner,
        )
        rotating._owner_pid -= 1
        with self.assertRaises(
            AUTH.SourceDeliveryHMACAuthenticationUnavailable,
        ) as caught:
            rotating.replace_credential(self.make_credential("2"))
        self.assertEqual(
            caught.exception.code,
            "source_delivery_hmac.signer_foreign_process",
        )

    def test_invalid_configuration_creates_neither_database(self):
        cases = (
            {"origin": "http://delivery.example"},
            {"worker_id": "not valid"},
            {"idle_delay_seconds": 0},
            {"sidecar_capabilities_sha256": "not-a-hash"},
            {"allow_loopback_http": 1},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(
                RUNTIME.SourceDeliveryHMACRuntimeBlocked,
            ):
                self.open(**overrides)
            self.assertFalse(self.outbox.exists())
            self.assertFalse(self.replay.exists())

        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked) as caught:
            RUNTIME.open_hosted_hmac_authenticated_cms_source_delivery(
                self.outbox,
                self.outbox,
                self.remote_client,
                (self.credential,),
                worker_id="website-source-worker",
                origin="https://delivery.example",
                sidecar_capabilities_sha256=self.sidecar_digest,
                remote_capabilities_sha256=self.remote_digest,
            )
        self.assertEqual(
            caught.exception.code,
            "source_delivery_hmac_runtime.database_paths_conflict",
        )
        self.assertFalse(self.outbox.exists())

    def test_permission_drift_blocks_authentication_before_runtime_access(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        os.chmod(self.replay, 0o644)

        auth_health = runtime.authentication_health()

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            client.submit_change(cms_support.event())

        self.assertEqual(auth_health["status"], "blocked")
        self.assertEqual(
            auth_health["error_code"],
            "source_delivery_hmac_runtime.database_unsafe",
        )
        self.assertIsNone(auth_health["consumed_nonces"])
        self.assertEqual(caught.exception.code, "source_delivery_client.http_status")
        self.assertTrue(caught.exception.retryable)
        count = runtime.delivery._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_database_replacement_blocks_before_nonce_or_outbox_write(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        replacement = Path(self.directory.name) / "replacement.sqlite3"
        os.replace(self.replay, replacement)
        self.replay.touch(mode=0o600)
        os.chmod(self.replay, 0o600)

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            client.submit_change(cms_support.event())

        self.assertTrue(caught.exception.retryable)
        self.assertEqual(runtime.authentication.state, "open")
        count = runtime.delivery._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        os.replace(replacement, self.replay)

    def test_links_and_unsafe_existing_files_are_rejected(self):
        target = Path(self.directory.name) / "target.sqlite3"
        target.touch(mode=0o600)
        os.chmod(target, 0o600)
        self.replay.symlink_to(target)
        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked):
            RUNTIME.open_durable_source_delivery_hmac_runtime(
                self.replay,
                (self.credential,),
                origin="https://delivery.example",
                sidecar_capabilities_sha256=self.sidecar_digest,
                remote_capabilities_sha256=self.remote_digest,
                clock=lambda: self.now,
            )
        self.replay.unlink()

        os.link(target, self.replay)
        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked):
            RUNTIME.open_durable_source_delivery_hmac_runtime(
                self.replay,
                (self.credential,),
                origin="https://delivery.example",
                sidecar_capabilities_sha256=self.sidecar_digest,
                remote_capabilities_sha256=self.remote_digest,
                clock=lambda: self.now,
            )
        self.assertFalse(self.outbox.exists())

    def test_storage_alias_is_rejected_before_worker_start(self):
        original_start = (
            RUNTIME._DELIVERY_RUNTIME.DurableCMSSourceDeliveryRuntime.start_worker
        )
        starts = []

        def recording_start(runtime, **kwargs):
            starts.append(kwargs)
            return original_start(runtime, **kwargs)

        with mock.patch.object(
            RUNTIME,
            "_same_database_identity",
            return_value=True,
        ), mock.patch.object(
            RUNTIME._DELIVERY_RUNTIME.DurableCMSSourceDeliveryRuntime,
            "start_worker",
            recording_start,
        ), self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked) as caught:
            self.open()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_hmac_runtime.database_paths_conflict",
        )
        self.assertEqual(starts, [])
        for path in (self.outbox, self.replay):
            connection = sqlite3.connect(path)
            connection.close()

    def test_foreign_process_and_closed_runtime_fail_as_retryable_http(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        runtime.authentication._owner_pid -= 1

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as foreign:
            client.health()
        self.assertTrue(foreign.exception.retryable)
        self.assertEqual(runtime.state, "foreign-process")

        runtime.authentication._owner_pid = os.getpid()
        runtime.close()
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as closed:
            client.health()
        self.assertTrue(closed.exception.retryable)
        self.assertEqual(runtime.state, "closed")

    def test_corrupt_replay_ledger_blocks_restart_without_outbox_mutation(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        client.capabilities()
        runtime.close()
        connection = sqlite3.connect(self.replay)
        connection.execute(
            "UPDATE cms_source_delivery_hmac_nonces "
            "SET proof_sha256 = 'invalid'"
        )
        connection.commit()
        connection.close()

        with self.assertRaises(RUNTIME.SourceDeliveryHMACRuntimeBlocked) as caught:
            self.open()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_hmac_runtime.initialization_failed",
        )
        connection = sqlite3.connect(self.outbox)
        count = connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_parallel_authenticated_replays_converge_on_one_outbox_item(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        change = cms_support.event()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _index: client.submit_change(change), range(16),
            ))

        self.assertTrue(all(result == results[0] for result in results))
        count = runtime.delivery._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(
            runtime.authentication_health()["consumed_nonces"], 32,
        )

    def test_two_connections_atomically_consume_one_exact_nonce(self):
        options = {
            "origin": "https://delivery.example",
            "sidecar_capabilities_sha256": self.sidecar_digest,
            "remote_capabilities_sha256": self.remote_digest,
            "clock": lambda: self.now,
        }
        first = RUNTIME.open_durable_source_delivery_hmac_runtime(
            self.replay, (self.credential,), **options,
        )
        second = RUNTIME.open_durable_source_delivery_hmac_runtime(
            self.replay, (self.credential,), **options,
        )
        signer = AUTH.SourceDeliveryHMACSigner(
            self.credential,
            nonce_factory=lambda: "shared-nonce-value-00000000000000",
            **options,
        )
        body_sha256 = hashlib.sha256(b"").hexdigest()
        headers = signer({
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "GET",
            "origin": options["origin"],
            "path": HTTP.CAPABILITIES_PATH,
            "scope": HTTP.SCOPES[HTTP.CAPABILITIES_PATH],
            "body_sha256": body_sha256,
        })
        request = {
            "schema": HTTP.AUTH_REQUEST_SCHEMA,
            "method": "GET",
            "path": HTTP.CAPABILITIES_PATH,
            "headers": [[name.lower(), value] for name, value in headers.items()],
            "body_sha256": body_sha256,
        }
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda runtime: runtime(request), (
                    first, second,
                )))
            self.assertEqual(sum(outcome is not None for outcome in outcomes), 1)
            self.assertEqual(first.health()["consumed_nonces"], 1)
            self.assertEqual(second.health()["consumed_nonces"], 1)
        finally:
            first.close()
            second.close()

    def test_shutdown_timeout_keeps_authentication_open_until_worker_stops(self):
        blocking = BlockingClient()
        runtime = self.open(
            remote_client=blocking,
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
            blocked_delay_seconds=0.01,
        )
        runtime.enqueue_change(cms_support.event())
        self.assertTrue(blocking.entered.wait(2))

        with self.assertRaises(
            RUNTIME._DELIVERY_RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked,
        ) as caught:
            runtime.close(worker_timeout_seconds=0.01)

        self.assertEqual(
            caught.exception.code,
            "source_delivery_runtime.worker_stop_timeout",
        )
        self.assertEqual(runtime.authentication.state, "open")
        blocking.release.set()
        deadline = time.monotonic() + 2
        while runtime.delivery.worker_state != "stopped":
            if time.monotonic() >= deadline:
                self.fail("delivery worker did not stop")
            time.sleep(0.01)
        runtime.close(worker_timeout_seconds=1)
        self.assertEqual(runtime.state, "closed")

    def test_health_and_representations_are_content_and_secret_free(self):
        runtime = self.open()
        client, _transport = self.http_client(runtime)
        client.capabilities()
        rendered = repr(runtime) + repr(runtime.authentication)
        health = runtime.authentication_health()
        serialized = repr(health)

        self.assertNotIn(self.credential.secret.hex(), rendered)
        self.assertNotIn(str(self.replay), rendered)
        self.assertNotIn("site-1", serialized)
        self.assertNotIn("website-credential", serialized)
        self.assertIs(RUNTIME.HMACCredential, AUTH.HMACCredential)
        self.assertEqual(
            set(health),
            {
                "schema", "status", "runtime_state", "consumed_nonces",
                "error_code",
            },
        )


if __name__ == "__main__":
    unittest.main()
