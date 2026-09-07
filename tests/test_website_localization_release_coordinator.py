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


COORDINATOR = load(
    "blun_test_website_localization_release_coordinator",
    ROOT / "integrations" / "website_localization_release_coordinator.py",
)
CMS = COORDINATOR._CMS
PLANNER = CMS._PLANNER
RELEASE = CMS._RELEASE
QUEUE = CMS._QUEUE
WORKER = RELEASE._WORKER


class CMSAuthority:
    def __init__(self, key):
        self.key = key
        self.sign_calls = 0

    def sign(self, payload):
        self.sign_calls += 1
        return CMS.CMSMessageSignature(
            "hmac-sha256-test",
            "cms-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


class ApprovalAuthority:
    def __init__(self, key=b"release-key"):
        self.key = key
        self.sign_calls = 0

    def sign(self, payload):
        self.sign_calls += 1
        return RELEASE.ApprovalSignature(
            "hmac-sha256-test",
            "release-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


def receipt(kind, source_text, target_text, target_locale, request_id):
    value = "\x1f".join((kind, source_text, target_text, target_locale, request_id))
    return kind + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class ReceiptVerifier:
    def __init__(self, kind, requests):
        self.kind = kind
        self.requests = requests
        self.calls = []

    def verify(self, **values):
        self.calls.append(values)
        return any(
            values["receipt"] == receipt(
                self.kind,
                values["source_text"],
                values["target_text"],
                values["target_locale"],
                request.request_id,
            )
            for request in self.requests
        )


class EvidenceProvider:
    def __init__(self, *, error=None, mutate=None, include_human=True, wrong_receipt=False):
        self.error = error
        self.mutate = mutate
        self.include_human = include_human
        self.wrong_receipt = wrong_receipt
        self.requests = []

    def obtain(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.mutate is not None:
            self.mutate(request)
        quality = receipt(
            "quality", request.source_text, request.target_text,
            request.target_locale, request.request_id,
        )
        if self.wrong_receipt:
            quality = "quality:" + "0" * 64
        human = None
        if request.human_review_required and self.include_human:
            human = receipt(
                "human", request.source_text, request.target_text,
                request.target_locale, request.request_id,
            )
        return {
            "schema": COORDINATOR.EVIDENCE_RESPONSE_SCHEMA,
            "request_id": request.request_id,
            "result_sha256": request.result_sha256,
            "quality_receipt": quality,
            "human_review_receipt": human,
        }


def change_event(*, targets=("de-AT", "sv-SE"), content_type="headline"):
    source_text = (
        "By continuing, you accept the terms."
        if content_type == "legal"
        else "Build your business with BLUN."
    )
    return {
        "schema": CMS.CHANGE_SCHEMA,
        "event_id": "cms-event-release-1",
        "site_id": "public-website",
        "website_version": "website-2026-09-07.1",
        "localization": {
            "source_id": "homepage.hero",
            "source_revision": "cms-184",
            "source_text": source_text,
            "source_locale": "en-IE",
            "content_type": content_type,
            "glossary_version": "public-glossary-3",
            "policy_version": "native-web-1",
            "provider_id": "customer-llm",
            "model_id": "configured-model",
            "model_version": "2026-09-07",
            "software_version": "6.43.0-dev",
            "target_locales": list(targets),
        },
    }


def completed_result(job, target_text):
    payload = job.as_payload()
    return {
        "schema": WORKER.RESULT_SCHEMA,
        "worker_schema": WORKER.WORKER_SCHEMA,
        "job_id": payload["job_id"],
        "source_sha256": payload["source"]["sha256"],
        "target_sha256": hashlib.sha256(target_text.encode("utf-8")).hexdigest(),
        "source_locale": payload["source"]["locale"],
        "target_locale": payload["target"]["locale"],
        "content_type": payload["content_type"],
        "glossary_version": payload["glossary_version"],
        "policy_version": payload["policy_version"],
        "provider": payload["provider"],
        "software_version": payload["software_version"],
        "candidate": target_text,
        "quality_passes": [
            {
                "phase": phase,
                "request_sha256": hashlib.sha256(("request-" + phase).encode()).hexdigest(),
                "response_sha256": hashlib.sha256(("response-" + phase).encode()).hexdigest(),
                "status": "PASS",
            }
            for phase in WORKER.PHASES
        ],
        "integrity": {"status": "PASS", "guard": "translate-native-structure-and-token-gate"},
        "human_review_required": payload["content_type"] == "legal",
        "release_required": True,
    }


class WebsiteLocalizationReleaseCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.store = RELEASE.LocalizationReleaseStore(self.release_connection, self.queue)
        self.cms_connection = sqlite3.connect(":memory:")
        self.bridge = CMS.WebsiteLocalizationCMSBridge(
            self.cms_connection, self.queue, self.store,
        )
        self.event_authority = CMSAuthority(b"event-key")
        self.publication_authority = CMSAuthority(b"publication-key")
        self.approval_authority = ApprovalAuthority()
        self.event = change_event()
        self.plan = PLANNER.plan_from_mapping(self.event["localization"])
        signature = self.event_authority.sign(CMS._canonical_json(self.event).encode("utf-8"))
        self.bridge.ingest_change(
            self.event, signature, self.event_authority, now=100,
        )

    def tearDown(self):
        self.cms_connection.close()
        self.release_connection.close()
        self.queue_connection.close()

    def complete_all(self, translations=None):
        translations = translations or {
            "de-AT": "Bring dein Unternehmen voran.",
            "sv-SE": "Ta ditt företag vidare.",
        }
        jobs = {job.job_id: job for job in self.plan.jobs}
        for index in range(len(jobs)):
            claim = self.queue.claim("worker", now=110 + index, lease_seconds=30)
            self.assertIsNotNone(claim)
            self.queue.complete(
                claim,
                completed_result(jobs[claim.job_id], translations[claim.target_locale]),
                now=111 + index,
            )

    def run_release(self, provider, **overrides):
        values = {
            "evidence_revision": "native-evidence-1",
            "now": 200,
            "approval_ttl_seconds": 1000,
        }
        values.update(overrides)
        quality = ReceiptVerifier("quality", provider.requests)
        human = ReceiptVerifier("human", provider.requests)
        outcome = COORDINATOR.run_next_release(
            self.bridge,
            self.event["event_id"],
            self.event_authority,
            provider,
            quality,
            self.approval_authority,
            self.publication_authority,
            human_review_verifier=human,
            **values,
        )
        return outcome, quality, human

    def test_approves_one_locale_per_run_then_prepares_only_the_complete_bundle(self):
        self.complete_all()
        provider = EvidenceProvider()

        first, _, _ = self.run_release(provider)
        self.assertEqual((first.status, first.target_locale), ("approved", "de-AT"))
        self.assertIsNone(first.delivery_id)
        self.assertEqual(self.cms_connection.execute(
            "SELECT COUNT(*) FROM cms_publication_deliveries"
        ).fetchone()[0], 0)

        second, _, _ = self.run_release(provider)
        self.assertEqual((second.status, second.target_locale), ("delivery_ready", "sv-SE"))
        self.assertIsNotNone(second.delivery_id)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(self.cms_connection.execute(
            "SELECT COUNT(*) FROM cms_publication_deliveries"
        ).fetchone()[0], 1)

    def test_evidence_request_is_exactly_bound_and_contains_one_locale(self):
        self.complete_all()
        provider = EvidenceProvider()
        outcome, _, _ = self.run_release(provider)
        request = provider.requests[0]
        payload = request.as_payload()

        self.assertEqual(outcome.target_locale, "de-AT")
        self.assertEqual(payload["schema"], COORDINATOR.EVIDENCE_REQUEST_SCHEMA)
        self.assertEqual(payload["target_locale"], "de-AT")
        self.assertEqual(payload["source_text"], self.event["localization"]["source_text"])
        self.assertEqual(payload["target_text"], "Bring dein Unternehmen voran.")
        self.assertEqual(payload["provider"]["model_id"], "configured-model")
        self.assertTrue(payload["request_id"].startswith("blun-l10n-evidence-"))
        self.assertNotIn("target_locales", json.dumps(payload))

    def test_replay_reuses_existing_delivery_without_evidence_or_resigning(self):
        self.complete_all()
        provider = EvidenceProvider()
        self.run_release(provider)
        ready, _, _ = self.run_release(provider)
        calls_before = len(provider.requests)
        signatures_before = self.publication_authority.sign_calls

        replay, _, _ = self.run_release(provider, now=201)

        self.assertEqual(replay.status, "delivery_ready")
        self.assertEqual(replay.delivery_id, ready.delivery_id)
        self.assertEqual(len(provider.requests), calls_before)
        self.assertEqual(self.publication_authority.sign_calls, signatures_before)

    def test_pending_delivery_with_expired_approval_is_not_reported_ready(self):
        self.complete_all()
        provider = EvidenceProvider()
        self.run_release(provider, approval_ttl_seconds=5)
        ready, _, _ = self.run_release(provider, approval_ttl_seconds=5)
        self.assertEqual(ready.status, "delivery_ready")

        with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
            self.run_release(provider, now=206, approval_ttl_seconds=5)

        self.assertEqual(caught.exception.code, "cms.delivery.approval_expired")
        self.assertEqual(self.cms_connection.execute(
            "SELECT COUNT(*) FROM cms_publication_deliveries"
        ).fetchone()[0], 1)
        self.assertEqual(len(provider.requests), 2)

    def test_publication_signing_failure_resumes_without_requesting_quality_again(self):
        self.complete_all()
        provider = EvidenceProvider()
        self.run_release(provider)

        working_authority = self.publication_authority
        self.publication_authority = type("BrokenAuthority", (), {
            "sign": lambda _self, _payload: (_ for _ in ()).throw(RuntimeError("private detail")),
            "verify": lambda _self, _payload, _signature: False,
        })()
        with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
            self.run_release(provider)
        self.assertEqual(caught.exception.code, "cms.publication.signing_failed")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(self.release_connection.execute(
            "SELECT COUNT(*) FROM localization_approvals"
        ).fetchone()[0], 2)
        self.assertEqual(self.cms_connection.execute(
            "SELECT COUNT(*) FROM cms_publication_deliveries"
        ).fetchone()[0], 0)

        self.publication_authority = working_authority
        recovered, _, _ = self.run_release(provider, now=201)
        self.assertEqual(recovered.status, "delivery_ready")
        self.assertIsNotNone(recovered.delivery_id)
        self.assertEqual(len(provider.requests), 2)

    def test_expired_partial_approval_requires_a_new_evidence_revision(self):
        self.complete_all()
        provider = EvidenceProvider()
        first, _, _ = self.run_release(provider, approval_ttl_seconds=5)
        self.assertEqual((first.status, first.target_locale), ("approved", "de-AT"))

        refreshed, _, _ = self.run_release(
            provider,
            now=206,
            evidence_revision="native-evidence-2",
            approval_ttl_seconds=1000,
        )
        self.assertEqual((refreshed.status, refreshed.target_locale), ("approved", "de-AT"))
        self.assertNotEqual(first.approval_id, refreshed.approval_id)
        self.assertEqual(self.release_connection.execute(
            "SELECT COUNT(*) FROM localization_approvals WHERE target_locale = 'de-AT'"
        ).fetchone()[0], 2)

    def test_successfully_delivered_event_is_terminal_for_release_coordinator(self):
        self.complete_all()
        provider = EvidenceProvider()
        self.run_release(provider)
        ready, _, _ = self.run_release(provider)
        publisher = type("Publisher", (), {"publish": lambda _self, request: {
            "schema": CMS.ACK_SCHEMA,
            "delivery_id": request.delivery_id,
            "payload_sha256": request.payload_sha256,
            "status": "accepted",
        }})()
        delivered = self.bridge.run_delivery(
            publisher,
            self.publication_authority,
            worker_id="publisher",
            clock=lambda: 201,
        )
        self.assertEqual(delivered.status, "succeeded")

        replay, _, _ = self.run_release(provider, now=1201)
        self.assertEqual((replay.status, replay.delivery_id), ("delivered", ready.delivery_id))

    def test_pending_or_failed_locale_never_requests_evidence_or_delivery(self):
        provider = EvidenceProvider()
        outcome, _, _ = self.run_release(provider)
        self.assertEqual(outcome.status, "waiting")
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.release_connection.execute(
            "SELECT COUNT(*) FROM localization_approvals"
        ).fetchone()[0], 0)
        self.assertEqual(self.cms_connection.execute(
            "SELECT COUNT(*) FROM cms_publication_deliveries"
        ).fetchone()[0], 0)

    def test_evidence_provider_failure_is_content_free_and_creates_no_approval(self):
        self.complete_all()
        provider = EvidenceProvider(error=COORDINATOR.QualityEvidenceUnavailable(
            "timeout", retryable=True,
        ))
        with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
            self.run_release(provider)
        self.assertEqual(caught.exception.code, "evidence.timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("Build your business", str(caught.exception))
        self.assertEqual(self.release_connection.execute(
            "SELECT COUNT(*) FROM localization_approvals"
        ).fetchone()[0], 0)

    def test_wrong_evidence_binding_and_mutated_request_fail_closed(self):
        self.complete_all()

        class WrongBinding(EvidenceProvider):
            def obtain(self, request):
                response = super().obtain(request)
                response["result_sha256"] = "0" * 64
                return response

        for provider, code in (
            (WrongBinding(), "evidence.response.binding_mismatch"),
            (EvidenceProvider(mutate=lambda request: object.__setattr__(
                request, "target_text", "changed after binding",
            )), "evidence.request_mutated"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
                    self.run_release(provider)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.release_connection.execute(
                    "SELECT COUNT(*) FROM localization_approvals"
                ).fetchone()[0], 0)

    def test_rejected_quality_receipt_never_reaches_outbox(self):
        self.complete_all()
        provider = EvidenceProvider(wrong_receipt=True)
        with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
            self.run_release(provider)
        self.assertEqual(caught.exception.code, "release.quality.receipt.rejected")
        self.assertEqual(self.release_connection.execute(
            "SELECT COUNT(*) FROM localization_approvals"
        ).fetchone()[0], 0)
        self.assertEqual(self.cms_connection.execute(
            "SELECT COUNT(*) FROM cms_publication_deliveries"
        ).fetchone()[0], 0)

    def test_legal_locale_requires_separate_human_receipt_and_verifier(self):
        self.tearDown()
        self.setUp_legal()
        self.complete_all({"sv-SE": "Genom att fortsätta godkänner du villkoren."})
        missing = EvidenceProvider(include_human=False)
        with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
            self.run_release(missing)
        self.assertEqual(caught.exception.code, "evidence.human_receipt.required")

        provider = EvidenceProvider()
        outcome, quality, human = self.run_release(provider)
        self.assertEqual(outcome.status, "delivery_ready")
        self.assertEqual(len(quality.calls), 1)
        self.assertEqual(len(human.calls), 1)

    def setUp_legal(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.store = RELEASE.LocalizationReleaseStore(self.release_connection, self.queue)
        self.cms_connection = sqlite3.connect(":memory:")
        self.bridge = CMS.WebsiteLocalizationCMSBridge(
            self.cms_connection, self.queue, self.store,
        )
        self.event_authority = CMSAuthority(b"event-key")
        self.publication_authority = CMSAuthority(b"publication-key")
        self.approval_authority = ApprovalAuthority()
        self.event = change_event(targets=("sv-SE",), content_type="legal")
        self.plan = PLANNER.plan_from_mapping(self.event["localization"])
        signature = self.event_authority.sign(CMS._canonical_json(self.event).encode("utf-8"))
        self.bridge.ingest_change(self.event, signature, self.event_authority, now=100)

    def test_result_tampering_blocks_before_evidence_provider(self):
        self.complete_all()
        self.queue_connection.execute(
            "UPDATE localization_jobs SET result_json = '{}' WHERE status = 'succeeded'"
        )
        self.queue_connection.commit()
        provider = EvidenceProvider()
        with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
            self.run_release(provider)
        self.assertEqual(caught.exception.code, "result.failed")
        self.assertEqual(provider.requests, [])


if __name__ == "__main__":
    unittest.main()
