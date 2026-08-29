from __future__ import annotations

import hashlib
import hmac
import importlib.util
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CMS = load(
    "blun_test_website_localization_cms",
    ROOT / "integrations" / "website_localization_cms.py",
)
PLANNER = CMS._PLANNER
RELEASE = CMS._RELEASE
QUEUE = CMS._QUEUE
WORKER = RELEASE._WORKER


class CMSAuthority:
    def __init__(self, key=b"cms-isolated-key"):
        self.key = key

    def sign(self, payload):
        return CMS.CMSMessageSignature(
            "hmac-sha256-test",
            "cms-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return (
            signature.algorithm == "hmac-sha256-test"
            and signature.key_id == "cms-key-1"
            and hmac.compare_digest(signature.signature, expected)
        )


class ApprovalAuthority:
    def __init__(self, key=b"release-isolated-key"):
        self.key = key

    def sign(self, payload):
        return RELEASE.ApprovalSignature(
            "hmac-sha256-test",
            "release-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


class ReceiptVerifier:
    def verify(self, **values):
        return values["receipt"] == "quality-receipt"


class Publisher:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def publish(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response(request)
        return {
            "schema": CMS.ACK_SCHEMA,
            "delivery_id": request.delivery_id,
            "payload_sha256": request.payload_sha256,
            "status": "accepted",
        }


class Clock:
    def __init__(self, value):
        self.value = float(value)

    def __call__(self):
        return self.value


def change_event(**overrides):
    localization = {
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
    }
    event = {
        "schema": CMS.CHANGE_SCHEMA,
        "event_id": "cms-event-184",
        "site_id": "blun-marketing",
        "website_version": "website-2026-08-29.1",
        "localization": localization,
    }
    for key, value in overrides.items():
        if key in localization:
            localization[key] = value
        else:
            event[key] = value
    return event


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


class WebsiteLocalizationCMSBridgeTests(unittest.TestCase):
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
        self.event_authority = CMSAuthority(b"event-key")
        self.publication_authority = CMSAuthority(b"publication-key")
        self.approval_authority = ApprovalAuthority()
        self.receipt_verifier = ReceiptVerifier()

    def tearDown(self):
        self.cms_connection.close()
        self.release_connection.close()
        self.queue_connection.close()

    def signed_event(self, event):
        payload = CMS._canonical_json(event).encode("utf-8")
        return self.event_authority.sign(payload)

    def ingest(self, event=None, **values):
        event = event or change_event()
        return self.bridge.ingest_change(
            event,
            self.signed_event(event),
            self.event_authority,
            now=100,
            **values,
        )

    def plan(self, event=None):
        event = event or change_event()
        return PLANNER.plan_from_mapping(event["localization"])

    def release_all(self, event=None):
        event = event or change_event()
        plan = self.plan(event)
        translations = {
            "de-AT": "Bring dein Unternehmen mit BLUN voran.",
            "sv-SE": "Ta ditt företag vidare med BLUN.",
        }
        for index, job in enumerate(plan.jobs):
            claim = self.queue.claim("translation-worker", now=110 + index, lease_seconds=20)
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
                ttl_seconds=1000,
            )
        return plan

    def prepare(self, event=None, **values):
        event = event or change_event()
        return self.bridge.prepare_delivery(
            event["event_id"],
            self.event_authority,
            self.approval_authority,
            self.publication_authority,
            now=250,
            **values,
        )

    def test_signed_change_is_enqueued_once_and_exact_replay_resumes(self):
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(first.job_count, 2)
        self.assertEqual(first.inserted_jobs, 2)
        self.assertEqual(second.inserted_jobs, 0)
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(self.queue.plan_counts(first.plan_id)["pending"], 2)

    def test_bad_signature_and_event_id_collision_fail_before_new_work(self):
        event = change_event()
        forged = replace(self.signed_event(event), signature="forged")
        with self.assertRaises(CMS.CMSBridgeBlocked) as caught:
            self.bridge.ingest_change(event, forged, self.event_authority, now=100)
        self.assertEqual(caught.exception.code, "cms.event.signature_rejected")
        self.assertEqual(self.cms_connection.execute("SELECT COUNT(*) FROM cms_change_events").fetchone()[0], 0)

        self.ingest(event)
        changed = change_event(source_text="Changed source under a reused event ID.")
        with self.assertRaises(CMS.CMSBridgeBlocked) as caught:
            self.ingest(changed)
        self.assertEqual(caught.exception.code, "cms.event.idempotency_collision")
        self.assertEqual(self.queue_connection.execute("SELECT COUNT(*) FROM localization_jobs").fetchone()[0], 2)

    def test_partial_approvals_never_create_a_delivery(self):
        self.ingest()
        plan = self.plan()
        first = self.queue.claim("translation-worker", now=110, lease_seconds=20)
        job = next(item for item in plan.jobs if item.job_id == first.job_id)
        self.queue.complete(first, completed_result(job, "Natürlicher Zieltext."), now=111)
        self.release_store.approve(
            plan, job.job_id, "quality-receipt", self.receipt_verifier,
            self.approval_authority, now=200, ttl_seconds=1000,
        )
        with self.assertRaises(CMS.CMSBridgeBlocked) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "cms.website.not_ready")
        self.assertEqual(
            self.cms_connection.execute("SELECT COUNT(*) FROM cms_publication_deliveries").fetchone()[0],
            0,
        )

    def test_complete_bundle_is_signed_ordered_and_idempotent(self):
        self.ingest()
        self.release_all()
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first.delivery_id, second.delivery_id)
        self.assertEqual(first.payload_sha256, second.payload_sha256)
        self.assertEqual(
            [item["locale"] for item in first.payload["localizations"]],
            ["de-AT", "sv-SE"],
        )
        self.assertTrue(self.publication_authority.verify(
            CMS._canonical_json(first.payload).encode("utf-8"),
            first.signature,
        ))
        self.assertEqual(
            self.cms_connection.execute("SELECT COUNT(*) FROM cms_publication_deliveries").fetchone()[0],
            1,
        )

    def test_success_requires_exact_ack_and_sends_one_complete_request(self):
        self.ingest()
        self.release_all()
        request = self.prepare()
        publisher = Publisher()
        outcome = self.bridge.run_delivery(
            publisher,
            self.publication_authority,
            worker_id="cms-worker",
            clock=Clock(260),
        )
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(publisher.requests), 1)
        self.assertEqual(publisher.requests[0].delivery_id, request.delivery_id)
        self.assertEqual(len(publisher.requests[0].payload["localizations"]), 2)
        self.assertEqual(self.bridge.run_delivery(
            publisher,
            self.publication_authority,
            worker_id="cms-worker",
            clock=Clock(261),
        ).status, "idle")

    def test_invalid_ack_retries_with_bound_and_opaque_error(self):
        self.ingest()
        self.release_all()
        request = self.prepare(max_attempts=2)
        publisher = Publisher(response=lambda _: {"status": "accepted"})
        first = self.bridge.run_delivery(
            publisher,
            self.publication_authority,
            worker_id="cms-worker",
            clock=Clock(260),
        )
        self.assertEqual((first.status, first.error_code), ("retry_wait", "publisher.ack_invalid"))
        second = self.bridge.run_delivery(
            publisher,
            self.publication_authority,
            worker_id="cms-worker",
            clock=Clock(265),
        )
        self.assertEqual((second.status, second.attempt), ("failed", 2))
        status = self.bridge.delivery_status(request.delivery_id)
        self.assertIsNone(status.last_error_detail_hash)
        self.assertEqual(len(publisher.requests), 2)

    def test_transport_detail_is_hashed_and_nonretryable_failure_is_terminal(self):
        self.ingest()
        self.release_all()
        request = self.prepare()
        publisher = Publisher(error=CMS.CMSPublishFailed(
            "publisher.rejected",
            retryable=False,
            detail="CMS rejected private customer content",
        ))
        outcome = self.bridge.run_delivery(
            publisher,
            self.publication_authority,
            worker_id="cms-worker",
            clock=Clock(260),
        )
        self.assertEqual(outcome.status, "failed")
        status = self.bridge.delivery_status(request.delivery_id)
        self.assertEqual(
            status.last_error_detail_hash,
            hashlib.sha256(b"CMS rejected private customer content").hexdigest(),
        )
        row = self.cms_connection.execute(
            "SELECT last_error_detail_hash FROM cms_publication_deliveries"
        ).fetchone()
        self.assertNotIn("customer", row[0])

    def test_expired_delivery_lease_is_recovered_and_stale_claim_cannot_finish(self):
        self.ingest()
        self.release_all()
        self.prepare(max_attempts=2)
        stale = self.bridge.claim_delivery(
            "crashed-worker", self.publication_authority, now=260, lease_seconds=5,
        )
        fresh = self.bridge.claim_delivery(
            "recovery-worker", self.publication_authority, now=265, lease_seconds=5,
        )
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh.attempt, 2)
        with self.assertRaises(CMS.CMSBridgeBlocked) as caught:
            self.bridge._finish(stale, now=266, error=None)
        self.assertEqual(caught.exception.code, "cms.delivery.lease_lost")

    def test_tampering_blocks_before_publisher_call(self):
        self.ingest()
        self.release_all()
        self.prepare()
        self.cms_connection.execute(
            "UPDATE cms_publication_deliveries SET payload_json = '{}'"
        )
        self.cms_connection.commit()
        publisher = Publisher()
        with self.assertRaises(CMS.CMSBridgeBlocked) as caught:
            self.bridge.run_delivery(
                publisher,
                self.publication_authority,
                worker_id="cms-worker",
                clock=Clock(260),
            )
        self.assertEqual(caught.exception.code, "cms.delivery.tampered")
        self.assertEqual(publisher.requests, [])

    def test_expired_approval_blocks_before_publisher_call(self):
        self.ingest()
        self.release_all()
        self.prepare()
        publisher = Publisher()
        with self.assertRaises(CMS.CMSBridgeBlocked) as caught:
            self.bridge.run_delivery(
                publisher,
                self.publication_authority,
                worker_id="cms-worker",
                clock=Clock(1200),
            )
        self.assertEqual(caught.exception.code, "cms.delivery.approval_expired")
        self.assertEqual(publisher.requests, [])


if __name__ == "__main__":
    unittest.main()
