from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from tests import test_website_localization_cms_client as cms_support
from tests import test_website_localization_cms_source_delivery as delivery_support
from tests import test_website_localization_cms_source_delivery_http as http_support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AUTH = load(
    "blun_test_website_localization_cms_source_delivery_auth",
    ROOT / "integrations" / "website_localization_cms_source_delivery_auth.py",
)
CLIENT = AUTH._CLIENT
HTTP = AUTH._HTTP
RUNTIME = http_support.RUNTIME


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


class TransformingTransport:
    def __init__(self, transport, transform):
        self.transport = transport
        self.transform = transform
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        transformed = self.transform(
            method, url, dict(headers), body, timeout,
        )
        self.calls.append(transformed)
        return self.transport.request(
            transformed[0], transformed[1], transformed[2], transformed[3],
            timeout=transformed[4],
        )


class RecordingVerifier:
    def __init__(self, verifier):
        self.verifier = verifier
        self.requests = []

    def __call__(self, request):
        self.requests.append(copy.deepcopy(request))
        return self.verifier(request)


class SourceDeliveryHMACAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "delivery.sqlite3"
        self.remote_client = delivery_support.ScriptedClient()
        self.sidecar_digest = HTTP._capabilities_payload()["sha256"]
        self.remote_digest = self.remote_client.expected_capabilities_sha256
        self.replay_connection = sqlite3.connect(
            ":memory:", check_same_thread=False,
        )
        self.replay_store = AUTH.DurableHMACReplayStore(
            self.replay_connection,
        )
        self.nonces = Nonces()
        self.credential = self.make_credential("1")
        self.signer = self.make_signer(self.credential)
        self.verifier = self.make_verifier((self.credential,))
        self.recording_verifier = RecordingVerifier(self.verifier)
        self.runtime = RUNTIME.open_hosted_cms_source_delivery(
            self.database,
            self.remote_client,
            worker_id="website-source-worker",
            clock=lambda: self.now,
            lease_seconds=60,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
            http_authenticator=self.recording_verifier,
        )
        self.transport = WSGITransport(self.runtime.http)
        self.client = self.make_client(self.signer)

    def tearDown(self):
        self.runtime._owner_pid = os.getpid()
        if self.database.exists() and not self.database.is_symlink():
            os.chmod(self.database, 0o600)
        try:
            self.runtime.close(worker_timeout_seconds=1)
        except Exception:
            pass
        self.replay_connection.close()
        self.directory.cleanup()

    def make_credential(self, version, *, secret=None, scopes=None, site="site-1"):
        return AUTH.HMACCredential(
            principal_id="website-backend",
            credential_id="website-credential",
            credential_version=version,
            secret=(bytes([int(version)]) * 32 if secret is None else secret),
            scopes=(
                tuple(sorted(HTTP.SCOPES.values()))
                if scopes is None else scopes
            ),
            site_id=site,
        )

    def make_signer(self, credential, **overrides):
        options = {
            "origin": "https://delivery.example",
            "sidecar_capabilities_sha256": self.sidecar_digest,
            "remote_capabilities_sha256": self.remote_digest,
            "clock": lambda: self.now,
            "nonce_factory": self.nonces,
        }
        options.update(overrides)
        return AUTH.SourceDeliveryHMACSigner(credential, **options)

    def make_verifier(self, credentials, **overrides):
        options = {
            "origin": "https://delivery.example",
            "sidecar_capabilities_sha256": self.sidecar_digest,
            "remote_capabilities_sha256": self.remote_digest,
            "clock": lambda: self.now,
        }
        options.update(overrides)
        return AUTH.SourceDeliveryHMACVerifier(
            credentials, self.replay_store, **options,
        )

    def make_client(self, signer, *, transport=None):
        return CLIENT.CMSSourceDeliverySidecarHTTPClient(
            "https://delivery.example",
            self.sidecar_digest,
            self.remote_digest,
            signer,
            transport=self.transport if transport is None else transport,
        )

    @staticmethod
    def payload_hash(payload):
        return hashlib.sha256(CLIENT._canonical(payload)).hexdigest()

    def test_end_to_end_change_removal_status_health_and_readiness(self):
        capabilities = self.client.capabilities()
        change = cms_support.event()
        queued = self.client.submit_change(
            change, source_max_attempts=4, delivery_max_attempts=3,
        )["queue"]
        status = self.client.status(
            queued["operation"], queued["request_id"], queued["event_id"],
            queued["site_id"], queued["payload_sha256"],
        )["status"]
        cancellation = cms_support.cancellation()
        removed = self.client.submit_removal(cancellation)["queue"]
        health = self.client.health()["health"]
        readiness = self.client.readiness()["readiness"]

        self.assertEqual(capabilities["capabilities"]["sha256"], self.sidecar_digest)
        self.assertEqual(status, queued)
        self.assertEqual(removed["operation"], "cancellation")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(
            self.replay_store.health()["consumed_nonces"],
            len(self.transport.calls),
        )

    def test_proof_binds_both_contracts_and_all_tenant_identities(self):
        change = cms_support.event()
        self.client.submit_change(change)
        request = self.recording_verifier.requests[-1]
        headers = dict(request["headers"])

        self.assertEqual(headers[AUTH.HEADER_SITE_ID.lower()], change["site_id"])
        self.assertEqual(headers[AUTH.HEADER_EVENT_ID.lower()], change["event_id"])
        self.assertEqual(headers[AUTH.HEADER_REQUEST_ID.lower()], change["event_id"])
        self.assertEqual(
            headers[AUTH.HEADER_PAYLOAD_SHA256.lower()],
            self.payload_hash(change),
        )
        self.assertEqual(headers["idempotency-key"], change["event_id"])
        self.assertEqual(
            headers["x-localization-source-payload-sha256"],
            self.payload_hash(change),
        )

        mismatched = self.make_verifier(
            (self.credential,), remote_capabilities_sha256="f" * 64,
        )
        unsigned_replay = copy.deepcopy(request)
        self.assertIsNone(mismatched(unsigned_replay))

    def test_exact_proof_replay_is_rejected_before_runtime_access(self):
        self.client.capabilities()
        first = self.transport.calls[0]
        before = len(self.recording_verifier.requests)

        replay = self.transport.request(
            first[0], first[1], first[2], first[3], timeout=first[4],
        )

        self.assertEqual(replay.status, 401)
        self.assertEqual(len(self.recording_verifier.requests), before + 1)
        self.assertEqual(self.replay_store.health()["consumed_nonces"], 1)

    def test_tampered_route_body_and_write_bindings_fail_closed(self):
        def alter_idempotency(method, url, headers, body, timeout):
            if url.endswith(HTTP.CHANGE_PATH):
                headers["Idempotency-Key"] = "another-request"
            return method, url, headers, body, timeout

        transport = TransformingTransport(self.transport, alter_idempotency)
        client = self.make_client(self.signer, transport=transport)
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            client.submit_change(cms_support.event())
        self.assertEqual(caught.exception.code, "source_delivery_client.http_status")
        self.assertFalse(caught.exception.retryable)

        self.client.capabilities()
        first = self.transport.calls[-1]
        changed_url = first[1].replace(
            HTTP.CAPABILITIES_PATH, HTTP.HEALTH_PATH,
        )
        altered = self.transport.request(
            first[0], changed_url, first[2], b"changed", timeout=first[4],
        )
        self.assertEqual(altered.status, 400)

    def test_expired_future_and_wrong_key_proofs_are_rejected(self):
        old = self.make_signer(self.credential, clock=lambda: self.now - 61)
        future = self.make_signer(self.credential, clock=lambda: self.now + 6)
        wrong = self.make_signer(
            self.make_credential("1", secret=b"z" * 32),
        )

        for signer in (old, future, wrong):
            with self.subTest(signer=signer), self.assertRaises(
                CLIENT.CMSSourceDeliveryClientBlocked,
            ) as caught:
                self.make_client(signer).capabilities()
            self.assertEqual(
                caught.exception.code, "source_delivery_client.http_status",
            )
            self.assertFalse(caught.exception.retryable)

    def test_rotation_accepts_explicit_generations_and_rejects_retired_one(self):
        second = self.make_credential("2")
        rotated = self.make_verifier((self.credential, second))
        self.runtime.http.authenticator = rotated
        first_client = self.make_client(self.make_signer(self.credential))
        second_client = self.make_client(self.make_signer(second))

        self.assertEqual(first_client.health()["health"]["status"], "ok")
        self.assertEqual(second_client.health()["health"]["status"], "ok")

        self.runtime.http.authenticator = self.make_verifier((second,))
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked):
            first_client.health()
        self.assertEqual(second_client.health()["health"]["status"], "ok")

    def test_scope_and_site_are_enforced_before_the_protected_write(self):
        limited = self.make_credential(
            "3", scopes=(HTTP.SCOPES[HTTP.CAPABILITIES_PATH],), site=None,
        )
        limited_signer = self.make_signer(limited)
        limited_verifier = self.make_verifier((limited,))
        self.runtime.http.authenticator = limited_verifier
        client = self.make_client(limited_signer)

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            client.submit_change(cms_support.event())
        self.assertEqual(caught.exception.code, "source_delivery_client.authentication")

        wrong_site = self.make_credential("4", site="site-2")
        wrong_signer = self.make_signer(wrong_site)
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked):
            self.make_client(wrong_signer).submit_change(cms_support.event())
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_replay_store_tampering_makes_authentication_unavailable(self):
        self.client.capabilities()
        self.replay_connection.execute("""
            UPDATE cms_source_delivery_hmac_nonces
            SET proof_sha256 = 'not-a-hash'
        """)
        self.replay_connection.commit()

        with self.assertRaises(
            AUTH.SourceDeliveryHMACAuthenticationUnavailable,
        ) as direct:
            self.replay_store.health()
        self.assertEqual(
            direct.exception.code,
            "source_delivery_hmac.replay_store_integrity",
        )
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            self.client.health()
        self.assertEqual(caught.exception.code, "source_delivery_client.http_status")
        self.assertTrue(caught.exception.retryable)

    def test_clock_outage_is_retryable_without_leaking_private_details(self):
        private = "private clock path /srv/customer/site-1"

        def broken_clock():
            raise RuntimeError(private)

        self.runtime.http.authenticator = self.make_verifier(
            (self.credential,), clock=broken_clock,
        )
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            self.client.health()
        self.assertEqual(caught.exception.code, "source_delivery_client.http_status")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn(private, str(caught.exception))

    def test_invalid_credentials_and_time_windows_fail_during_preflight(self):
        with self.assertRaises(ValueError):
            self.make_credential("5", secret=b"weak")
        with self.assertRaises(ValueError):
            self.make_credential(
                "5", scopes=(HTTP.SCOPES[HTTP.STATUS_PATH],), site=None,
            )
        with self.assertRaises(ValueError):
            self.make_verifier((self.credential, self.credential))
        with self.assertRaises(ValueError):
            self.make_verifier((self.credential,), max_age_seconds=0)
        with self.assertRaises(ValueError):
            self.make_verifier(
                (self.credential,), max_age_seconds=10, future_skew_seconds=11,
            )
        self.assertNotIn(self.credential.secret.hex(), repr(self.credential))
        self.assertNotIn(self.credential.secret.hex(), repr(self.signer))
        self.assertNotIn(self.credential.secret.hex(), repr(self.verifier))

    def test_concurrent_exact_submissions_are_authenticated_and_idempotent(self):
        change = cms_support.event()
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(
                lambda _index: self.client.submit_change(change), range(16),
            ))

        self.assertTrue(all(response == responses[0] for response in responses))
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(
            self.replay_store.health()["consumed_nonces"], 32,
        )


if __name__ == "__main__":
    unittest.main()
