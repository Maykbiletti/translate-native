from __future__ import annotations

import hashlib
import hmac
import importlib.util
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


PLANNER = load(
    "blun_test_fallback_planner",
    ROOT / "integrations" / "website_localization.py",
)
RUNNER = load(
    "blun_test_fallback_runner",
    ROOT / "integrations" / "website_localization_runner.py",
)
RELEASE = load(
    "blun_test_fallback_release",
    ROOT / "integrations" / "website_localization_release.py",
)


def make_plan(**overrides):
    values = {
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
        "target_locales": ["sv-SE"],
    }
    values.update(overrides)
    return PLANNER.plan_website_localization(**values)


def completed_result(job, candidate="Bygg ditt företag med BLUN."):
    payload = job.as_payload()
    phases = [
        {
            "phase": phase,
            "request_sha256": hashlib.sha256(f"request-{phase}".encode()).hexdigest(),
            "response_sha256": hashlib.sha256(f"response-{phase}".encode()).hexdigest(),
            "status": "PASS",
        }
        for phase in RELEASE._WORKER.PHASES
    ]
    return {
        "schema": RELEASE._WORKER.RESULT_SCHEMA,
        "worker_schema": RELEASE._WORKER.WORKER_SCHEMA,
        "job_id": payload["job_id"],
        "source_sha256": payload["source"]["sha256"],
        "target_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "source_locale": payload["source"]["locale"],
        "target_locale": payload["target"]["locale"],
        "content_type": payload["content_type"],
        "glossary_version": payload["glossary_version"],
        "policy_version": payload["policy_version"],
        "provider": payload["provider"],
        "software_version": payload["software_version"],
        "candidate": candidate,
        "quality_passes": phases,
        "integrity": {
            "status": "PASS",
            "guard": "translate-native-structure-and-token-gate",
        },
        "human_review_required": False,
        "release_required": True,
    }


class HmacAuthority:
    def __init__(self, key=b"isolated-fallback-test-key"):
        self.key = key

    def sign(self, payload):
        return RELEASE.ApprovalSignature(
            "hmac-sha256-test",
            "fallback-test-key",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


class ReceiptVerifier:
    def verify(self, **values):
        return values["receipt"] == "quality-receipt"


class Clock:
    def __init__(self, start=200):
        self.value = start - 1

    def __call__(self):
        self.value += 1
        return self.value


class WebsiteLocalizationFallbackTests(unittest.TestCase):
    def setUp(self):
        self.release_queue_connection = sqlite3.connect(":memory:")
        self.release_queue = RELEASE._QUEUE.LocalizationQueue(
            self.release_queue_connection,
        )
        self.release_connection = sqlite3.connect(":memory:")
        self.store = RELEASE.LocalizationReleaseStore(
            self.release_connection,
            self.release_queue,
        )
        self.authority = HmacAuthority()
        self.runner_connection = sqlite3.connect(":memory:")
        self.runner_queue = RUNNER._QUEUE.LocalizationQueue(
            self.runner_connection,
        )

    def tearDown(self):
        self.runner_connection.close()
        self.release_connection.close()
        self.release_queue_connection.close()

    def approve(self, plan, *, ttl_seconds=1000):
        self.release_queue.enqueue_plan(plan, now=50)
        claim = self.release_queue.claim("release-worker", now=60, lease_seconds=30)
        self.release_queue.complete(
            claim,
            completed_result(plan.jobs[0]),
            now=61,
        )
        self.store.approve(
            plan,
            plan.jobs[0].job_id,
            "quality-receipt",
            ReceiptVerifier(),
            self.authority,
            now=100,
            ttl_seconds=ttl_seconds,
        )

    def run_with_cache(self, plan, provider_resolver, assets_resolver):
        self.runner_queue.enqueue_plan(plan, max_attempts=3, now=150)
        return RUNNER.run_next_localization_job(
            self.runner_queue,
            "fallback-worker",
            provider_resolver,
            assets_resolver,
            clock=Clock(),
            lease_seconds=30,
            result_cache=self.store.verified_result_cache(self.authority),
        )

    def test_exact_signed_result_restores_without_provider_or_assets(self):
        plan = make_plan()
        self.approve(plan)
        calls = []
        outcome = self.run_with_cache(
            plan,
            lambda payload: calls.append("provider"),
            lambda payload: calls.append("assets"),
        )
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.result_origin, "translation_memory")
        self.assertEqual(calls, [])
        self.assertEqual(
            self.runner_queue.result(plan.jobs[0].job_id)["candidate"],
            "Bygg ditt företag med BLUN.",
        )

    def test_changed_policy_is_a_miss_and_never_reuses_stale_text(self):
        original = make_plan()
        self.approve(original)
        changed = make_plan(policy_version="native-web-2")
        calls = []

        def provider(payload):
            calls.append("provider")
            raise RUNNER.RunnerDependencyFailed("provider.unavailable", retryable=True)

        outcome = self.run_with_cache(changed, provider, lambda payload: calls.append("assets"))
        self.assertEqual(outcome.status, "retry_wait")
        self.assertEqual(outcome.error_code, "runner.dependency.provider.unavailable")
        self.assertEqual(calls, ["assets", "provider"])
        self.assertIsNone(self.runner_queue.status(changed.jobs[0].job_id).result_sha256)

    def test_expired_approval_is_a_miss_and_provider_must_supply_new_text(self):
        plan = make_plan()
        self.approve(plan, ttl_seconds=50)
        calls = []

        def provider(payload):
            calls.append("provider")
            raise RUNNER.RunnerDependencyFailed("provider.offline", retryable=True)

        outcome = self.run_with_cache(plan, provider, lambda payload: calls.append("assets"))
        self.assertEqual(outcome.error_code, "runner.dependency.provider.offline")
        self.assertEqual(calls, ["assets", "provider"])
        self.assertIsNone(outcome.result_sha256)

    def test_tampered_cache_blocks_without_calling_provider(self):
        plan = make_plan()
        self.approve(plan)
        self.release_connection.execute(
            "UPDATE localization_approvals SET result_json = '{}'",
        )
        self.release_connection.commit()
        calls = []
        outcome = self.run_with_cache(
            plan,
            lambda payload: calls.append("provider"),
            lambda payload: calls.append("assets"),
        )
        self.assertEqual(outcome.status, "retry_wait")
        self.assertEqual(outcome.error_code, "runner.cache.unexpected")
        self.assertEqual(calls, [])
        self.assertIsNone(outcome.result_sha256)


if __name__ == "__main__":
    unittest.main()
