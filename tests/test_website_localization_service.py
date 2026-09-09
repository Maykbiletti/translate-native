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


SERVICE = load(
    "blun_test_website_localization_service",
    ROOT / "integrations" / "website_localization_service.py",
)
COORDINATOR = SERVICE._COORDINATOR
CMS = SERVICE._CMS
QUEUE = CMS._QUEUE
RELEASE = CMS._RELEASE
WORKER = SERVICE._RUNNER._WORKER


class Authority:
    def __init__(self, signature_type, key: bytes, key_id: str):
        self.signature_type = signature_type
        self.key = key
        self.key_id = key_id

    def sign(self, payload):
        return self.signature_type(
            "hmac-sha256-test",
            self.key_id,
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        self.value += 1
        return self.value


class LocalizationProvider:
    def __init__(self, *, fail_once=False):
        self.fail_once = fail_once
        self.calls = []

    def invoke(self, request):
        self.calls.append(request.phase)
        if self.fail_once:
            self.fail_once = False
            raise WORKER.ProviderCallFailed("network", retryable=True)
        if request.phase == "transcreation":
            return {
                "schema": WORKER.CANDIDATE_SCHEMA,
                "phase": request.phase,
                "locale": request.input["target"]["locale"],
                "candidate": "Baue dein Unternehmen mit BLUN auf.",
            }
        return {
            "schema": WORKER.REVIEW_SCHEMA,
            "phase": request.phase,
            "locale": request.input["target"]["locale"],
            "status": "PASS",
            "confidence": "high",
            "blocking_defects": [],
            "major_defects": [],
        }


def quality_receipt(source_text, target_text, target_locale, request_id):
    value = "\x1f".join((source_text, target_text, target_locale, request_id))
    return "quality:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class EvidenceProvider:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    def obtain(self, request):
        self.requests.append(request)
        if self.fail:
            raise COORDINATOR.QualityEvidenceUnavailable("offline", retryable=True)
        return {
            "schema": COORDINATOR.EVIDENCE_RESPONSE_SCHEMA,
            "request_id": request.request_id,
            "result_sha256": request.result_sha256,
            "quality_receipt": quality_receipt(
                request.source_text,
                request.target_text,
                request.target_locale,
                request.request_id,
            ),
            "human_review_receipt": None,
            "independent_model_review": None,
        }


class QualityVerifier:
    def __init__(self, provider):
        self.provider = provider

    def verify(self, **values):
        binding = values["binding"]
        return any(
            values["receipt"] == quality_receipt(
                binding["source_text"],
                binding["target_text"],
                binding["target_locale"],
                request.request_id,
            )
            for request in self.provider.requests
        )


class Publisher:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    def publish(self, request):
        self.requests.append(request)
        if self.fail:
            raise CMS.CMSPublishFailed(
                "network", retryable=True, detail="private transport prose",
            )
        return {
            "schema": CMS.ACK_SCHEMA,
            "delivery_id": request.delivery_id,
            "payload_sha256": request.payload_sha256,
            "status": "accepted",
        }


class WebsiteLocalizationServiceTests(unittest.TestCase):
    def setUp(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.release_store = RELEASE.LocalizationReleaseStore(
            self.release_connection, self.queue,
        )
        self.cms_connection = sqlite3.connect(":memory:")
        self.bridge = CMS.WebsiteLocalizationCMSBridge(
            self.cms_connection, self.queue, self.release_store,
        )
        self.evidence_connection = sqlite3.connect(":memory:")
        self.evidence_state = COORDINATOR.QualityEvidenceStateStore(
            self.evidence_connection,
        )
        self.event_authority = Authority(
            CMS.CMSMessageSignature, b"event", "event-key",
        )
        self.publication_authority = Authority(
            CMS.CMSMessageSignature, b"publication", "publication-key",
        )
        self.approval_authority = Authority(
            RELEASE.ApprovalSignature, b"approval", "approval-key",
        )
        self.provider = LocalizationProvider()
        self.evidence = EvidenceProvider()
        self.publisher = Publisher()
        self.clock = Clock()
        self.ingest("event-1", "site-version-1", source_sequence=1)

    def tearDown(self):
        self.evidence_connection.close()
        self.cms_connection.close()
        self.release_connection.close()
        self.queue_connection.close()

    def event(self, event_id, version, *, source_id="homepage.hero", source_sequence=1):
        return {
            "schema": CMS.CHANGE_SCHEMA,
            "event_id": event_id,
            "site_id": "public-site",
            "website_version": version,
            "source_sequence": source_sequence,
            "localization": {
                "source_id": source_id,
                "source_revision": version,
                "source_text": "Build your business with BLUN.",
                "source_locale": "en-IE",
                "content_type": "headline",
                "glossary_version": "public-glossary-1",
                "policy_version": "native-web-1",
                "provider_id": "customer-llm",
                "model_id": "configured-model",
                "model_version": "2026-09-07",
                "software_version": "6.43.0-dev",
                "target_locales": ["de-AT"],
            },
        }

    def ingest(self, event_id, version, *, source_id="homepage.hero", source_sequence=1):
        event = self.event(
            event_id, version, source_id=source_id, source_sequence=source_sequence,
        )
        signature = self.event_authority.sign(
            CMS._canonical_json(event).encode("utf-8"),
        )
        self.bridge.ingest_change(
            event, signature, self.event_authority, now=self.clock(),
        )

    def assets(self, payload):
        return WORKER.LocalizationAssets(
            glossary_version=payload["glossary_version"],
            policy_version=payload["policy_version"],
            audience="European business owners",
            tone_profile="Natural, concise, and precise",
            protected_terms=("BLUN",),
        )

    def tick(self, **overrides):
        values = {
            "provider_resolver": lambda payload: self.provider,
            "assets_resolver": self.assets,
            "evidence_provider": self.evidence,
            "quality_verifier": QualityVerifier(self.evidence),
            "event_verifier": self.event_authority,
            "approval_authority": self.approval_authority,
            "publication_authority": self.publication_authority,
            "publisher": self.publisher,
            "translation_worker_id": "translation-worker",
            "evidence_worker_id": "quality-worker",
            "delivery_worker_id": "delivery-worker",
            "evidence_revision": "quality-evidence-1",
            "clock": self.clock,
            "translation_retry_base_seconds": 5,
            "translation_retry_max_seconds": 60,
            "approval_ttl_seconds": 1000,
        }
        values.update(overrides)
        return SERVICE.run_service_tick(
            self.bridge, self.evidence_state, **values,
        )

    def test_three_ticks_move_one_locale_from_queue_to_cms(self):
        translated = self.tick()
        self.assertEqual((translated.phase, translated.status), ("translation", "succeeded"))
        self.assertEqual(self.provider.calls, [
            "transcreation", "target_native", "source_fidelity",
        ])
        self.assertEqual(self.evidence.requests, [])
        self.assertEqual(self.publisher.requests, [])

        released = self.tick()
        self.assertEqual((released.phase, released.status), ("release", "delivery_ready"))
        self.assertEqual(released.target_locale, "de-AT")
        self.assertEqual(len(self.evidence.requests), 1)
        self.assertEqual(self.publisher.requests, [])

        delivered = self.tick()
        self.assertEqual((delivered.phase, delivered.status), ("delivery", "succeeded"))
        self.assertEqual(delivered.event_id, "event-1")
        self.assertEqual(len(self.publisher.requests), 1)
        idle = self.tick()
        self.assertEqual((idle.phase, idle.status), ("idle", "idle"))

    def test_due_delivery_has_priority_over_a_new_translation(self):
        self.tick()
        self.tick()
        self.ingest("event-2", "site-version-2", source_id="homepage.footer")

        delivered = self.tick()

        self.assertEqual(delivered.phase, "delivery")
        self.assertEqual(delivered.event_id, "event-1")
        self.assertEqual(self.queue.plan_counts(
            self.cms_connection.execute(
                "SELECT plan_id FROM cms_change_events WHERE event_id = 'event-2'"
            ).fetchone()[0]
        )["pending"], 1)

    def test_new_source_revision_blocks_older_delivery_and_releases_new_event(self):
        self.tick()
        self.tick()
        self.ingest("event-2", "site-version-2", source_sequence=2)

        translated = self.tick()

        self.assertEqual((translated.phase, translated.status), ("translation", "succeeded"))
        self.assertEqual(self.publisher.requests, [])
        old_delivery = self.cms_connection.execute("""
            SELECT status, last_error_code FROM cms_publication_deliveries
            WHERE event_id = 'event-1'
        """).fetchone()
        self.assertEqual(tuple(old_delivery), ("failed", "event_superseded"))
        released = self.tick()
        self.assertEqual((released.phase, released.status), ("release", "delivery_ready"))
        self.assertEqual(released.event_id, "event-2")

    def test_worker_never_calls_provider_for_superseded_pending_plan(self):
        old_plan_id = self.cms_connection.execute(
            "SELECT plan_id FROM cms_change_events WHERE event_id = 'event-1'"
        ).fetchone()[0]
        self.ingest("event-2", "site-version-2", source_sequence=2)

        outcome = self.tick()

        new_plan_id = self.cms_connection.execute(
            "SELECT plan_id FROM cms_change_events WHERE event_id = 'event-2'"
        ).fetchone()[0]
        self.assertEqual((outcome.phase, outcome.status), ("translation", "succeeded"))
        self.assertIn(new_plan_id, self.queue.status(outcome.job_id).plan_ids)
        self.assertNotIn(old_plan_id, self.queue.status(outcome.job_id).plan_ids)
        old_status = self.queue_connection.execute("""
            SELECT jobs.status
            FROM localization_jobs AS jobs
            JOIN localization_plan_jobs AS mapping ON mapping.job_id = jobs.job_id
            WHERE mapping.plan_id = ?
        """, (old_plan_id,)).fetchone()[0]
        self.assertEqual(old_status, "pending")
        self.assertEqual(self.provider.calls, [
            "transcreation", "target_native", "source_fidelity",
        ])

    def test_provider_failure_is_one_content_free_transition(self):
        self.provider = LocalizationProvider(fail_once=True)

        outcome = self.tick()

        self.assertEqual((outcome.phase, outcome.status), ("translation", "retry_wait"))
        self.assertEqual(outcome.error_code, "provider.network")
        self.assertEqual(self.provider.calls, ["transcreation"])
        encoded = json.dumps(outcome.as_payload())
        self.assertNotIn("Build your business", encoded)
        self.assertNotIn("Baue dein Unternehmen", encoded)

    def test_evidence_failure_does_not_fall_through_to_another_provider(self):
        self.tick()
        self.evidence = EvidenceProvider(fail=True)
        provider_calls = len(self.provider.calls)

        outcome = self.tick()

        self.assertEqual((outcome.phase, outcome.status), ("release", "blocked"))
        self.assertEqual(outcome.error_code, "evidence.offline")
        self.assertEqual(len(self.evidence.requests), 1)
        self.assertEqual(len(self.provider.calls), provider_calls)

    def test_publisher_failure_keeps_detail_out_of_service_status(self):
        self.tick()
        self.tick()
        self.publisher = Publisher(fail=True)

        outcome = self.tick()

        self.assertEqual((outcome.phase, outcome.status), ("delivery", "retry_wait"))
        self.assertEqual(outcome.error_code, "network")
        self.assertNotIn("private transport prose", json.dumps(outcome.as_payload()))

    def test_tampered_event_blocks_without_translation_or_evidence_call(self):
        self.cms_connection.execute(
            "UPDATE cms_change_events SET event_sha256 = ? WHERE event_id = ?",
            ("0" * 64, "event-1"),
        )
        self.cms_connection.commit()

        outcome = self.tick()

        self.assertEqual((outcome.phase, outcome.status), ("release", "blocked"))
        self.assertEqual(outcome.error_code, "cms.event.tampered")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.evidence.requests, [])

    def test_tampered_identifier_is_never_echoed_in_status(self):
        secret = "customer source prose must not become an identifier"
        self.cms_connection.execute(
            "UPDATE cms_change_events SET plan_id = ? WHERE event_id = ?",
            (secret, "event-1"),
        )
        self.cms_connection.commit()

        outcome = self.tick()

        self.assertEqual((outcome.phase, outcome.status), ("release", "blocked"))
        self.assertEqual(outcome.error_code, "service.state.identifier_invalid")
        self.assertIsNone(outcome.event_id)
        self.assertIsNone(outcome.plan_id)
        self.assertNotIn(secret, json.dumps(outcome.as_payload()))


if __name__ == "__main__":
    unittest.main()
