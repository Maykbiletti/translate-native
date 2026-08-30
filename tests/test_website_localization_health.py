from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HEALTH = load(
    "blun_test_website_localization_health",
    ROOT / "integrations" / "website_localization_health.py",
)
CMS = HEALTH._CMS
PLANNER = CMS._PLANNER
RELEASE = CMS._RELEASE
QUEUE = CMS._QUEUE
WORKER = RELEASE._WORKER


class CMSAuthority:
    def __init__(self, key):
        self.key = key

    def sign(self, payload):
        return CMS.CMSMessageSignature(
            "hmac-sha256-test",
            "cms-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


class ApprovalAuthority:
    def __init__(self, key=b"approval-key"):
        self.key = key

    def sign(self, payload):
        return RELEASE.ApprovalSignature(
            "hmac-sha256-test",
            "approval-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


class ReceiptVerifier:
    def verify(self, **values):
        return values["receipt"] == "quality-receipt"


class ProviderProbe:
    def __init__(self, mode="healthy"):
        self.mode = mode
        self.calls = []

    def check(self, **provider):
        self.calls.append(provider)
        if self.mode == "raise":
            raise RuntimeError("private provider diagnostic")
        response = {
            "schema": HEALTH.PROVIDER_HEALTH_SCHEMA,
            "provider": {
                "id": provider["provider_id"],
                "model_id": provider["model_id"],
                "model_version": provider["model_version"],
            },
            "status": "healthy",
        }
        if self.mode == "malformed":
            response["status"] = "probably-healthy"
        return response


class Publisher:
    def __init__(self):
        self.requests = []

    def publish(self, request):
        self.requests.append(request)
        return {
            "schema": CMS.ACK_SCHEMA,
            "delivery_id": request.delivery_id,
            "payload_sha256": request.payload_sha256,
            "status": "accepted",
        }


def event():
    return {
        "schema": CMS.CHANGE_SCHEMA,
        "event_id": "cms-event-184",
        "site_id": "blun-marketing",
        "website_version": "website-2026-08-29.1",
        "localization": {
            "source_id": "homepage.hero",
            "source_revision": "cms-184",
            "source_text": "Build your business with BLUN.",
            "source_locale": "en-IE",
            "content_type": "headline",
            "glossary_version": "blun-glossary-3",
            "policy_version": "native-web-1",
            "provider_id": "customer-llm",
            "model_id": "king",
            "model_version": "2026-08-29",
            "software_version": "6.43.0-dev",
            "target_locales": ["de-AT", "sv-SE"],
        },
    }


def completed_result(job, candidate):
    payload = job.as_payload()
    return {
        "schema": WORKER.RESULT_SCHEMA,
        "worker_schema": WORKER.WORKER_SCHEMA,
        "job_id": payload["job_id"],
        "source_sha256": payload["source"]["sha256"],
        "target_sha256": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
        "source_locale": payload["source"]["locale"],
        "target_locale": payload["target"]["locale"],
        "content_type": payload["content_type"],
        "glossary_version": payload["glossary_version"],
        "policy_version": payload["policy_version"],
        "provider": payload["provider"],
        "software_version": payload["software_version"],
        "candidate": candidate,
        "quality_passes": [
            {
                "phase": phase,
                "request_sha256": hashlib.sha256(f"request-{phase}".encode()).hexdigest(),
                "response_sha256": hashlib.sha256(f"response-{phase}".encode()).hexdigest(),
                "status": "PASS",
            }
            for phase in WORKER.PHASES
        ],
        "integrity": {
            "status": "PASS",
            "guard": "translate-native-structure-and-token-gate",
        },
        "human_review_required": False,
        "release_required": True,
    }


class WebsiteLocalizationHealthTests(unittest.TestCase):
    def setUp(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.release_store = RELEASE.LocalizationReleaseStore(
            self.release_connection,
            self.queue,
        )
        self.cms_connection = sqlite3.connect(":memory:")
        self.bridge = CMS.WebsiteLocalizationCMSBridge(
            self.cms_connection,
            self.queue,
            self.release_store,
        )
        self.monitor = HEALTH.LocalizationHealthMonitor(self.bridge)
        self.event_authority = CMSAuthority(b"event-key")
        self.publication_authority = CMSAuthority(b"publication-key")
        self.approval_authority = ApprovalAuthority()
        self.receipt_verifier = ReceiptVerifier()
        self.probe = ProviderProbe()

    def tearDown(self):
        self.cms_connection.close()
        self.release_connection.close()
        self.queue_connection.close()

    def ingest(self):
        current = event()
        signature = self.event_authority.sign(
            CMS._canonical_json(current).encode("utf-8")
        )
        self.bridge.ingest_change(
            current,
            signature,
            self.event_authority,
            now=100,
        )
        return PLANNER.plan_from_mapping(current["localization"])

    def complete(self, plan, ttl=1000):
        translations = {
            "de-AT": "Bring dein Unternehmen mit BLUN voran.",
            "sv-SE": "Ta ditt företag vidare med BLUN.",
        }
        for index in range(len(plan.jobs)):
            claim = self.queue.claim(
                "translation-worker", now=110 + index, lease_seconds=20,
            )
            job = next(item for item in plan.jobs if item.job_id == claim.job_id)
            self.queue.complete(
                claim,
                completed_result(job, translations[claim.target_locale]),
                now=111 + index,
            )
            self.release_store.approve(
                plan,
                job.job_id,
                "quality-receipt",
                self.receipt_verifier,
                self.approval_authority,
                now=200,
                ttl_seconds=ttl,
            )

    def report(self, now=250, probe=None):
        return self.monitor.check(
            event_verifier=self.event_authority,
            approval_authority=self.approval_authority,
            publication_authority=self.publication_authority,
            provider_probe=self.probe if probe is None else probe,
            now=now,
        )

    @staticmethod
    def component(report, name):
        return next(item for item in report.components if item.component == name)

    def test_empty_config_is_healthy_and_check_is_read_only(self):
        before = tuple(connection.total_changes for connection in (
            self.queue_connection, self.release_connection, self.cms_connection,
        ))
        report = self.report()
        after = tuple(connection.total_changes for connection in (
            self.queue_connection, self.release_connection, self.cms_connection,
        ))
        self.assertEqual(report.status, "healthy")
        self.assertEqual(report.providers, ())
        self.assertEqual(report.website_versions, ())
        self.assertEqual(before, after)

    def test_pending_locales_are_visible_without_degrading_service_health(self):
        plan = self.ingest()
        report = self.report()
        self.assertEqual(report.status, "healthy")
        self.assertEqual(len(report.website_versions), 1)
        version = report.website_versions[0]
        self.assertEqual(version.plan_id, plan.plan_id)
        self.assertEqual(version.status, "processing")
        self.assertEqual(dict(version.queue_counts)["pending"], 2)
        self.assertEqual(len(self.probe.calls), 1)
        self.assertEqual(set(self.probe.calls[0]), {
            "provider_id", "model_id", "model_version",
        })

    def test_missing_or_malformed_provider_probe_blocks_without_content(self):
        self.ingest()
        missing = self.report(probe=False)
        self.assertEqual(missing.status, "blocked")
        self.assertEqual(missing.providers[0].reason, "provider.probe_missing")
        malformed = self.report(probe=ProviderProbe("malformed"))
        self.assertEqual(malformed.status, "blocked")
        self.assertEqual(malformed.providers[0].reason, "provider.unavailable")
        encoded = json.dumps(malformed.as_payload(), ensure_ascii=False)
        self.assertNotIn("Build your business", encoded)
        self.assertNotIn("private provider diagnostic", encoded)

    def test_provider_exception_is_reduced_to_a_stable_reason(self):
        self.ingest()
        report = self.report(probe=ProviderProbe("raise"))
        self.assertEqual(report.status, "blocked")
        provider = self.component(report, "providers")
        self.assertEqual(provider.reasons, ("provider.unavailable",))

    def test_completed_signed_locales_make_the_version_ready(self):
        plan = self.ingest()
        self.complete(plan)
        report = self.report()
        self.assertEqual(report.status, "healthy")
        version = report.website_versions[0]
        self.assertEqual(version.status, "ready")
        self.assertEqual(version.required_locales, 2)
        self.assertEqual(version.approved_locales, 2)
        self.assertEqual(dict(self.component(report, "release").counts), {
            "current": 2,
            "expired": 0,
            "total": 2,
        })

    def test_expired_current_approvals_are_degraded_and_visible_per_locale(self):
        plan = self.ingest()
        self.complete(plan, ttl=50)
        report = self.report(now=250)
        self.assertEqual(report.status, "degraded")
        version = report.website_versions[0]
        self.assertEqual(version.status, "awaiting_approval")
        self.assertEqual(version.approved_locales, 0)
        self.assertEqual(
            {code for _, code in version.blocked_locales},
            {"approval.expired"},
        )
        self.assertEqual(
            self.component(report, "release").reasons,
            ("release.approval_expired",),
        )

    def test_queue_tamper_blocks_and_never_discloses_payload(self):
        self.ingest()
        self.queue_connection.execute(
            "UPDATE localization_jobs SET payload_json = '{}' WHERE target_locale = 'de-AT'"
        )
        self.queue_connection.commit()
        report = self.report()
        self.assertEqual(report.status, "blocked")
        queue = self.component(report, "queue")
        self.assertEqual(queue.status, "blocked")
        self.assertIn("queue.state_invalid", queue.reasons)
        self.assertNotIn("Build your business", json.dumps(report.as_payload()))

    def test_expired_worker_lease_is_degraded_but_not_mutated(self):
        self.ingest()
        claim = self.queue.claim("crashed-worker", now=110, lease_seconds=5)
        before = self.queue_connection.total_changes
        report = self.report(now=115)
        self.assertEqual(report.status, "degraded")
        self.assertEqual(
            self.component(report, "queue").reasons,
            ("queue.lease_expired",),
        )
        self.assertEqual(self.queue_connection.total_changes, before)
        self.assertEqual(self.queue.status(claim.job_id).status, "leased")

    def test_stable_queue_failure_code_and_failed_version_are_visible(self):
        self.ingest()
        claim = self.queue.claim("translation-worker", now=110, lease_seconds=20)
        self.queue.fail(
            claim,
            "provider.invalid",
            error_detail="private provider response",
            now=111,
        )
        report = self.report()
        self.assertEqual(report.status, "degraded")
        self.assertEqual(report.website_versions[0].status, "localization_failed")
        reasons = self.component(report, "queue").reasons
        self.assertIn("queue.error.provider.invalid", reasons)
        self.assertIn("queue.locale_failed", reasons)
        self.assertNotIn("private provider response", json.dumps(report.as_payload()))

    def test_outbox_and_successful_publication_are_visible(self):
        plan = self.ingest()
        self.complete(plan)
        request = self.bridge.prepare_delivery(
            event()["event_id"],
            self.event_authority,
            self.approval_authority,
            self.publication_authority,
            now=250,
        )
        pending = self.report()
        self.assertEqual(pending.website_versions[0].status, "publishing")
        self.assertEqual(dict(self.component(pending, "cms").counts)["pending"], 1)

        publisher = Publisher()
        outcome = self.bridge.run_delivery(
            publisher,
            self.publication_authority,
            worker_id="cms-worker",
            clock=lambda: 260,
        )
        self.assertEqual(outcome.delivery_id, request.delivery_id)
        published = self.report(now=261)
        self.assertEqual(published.website_versions[0].status, "published")
        self.assertEqual(dict(self.component(published, "cms").counts)["succeeded"], 1)

    def test_retrying_publication_exposes_only_the_stable_ack_error(self):
        plan = self.ingest()
        self.complete(plan)
        self.bridge.prepare_delivery(
            event()["event_id"],
            self.event_authority,
            self.approval_authority,
            self.publication_authority,
            now=250,
        )

        class WrongAcknowledgement:
            def publish(self, request):
                return {"status": "accepted"}

        outcome = self.bridge.run_delivery(
            WrongAcknowledgement(),
            self.publication_authority,
            worker_id="cms-worker",
            clock=lambda: 260,
        )
        self.assertEqual(outcome.status, "retry_wait")
        report = self.report(now=261)
        self.assertEqual(report.status, "degraded")
        self.assertIn(
            "cms.delivery.error.publisher.ack_invalid",
            self.component(report, "cms").reasons,
        )

    def test_delivery_and_event_tampering_block_fail_closed(self):
        plan = self.ingest()
        self.complete(plan)
        self.bridge.prepare_delivery(
            event()["event_id"],
            self.event_authority,
            self.approval_authority,
            self.publication_authority,
            now=250,
        )
        self.cms_connection.execute(
            "UPDATE cms_publication_deliveries SET payload_json = '{}'"
        )
        self.cms_connection.commit()
        delivery_report = self.report()
        self.assertEqual(delivery_report.status, "blocked")
        self.assertIn("cms.delivery.invalid", self.component(delivery_report, "cms").reasons)

        self.cms_connection.execute(
            "UPDATE cms_change_events SET signature = 'forged'"
        )
        self.cms_connection.commit()
        event_report = self.report()
        self.assertEqual(event_report.status, "blocked")
        self.assertIn("cms.event.invalid", self.component(event_report, "cms").reasons)

    def test_signed_payloads_cannot_be_detached_from_database_identity(self):
        plan = self.ingest()
        self.complete(plan)
        self.bridge.prepare_delivery(
            event()["event_id"],
            self.event_authority,
            self.approval_authority,
            self.publication_authority,
            now=250,
        )
        self.cms_connection.execute(
            "UPDATE cms_publication_deliveries SET plan_id = 'different-plan'"
        )
        self.cms_connection.commit()
        cms_report = self.report()
        self.assertEqual(cms_report.status, "blocked")
        self.assertIn("cms.delivery.invalid", self.component(cms_report, "cms").reasons)

        self.release_connection.execute(
            "UPDATE localization_approvals SET expires_at = expires_at + 1"
        )
        self.release_connection.commit()
        release_report = self.report()
        self.assertEqual(release_report.status, "blocked")
        self.assertIn(
            "release.approval_invalid",
            self.component(release_report, "release").reasons,
        )


if __name__ == "__main__":
    unittest.main()
