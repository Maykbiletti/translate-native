from __future__ import annotations

import copy
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

    def test_submission_lifecycle_reaches_source_without_collapsing_stages(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(change, source_max_attempts=3)

        local = runtime.submission_lifecycle("change", change["event_id"])
        self.assertEqual((local.status, local.stage), (
            "pending", "website_acceptance",
        ))
        self.assertIsNone(local.source_status)
        self.assertEqual(len(self.transport.calls), 0)

        runtime.run_once()
        sidecar = runtime.submission_lifecycle("change", change["event_id"])
        self.assertEqual((sidecar.status, sidecar.stage), (
            "pending", "sidecar_delivery",
        ))
        self.assertIsNone(sidecar.source_status)

        self.sidecar.delivery.run_once()
        lifecycle = runtime.submission_lifecycle(
            "change", change["event_id"],
        )

        self.assertEqual((lifecycle.status, lifecycle.stage), (
            "processing", "localization_lifecycle",
        ))
        self.assertEqual(lifecycle.source_status["required_locales"], [
            "fi-FI", "mt-MT",
        ])
        self.assertEqual(lifecycle.submission["status"], "accepted")
        self.assertEqual(
            lifecycle.source_capability_binding,
            self.remote.capability_binding(),
        )
        payload = lifecycle.as_payload()
        self.assertEqual(payload["source_status"]["remote_status"], "processing")
        self.assertNotIn(
            change["localization"]["source_text"], repr(payload),
        )

    def test_submission_lifecycle_rejects_tampered_source_state(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(change)
        runtime.run_once()
        self.sidecar.delivery.run_once()
        original = self.remote.status

        def tampered(event_id, site_id):
            response = copy.deepcopy(original(event_id, site_id))
            response["status"]["site_id"] = "other-site"
            return response

        self.remote.status = tampered
        with self.assertRaisesRegex(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
            "source_delivery_submission_runtime.lifecycle_unavailable",
        ):
            runtime.submission_lifecycle("change", change["event_id"])

    def test_submission_health_requires_both_outboxes_to_be_healthy(self):
        runtime = self.open()

        health = runtime.submission_health()

        self.assertEqual(health.status, "ok")
        self.assertEqual(health.website_health["status"], "ok")
        self.assertEqual(health.sidecar_health["status"], "ok")
        self.assertEqual(
            health.sidecar_capabilities_sha256, self.sidecar_digest,
        )
        self.assertEqual(
            health.source_capabilities_sha256, self.remote_digest,
        )
        payload = health.as_payload()
        self.assertEqual(set(payload), {
            "schema", "status", "website_health", "sidecar_health",
            "sidecar_capabilities_sha256", "source_capabilities_sha256",
        })
        self.assertNotIn("delivery.example", repr(payload))
        self.assertNotIn("website-credential", repr(payload))

    def test_submission_health_keeps_local_failure_separate(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(change, delivery_max_attempts=1)
        original = self.transport.request

        def fail_once(*_args, **_kwargs):
            self.transport.request = original
            raise SUBMISSION._AUTH._CLIENT.CMSSourceDeliveryClientBlocked(
                "source_delivery_client.network", retryable=True,
            )

        self.transport.request = fail_once
        failed = runtime.run_once()

        health = runtime.submission_health()

        self.assertEqual(failed.status, "failed")
        self.assertEqual(health.status, "blocked")
        self.assertEqual(health.website_health["status"], "blocked")
        self.assertEqual(
            health.website_health["error_code"], "source_delivery.failed",
        )
        self.assertEqual(health.sidecar_health["status"], "ok")

    def test_submission_health_keeps_sidecar_failure_separate(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(change)
        runtime.run_once()
        self.remote.failures.append(delivery_support.ClientFailure(
            "source_client.denied", retryable=False,
        ))
        failed = self.sidecar.delivery.run_once()

        health = runtime.submission_health()

        self.assertEqual(failed.status, "failed")
        self.assertEqual(health.status, "blocked")
        self.assertEqual(health.website_health["status"], "ok")
        self.assertEqual(health.sidecar_health["status"], "blocked")
        self.assertEqual(
            health.sidecar_health["error_code"], "source_delivery.failed",
        )

    def test_submission_health_rejects_malformed_local_state_offline(self):
        runtime = self.open()
        runtime._delivery.health = lambda: {"status": "ok"}
        before = len(self.transport.calls)

        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as caught:
            runtime.submission_health()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_runtime.health_invalid",
        )
        self.assertEqual(len(self.transport.calls), before)

    def test_submission_health_rejects_malformed_remote_state(self):
        runtime = self.open()
        response = runtime.sidecar_health()
        altered = copy.deepcopy(response)
        altered["health"]["failed"] = 1
        runtime._client.health = lambda: altered

        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as caught:
            runtime.submission_health()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_runtime.health_invalid",
        )

    def test_submission_health_rejects_sidecar_capability_substitution(self):
        runtime = self.open()
        response = runtime.sidecar_health()
        altered = copy.deepcopy(response)
        altered["capabilities_sha256"] = "f" * 64
        runtime._client.health = lambda: altered

        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as caught:
            runtime.submission_health()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_runtime.health_invalid",
        )

    def test_pipeline_health_covers_intake_and_source_queues(self):
        runtime = self.open()

        health = runtime.submission_pipeline_health()

        self.assertEqual(health.status, "ok")
        self.assertEqual(health.intake_health["status"], "ok")
        self.assertEqual(health.source_health["status"], "ok")
        self.assertEqual(
            health.source_capability_binding,
            self.remote.capability_binding(),
        )
        self.assertEqual(
            health.sidecar_capabilities_sha256, self.sidecar_digest,
        )
        self.assertEqual(
            health.source_capabilities_sha256, self.remote_digest,
        )
        payload = health.as_payload()
        self.assertEqual(set(payload), {
            "schema", "status", "intake_health", "source_health",
            "source_capability_binding",
            "sidecar_capabilities_sha256", "source_capabilities_sha256",
        })
        self.assertNotIn("delivery.example", repr(payload))
        self.assertNotIn("website-credential", repr(payload))

        original = self.remote.health

        def degraded():
            response = original()
            response["health"].update({
                "status": "degraded",
                "pending_lifecycle_registrations": 1,
                "error_code": "source_service.lifecycle_registration_pending",
            })
            return response

        self.remote.health = degraded
        degraded_health = runtime.submission_pipeline_health()
        self.assertEqual(degraded_health.status, "degraded")
        self.assertEqual(degraded_health.intake_health["status"], "ok")
        self.assertEqual(degraded_health.source_health["status"], "degraded")

        def blocked():
            response = original()
            response["health"].update({
                "status": "blocked",
                "error_code": "source_service.component_blocked",
            })
            response["health"]["changes"]["status"] = "blocked"
            return response

        self.remote.health = blocked
        blocked_health = runtime.submission_pipeline_health()
        self.assertEqual(blocked_health.status, "blocked")
        self.assertEqual(blocked_health.intake_health["status"], "ok")
        self.assertEqual(blocked_health.source_health["status"], "blocked")

    def test_pipeline_health_stops_before_source_when_intake_is_blocked(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(change, delivery_max_attempts=1)
        original = self.transport.request

        def fail_once(*_args, **_kwargs):
            self.transport.request = original
            raise SUBMISSION._AUTH._CLIENT.CMSSourceDeliveryClientBlocked(
                "source_delivery_client.network", retryable=True,
            )

        self.transport.request = fail_once
        self.assertEqual(runtime.run_once().status, "failed")
        self.remote.calls.clear()

        health = runtime.submission_pipeline_health()

        self.assertEqual(health.status, "blocked")
        self.assertEqual(health.intake_health["status"], "blocked")
        self.assertIsNone(health.source_health)
        self.assertNotIn(("health",), self.remote.calls)

    def test_submission_readiness_requires_both_owned_workers(self):
        runtime = self.open(
            hosted=True,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
        )

        readiness = runtime.submission_readiness()

        self.assertEqual(readiness.status, "ready")
        self.assertEqual((
            readiness.website_status,
            readiness.website_worker_state,
            readiness.website_outbox_status,
            readiness.sidecar_status,
            readiness.sidecar_worker_state,
            readiness.sidecar_outbox_status,
        ), ("ready", "running", "ok", "ready", "running", "ok"))
        self.assertEqual(
            readiness.sidecar_capabilities_sha256, self.sidecar_digest,
        )
        self.assertEqual(
            readiness.source_capabilities_sha256, self.remote_digest,
        )
        payload = readiness.as_payload()
        self.assertEqual(set(payload), {
            "schema", "status", "website_status", "website_worker_state",
            "website_outbox_status", "website_error_code", "sidecar_status",
            "sidecar_worker_state", "sidecar_outbox_status",
            "sidecar_error_code", "sidecar_capabilities_sha256",
            "source_capabilities_sha256",
        })
        self.assertNotIn("delivery.example", repr(payload))
        self.assertNotIn("website-credential", repr(payload))

    def test_submission_readiness_stays_local_when_website_is_not_ready(self):
        runtime = self.open()
        before = len(self.transport.calls)

        readiness = runtime.submission_readiness()

        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual((
            readiness.status,
            readiness.website_status,
            readiness.website_worker_state,
            readiness.sidecar_status,
        ), ("not_ready", "not_ready", "unmanaged", None))
        self.assertEqual(
            readiness.website_error_code,
            "source_delivery_runtime.worker_not_ready",
        )

    def test_submission_readiness_surfaces_sidecar_worker_failure(self):
        runtime = self.open(
            hosted=True,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
        )
        self.sidecar.delivery.stop_worker(timeout_seconds=1)

        readiness = runtime.submission_readiness()

        self.assertEqual((
            readiness.status,
            readiness.website_status,
            readiness.sidecar_status,
            readiness.sidecar_worker_state,
        ), ("not_ready", "ready", "not_ready", "stopped"))
        self.assertEqual(
            readiness.sidecar_error_code,
            "source_delivery_runtime.worker_not_ready",
        )

    def test_submission_readiness_rejects_malformed_local_state_offline(self):
        runtime = self.open()
        runtime._delivery.worker_readiness = lambda: {
            "schema": "blun.cms-source-delivery-worker-readiness.v1",
            "status": "ready",
        }
        before = len(self.transport.calls)

        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as caught:
            runtime.submission_readiness()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_runtime.readiness_invalid",
        )
        self.assertEqual(len(self.transport.calls), before)

    def test_submission_readiness_rejects_malformed_remote_state(self):
        runtime = self.open(
            hosted=True,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
        )
        response = runtime.sidecar_readiness()
        altered = copy.deepcopy(response)
        altered["readiness"]["outbox_status"] = "blocked"
        runtime._client.readiness = lambda: altered

        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as caught:
            runtime.submission_readiness()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_runtime.readiness_invalid",
        )

    def test_pipeline_readiness_covers_intake_and_source_processing(self):
        runtime = self.open(
            hosted=True,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
        )

        readiness = runtime.submission_pipeline_readiness()

        self.assertEqual(readiness.status, "ready")
        self.assertEqual(readiness.intake_readiness["status"], "ready")
        self.assertEqual(readiness.source_readiness, {
            "schema": "blun.cms-source-worker-readiness.v1",
            "status": "ready",
            "worker_state": "running",
            "service_status": "ok",
            "error_code": None,
        })
        self.assertEqual(
            readiness.source_capability_binding,
            self.remote.capability_binding(),
        )
        self.assertEqual(
            readiness.sidecar_capabilities_sha256, self.sidecar_digest,
        )
        self.assertEqual(
            readiness.source_capabilities_sha256, self.remote_digest,
        )
        payload = readiness.as_payload()
        self.assertEqual(set(payload), {
            "schema", "status", "intake_readiness", "source_readiness",
            "source_capability_binding",
            "sidecar_capabilities_sha256", "source_capabilities_sha256",
        })
        self.assertNotIn("delivery.example", repr(payload))
        self.assertNotIn("website-credential", repr(payload))

        def not_ready():
            return {
                "schema": delivery_support.DELIVERY._CLIENT._HTTP.READINESS_RESPONSE_SCHEMA,
                "readiness": {
                    "schema": "blun.cms-source-worker-readiness.v1",
                    "status": "not_ready",
                    "worker_state": "stopped",
                    "service_status": "blocked",
                    "error_code": "source_runtime.worker_not_ready",
                },
                "capabilities_sha256": self.remote_digest,
                "capability_binding": self.remote.capability_binding(),
            }

        self.remote.readiness = not_ready
        blocked = runtime.submission_pipeline_readiness()
        self.assertEqual(blocked.status, "not_ready")
        self.assertEqual(blocked.intake_readiness["status"], "ready")
        self.assertEqual(blocked.source_readiness["status"], "not_ready")
        self.assertEqual(
            blocked.source_readiness["error_code"],
            "source_runtime.worker_not_ready",
        )

    def test_pipeline_readiness_stays_local_when_intake_is_not_ready(self):
        runtime = self.open()
        before = len(self.transport.calls)

        readiness = runtime.submission_pipeline_readiness()

        self.assertEqual(readiness.status, "not_ready")
        self.assertEqual(readiness.intake_readiness["status"], "not_ready")
        self.assertIsNone(readiness.source_readiness)
        self.assertEqual(len(self.transport.calls), before)

    def test_submission_status_stays_local_until_sidecar_acceptance(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(
            change, source_max_attempts=3, delivery_max_attempts=2,
        )
        before = len(self.transport.calls)

        status = runtime.submission_status("change", change["event_id"])

        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual((status.status, status.stage), (
            "pending", "website_acceptance",
        ))
        self.assertEqual((
            status.website_status,
            status.website_delivery_max_attempts,
            status.sidecar_status,
            status.sidecar_delivery_max_attempts,
            status.source_max_attempts,
        ), ("pending", 2, None, 4, 3))
        payload = status.as_payload()
        self.assertEqual(set(payload), {
            "schema", "operation", "request_id", "event_id", "site_id",
            "payload_sha256", "status", "stage", "website_status",
            "website_attempts", "website_delivery_max_attempts",
            "sidecar_status", "sidecar_attempts",
            "sidecar_delivery_max_attempts", "source_max_attempts",
            "next_attempt_at", "lease_expired", "error_code",
        })
        self.assertNotIn(change["localization"]["source_text"], repr(payload))

    def test_submission_status_projects_exact_sidecar_pending_state(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(
            change, source_max_attempts=3, delivery_max_attempts=2,
        )
        runtime.run_once()

        status = runtime.submission_status("change", change["event_id"])

        self.assertEqual((status.status, status.stage), (
            "pending", "sidecar_delivery",
        ))
        self.assertEqual((
            status.website_status,
            status.website_delivery_max_attempts,
            status.sidecar_status,
            status.sidecar_delivery_max_attempts,
            status.source_max_attempts,
        ), ("succeeded", 2, "pending", 4, 3))
        self.assertIsNone(status.error_code)

    def test_submission_status_calls_source_acceptance_only_after_delivery(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(
            change, source_max_attempts=3, delivery_max_attempts=2,
        )
        runtime.run_once()
        delivered = self.sidecar.delivery.run_once()

        status = runtime.submission_status("change", change["event_id"])

        self.assertEqual(delivered.status, "succeeded")
        self.assertEqual((status.status, status.stage), (
            "accepted", "source_acceptance",
        ))
        self.assertEqual(status.sidecar_status, "succeeded")
        self.assertIsNone(status.error_code)

    def test_submission_status_preserves_distinct_removal_identities(self):
        runtime = self.open()
        removal = cms_support.cancellation()
        runtime.enqueue_removal(
            removal, source_max_attempts=3, delivery_max_attempts=2,
        )
        runtime.run_once()
        self.sidecar.delivery.run_once()

        status = runtime.submission_status(
            "cancellation", removal["cancellation_id"],
        )

        self.assertEqual((status.status, status.stage), (
            "accepted", "source_acceptance",
        ))
        self.assertEqual((status.operation, status.request_id, status.event_id), (
            "cancellation", removal["cancellation_id"], removal["event_id"],
        ))
        self.assertEqual(status.site_id, removal["site_id"])
        self.assertEqual(status.payload_sha256, self.payload_hash(removal))

    def test_submission_status_surfaces_terminal_sidecar_failure(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(change)
        runtime.run_once()
        self.remote.failures.append(delivery_support.ClientFailure(
            "source_client.denied", retryable=False,
        ))
        failed = self.sidecar.delivery.run_once()

        status = runtime.submission_status("change", change["event_id"])

        self.assertEqual(failed.status, "failed")
        self.assertEqual((status.status, status.stage), (
            "failed", "sidecar_delivery",
        ))
        self.assertEqual(status.sidecar_status, "failed")
        self.assertEqual(status.error_code, "source_client.denied")

    def test_submission_status_rejects_changed_retry_bindings(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(
            change, source_max_attempts=3, delivery_max_attempts=2,
        )
        runtime.run_once()
        response = runtime.sidecar_status(
            "change",
            change["event_id"],
            change["event_id"],
            change["site_id"],
            self.payload_hash(change),
        )
        altered = copy.deepcopy(response)
        altered["status"]["delivery_max_attempts"] = 5
        runtime._client.status = lambda *_args: altered

        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as caught:
            runtime.submission_status("change", change["event_id"])

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_runtime.status_invalid",
        )

    def test_stale_local_contract_blocks_before_sidecar_status_access(self):
        runtime = self.open()
        change = cms_support.event()
        runtime.enqueue_change(change)
        runtime.run_once()
        runtime._delivery._connection.execute(
            """
            UPDATE cms_source_delivery_outbox
            SET capabilities_sha256 = ?
            WHERE operation = 'change' AND request_id = ?
            """,
            ("f" * 64, change["event_id"]),
        )
        before = len(self.transport.calls)

        with self.assertRaises(
            SUBMISSION.HMACCMSSourceDeliverySubmissionRuntimeBlocked,
        ) as caught:
            runtime.submission_status("change", change["event_id"])

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_runtime.status_invalid",
        )
        self.assertEqual(len(self.transport.calls), before)

    def test_terminal_website_failure_never_reads_sidecar_status(self):
        transport = NoNetworkTransport()
        runtime = self.open(transport=transport)
        change = cms_support.event()
        runtime.enqueue_change(change, delivery_max_attempts=1)
        failed = runtime.run_once()
        before = len(transport.calls)

        status = runtime.submission_status("change", change["event_id"])

        self.assertEqual(failed.status, "failed")
        self.assertEqual(len(transport.calls), before)
        self.assertEqual((status.status, status.stage, status.sidecar_status), (
            "failed", "website_acceptance", None,
        ))
        self.assertEqual(status.error_code, "source_delivery_client.network")

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
