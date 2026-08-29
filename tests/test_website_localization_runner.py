from __future__ import annotations

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


PLANNER = load(
    "blun_test_runner_planner",
    ROOT / "integrations" / "website_localization.py",
)
RUNNER = load(
    "blun_test_website_localization_runner",
    ROOT / "integrations" / "website_localization_runner.py",
)
QUEUE = RUNNER._QUEUE
WORKER = RUNNER._WORKER


def plan(targets=("sv-SE",)):
    return PLANNER.plan_website_localization(
        source_id="homepage.hero",
        source_revision="cms-184",
        source_text="Build your business with BLUN.",
        source_locale="en-IE",
        content_type="headline",
        glossary_version="blun-glossary-3",
        policy_version="native-web-1",
        provider_id="customer-llm",
        model_id="king",
        model_version="2026-08-29",
        software_version="6.43.0-dev",
        target_locales=list(targets),
    )


def assets():
    return WORKER.LocalizationAssets(
        glossary_version="blun-glossary-3",
        policy_version="native-web-1",
        audience="European small-business owners",
        tone_profile="Confident, warm, concise, and never inflated",
        protected_terms=("BLUN",),
    )


def candidate(locale, text):
    return {
        "schema": WORKER.CANDIDATE_SCHEMA,
        "phase": "transcreation",
        "locale": locale,
        "candidate": text,
    }


def review(locale, phase, status="PASS", findings=None):
    return {
        "schema": WORKER.REVIEW_SCHEMA,
        "phase": phase,
        "locale": locale,
        "status": status,
        "blocking_defects": [] if findings is None else findings,
        "major_defects": [],
    }


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def invoke(self, request):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def successful_provider(locale="sv-SE", text="Bygg ditt företag med BLUN."):
    return ScriptedProvider([
        candidate(locale, text),
        review(locale, "target_native"),
        review(locale, "source_fidelity"),
    ])


class IncrementingClock:
    def __init__(self, start=100.0, step=1.0):
        self.value = start - step
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


class WebsiteLocalizationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.connection)

    def tearDown(self):
        self.connection.close()

    def execute(self, provider, **overrides):
        values = {
            "clock": IncrementingClock(),
            "lease_seconds": 10,
            "retry_base_seconds": 5,
            "retry_max_seconds": 60,
        }
        values.update(overrides)
        return RUNNER.run_next_localization_job(
            self.queue,
            "worker-a",
            lambda payload: provider,
            lambda payload: assets(),
            **values,
        )

    def test_claims_runs_and_completes_exactly_one_locale(self):
        current = plan(("sv-SE", "de-AT"))
        self.queue.enqueue_plan(current, now=90)
        outcome = self.execute(successful_provider("de-AT", "Baue dein Unternehmen mit BLUN auf."))
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.target_locale, "de-AT")
        self.assertIsNotNone(outcome.result_sha256)
        self.assertEqual(self.queue.plan_counts(current.plan_id), {
            "pending": 1,
            "leased": 0,
            "retry_wait": 0,
            "succeeded": 1,
            "failed": 0,
        })

    def test_retryable_provider_failure_uses_bounded_exponential_delay(self):
        current = plan()
        self.queue.enqueue_plan(current, max_attempts=3, now=90)
        outcome = self.execute(ScriptedProvider([
            WORKER.ProviderCallFailed("timeout", retryable=True),
        ]))
        self.assertEqual(outcome.status, "retry_wait")
        self.assertEqual(outcome.error_code, "provider.timeout")
        self.assertEqual(outcome.next_attempt_at, 107)
        self.assertIsNone(outcome.result_sha256)

    def test_retryable_failure_becomes_terminal_at_attempt_ceiling(self):
        current = plan()
        self.queue.enqueue_plan(current, max_attempts=1, now=90)
        outcome = self.execute(ScriptedProvider([
            WORKER.ProviderCallFailed("unavailable", retryable=True),
        ]))
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.attempt, 1)
        self.assertEqual(outcome.error_code, "provider.unavailable")

    def test_permanent_asset_resolution_failure_is_content_free(self):
        current = plan()
        self.queue.enqueue_plan(current, now=90)

        def broken_assets(payload):
            raise RUNNER.RunnerDependencyFailed("assets.missing", retryable=False)

        outcome = RUNNER.run_next_localization_job(
            self.queue,
            "worker-a",
            lambda payload: successful_provider(),
            broken_assets,
            clock=IncrementingClock(),
            lease_seconds=10,
        )
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "runner.dependency.assets.missing")
        self.assertIsNone(outcome.error_detail_hash)

    def test_unexpected_resolver_exception_never_persists_exception_text(self):
        current = plan()
        self.queue.enqueue_plan(current, now=90)

        def broken_provider(payload):
            raise RuntimeError("Build your secret acquisition plan.")

        outcome = RUNNER.run_next_localization_job(
            self.queue,
            "worker-a",
            broken_provider,
            lambda payload: assets(),
            clock=IncrementingClock(),
            lease_seconds=10,
        )
        self.assertEqual(outcome.error_code, "runner.dependency.unexpected")
        rows = self.connection.execute(
            "SELECT last_error_code, last_error_detail_hash FROM localization_jobs"
        ).fetchall()
        self.assertNotIn(
            "secret acquisition",
            json.dumps([tuple(row) for row in rows]),
        )

    def test_review_findings_store_only_an_opaque_hash(self):
        current = plan()
        self.queue.enqueue_plan(current, now=90)
        provider = ScriptedProvider([
            candidate("sv-SE", "Bygg ditt företag med BLUN."),
            review("sv-SE", "target_native", "FAIL", [{
                "class": "nativeness",
                "excerpt": "hemligt kunduttryck",
                "reason": "ordföljden känns översatt",
            }]),
        ])
        outcome = self.execute(provider)
        self.assertEqual(outcome.status, "retry_wait")
        self.assertEqual(outcome.error_code, "review.target_native.failed")
        self.assertIsNotNone(outcome.error_detail_hash)
        database = " ".join(
            str(value)
            for row in self.connection.execute(
                "SELECT last_error_code, last_error_detail_hash FROM localization_jobs"
            )
            for value in row
        )
        self.assertNotIn("hemligt", database)
        self.assertNotIn("översatt", database)

    def test_partial_locale_failure_does_not_discard_successful_locale(self):
        current = plan(("de-AT", "sv-SE"))
        self.queue.enqueue_plan(current, max_attempts=1, now=90)

        def providers(payload):
            if payload["target"]["locale"] == "de-AT":
                return ScriptedProvider([
                    WORKER.ProviderCallFailed("policy_block", retryable=False),
                ])
            return successful_provider()

        first = RUNNER.run_next_localization_job(
            self.queue,
            "worker-a",
            providers,
            lambda payload: assets(),
            clock=IncrementingClock(),
            lease_seconds=10,
        )
        second = RUNNER.run_next_localization_job(
            self.queue,
            "worker-b",
            providers,
            lambda payload: assets(),
            clock=IncrementingClock(start=200),
            lease_seconds=10,
        )
        self.assertEqual((first.status, second.status), ("failed", "succeeded"))
        self.assertEqual(self.queue.plan_counts(current.plan_id)["failed"], 1)
        self.assertEqual(self.queue.plan_counts(current.plan_id)["succeeded"], 1)

    def test_expired_lease_cannot_record_provider_output(self):
        current = plan()
        self.queue.enqueue_plan(current, now=90)
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.execute(
                successful_provider(),
                clock=IncrementingClock(start=100, step=11),
                lease_seconds=10,
            )
        status = self.queue.status(current.jobs[0].job_id)
        self.assertEqual(status.status, "leased")
        self.assertIsNone(status.result_sha256)

    def test_empty_queue_returns_none_without_resolving_dependencies(self):
        called = []
        outcome = RUNNER.run_next_localization_job(
            self.queue,
            "worker-a",
            lambda payload: called.append("provider"),
            lambda payload: called.append("assets"),
            clock=IncrementingClock(),
        )
        self.assertIsNone(outcome)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
