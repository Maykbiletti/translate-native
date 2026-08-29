from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
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


PLANNER = load(
    "blun_test_queue_planner",
    ROOT / "integrations" / "website_localization.py",
)
QUEUE = load(
    "blun_test_website_localization_queue",
    ROOT / "integrations" / "website_localization_queue.py",
)


def request(**overrides):
    value = {
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
    }
    value.update(overrides)
    return value


def plan(targets=("de-AT", "sv-SE")):
    return PLANNER.plan_website_localization(
        **request(target_locales=list(targets)),
    )


class FakeJob:
    def __init__(self, job_id, payload):
        self.job_id = job_id
        self._payload = payload

    def as_payload(self):
        return self._payload


class FakePlan:
    def __init__(self, plan_id, jobs):
        self.plan_id = plan_id
        self.jobs = tuple(jobs)


class WebsiteLocalizationQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_plan_enqueue_is_atomic_visible_and_idempotent(self) -> None:
        current = plan()
        self.assertEqual(self.queue.enqueue_plan(current, now=100), 2)
        self.assertEqual(self.queue.enqueue_plan(current, now=200), 0)
        self.assertEqual(self.queue.plan_counts(current.plan_id), {
            "pending": 2,
            "leased": 0,
            "retry_wait": 0,
            "succeeded": 0,
            "failed": 0,
        })

    def test_schema_creation_rechecks_version_after_acquiring_lock(self) -> None:
        current = plan(("de-AT",))
        self.queue.enqueue_plan(current, now=100)
        self.queue._create_schema()
        self.assertEqual(self.queue.plan_counts(current.plan_id)["pending"], 1)

    def test_idempotency_collision_rolls_back_the_complete_plan(self) -> None:
        single = plan(("de-AT",))
        self.queue.enqueue_plan(single, now=90)
        current = plan(("de-AT", "sv-SE"))
        original = current.jobs[0]
        payload = original.as_payload()
        payload["source"]["text"] = "Different source under the same job ID."
        colliding = FakePlan(
            current.plan_id,
            [FakeJob(original.job_id, payload), current.jobs[1]],
        )
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.queue.enqueue_plan(colliding, now=100)
        self.assertEqual(self.queue.plan_counts(current.plan_id)["pending"], 0)

    def test_identical_job_can_belong_to_multiple_plans(self) -> None:
        single = plan(("de-AT",))
        combined = plan(("de-AT", "sv-SE"))
        self.assertEqual(single.jobs[0].job_id, combined.jobs[0].job_id)
        self.assertEqual(self.queue.enqueue_plan(single, now=90), 1)
        self.assertEqual(self.queue.enqueue_plan(combined, now=100), 1)
        status = self.queue.status(single.jobs[0].job_id)
        self.assertEqual(status.plan_ids, tuple(sorted((single.plan_id, combined.plan_id))))
        self.assertEqual(self.queue.plan_counts(single.plan_id)["pending"], 1)
        self.assertEqual(self.queue.plan_counts(combined.plan_id)["pending"], 2)

    def test_claims_are_exclusive_attempt_bound_and_ordered(self) -> None:
        current = plan(("sv-SE", "de-AT"))
        self.queue.enqueue_plan(current, now=100)
        first = self.queue.claim("worker-a", now=100, lease_seconds=10)
        second = self.queue.claim("worker-b", now=100, lease_seconds=10)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.target_locale, "de-AT")
        self.assertEqual(second.target_locale, "sv-SE")
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertIsNone(self.queue.claim("worker-c", now=100, lease_seconds=10))

    def test_completion_requires_exact_live_lease_and_hashes_result(self) -> None:
        current = plan(("sv-SE",))
        self.queue.enqueue_plan(current, now=100)
        claim = self.queue.claim("worker-a", now=100, lease_seconds=10)
        forged = replace(claim, lease_token="forged-token")
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.queue.complete(forged, {"candidate": "Bygg ditt företag med BLUN."}, now=101)
        status = self.queue.complete(
            claim,
            {"candidate": "Bygg ditt företag med BLUN."},
            now=101,
        )
        self.assertEqual(status.status, "succeeded")
        self.assertIsNotNone(status.result_sha256)
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.queue.complete(claim, {"candidate": "Replay"}, now=102)

    def test_retry_waits_until_due_and_stops_at_attempt_limit(self) -> None:
        current = plan(("sv-SE",))
        self.queue.enqueue_plan(current, max_attempts=2, now=100)
        first = self.queue.claim("worker-a", now=100, lease_seconds=10)
        status = self.queue.retry(
            first,
            "provider.timeout",
            error_detail="Provider did not answer.",
            delay_seconds=5,
            now=101,
        )
        self.assertEqual(status.status, "retry_wait")
        self.assertEqual(status.next_attempt_at, 106)
        self.assertIsNotNone(status.last_error_detail_hash)
        self.assertIsNone(self.queue.claim("worker-b", now=105, lease_seconds=10))
        second = self.queue.claim("worker-b", now=106, lease_seconds=10)
        self.assertEqual(second.attempt, 2)
        terminal = self.queue.retry(second, "provider.timeout", now=107)
        self.assertEqual(terminal.status, "failed")
        self.assertIsNone(self.queue.claim("worker-c", now=200, lease_seconds=10))

    def test_expired_lease_recovers_after_crash_and_stale_claim_cannot_finish(self) -> None:
        current = plan(("de-AT",))
        self.queue.enqueue_plan(current, max_attempts=2, now=100)
        stale = self.queue.claim("worker-a", now=100, lease_seconds=5)
        recovered = self.queue.claim("worker-b", now=105, lease_seconds=5)
        self.assertEqual(recovered.job_id, stale.job_id)
        self.assertEqual(recovered.attempt, 2)
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.queue.complete(stale, {"candidate": "Alt"}, now=106)
        self.queue.complete(recovered, {"candidate": "Neu"}, now=106)

    def test_expired_final_attempt_becomes_failed(self) -> None:
        current = plan(("de-AT",))
        self.queue.enqueue_plan(current, max_attempts=1, now=100)
        claim = self.queue.claim("worker-a", now=100, lease_seconds=5)
        self.assertIsNone(self.queue.claim("worker-b", now=105, lease_seconds=5))
        status = self.queue.status(claim.job_id)
        self.assertEqual(status.status, "failed")
        self.assertEqual(status.last_error_code, "lease_expired")

    def test_renewal_extends_only_the_exact_live_claim(self) -> None:
        current = plan(("sv-SE",))
        self.queue.enqueue_plan(current, now=100)
        claim = self.queue.claim("worker-a", now=100, lease_seconds=5)
        renewed = self.queue.renew(claim, now=104, lease_seconds=10)
        self.assertEqual(renewed.lease_expires_at, 114)
        self.queue.complete(renewed, {"candidate": "Klar"}, now=113)
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.queue.renew(claim, now=115, lease_seconds=10)

    def test_permanent_failure_is_text_free_but_diagnostic(self) -> None:
        current = plan(("de-AT",))
        self.queue.enqueue_plan(current, now=100)
        claim = self.queue.claim("worker-a", now=100, lease_seconds=10)
        status = self.queue.fail(
            claim,
            "structure.invalid",
            error_detail="Secret customer source text",
            now=101,
        )
        self.assertEqual(status.status, "failed")
        self.assertEqual(status.last_error_code, "structure.invalid")
        self.assertNotIn("Secret customer source text", repr(status))
        database_bytes = " ".join(
            str(value)
            for row in self.connection.execute(
                "SELECT last_error_code, last_error_detail_hash FROM localization_jobs"
            )
            for value in row
        )
        self.assertNotIn("Secret customer source text", database_bytes)

    def test_two_connections_claim_different_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            first_connection = sqlite3.connect(path)
            second_connection = sqlite3.connect(path)
            try:
                first = QUEUE.LocalizationQueue(first_connection)
                second = QUEUE.LocalizationQueue(second_connection)
                current = plan()
                first.enqueue_plan(current, now=100)
                first_claim = first.claim("worker-a", now=100, lease_seconds=10)
                second_claim = second.claim("worker-b", now=100, lease_seconds=10)
                self.assertNotEqual(first_claim.job_id, second_claim.job_id)
            finally:
                first_connection.close()
                second_connection.close()

    def test_altered_schema_and_payload_fail_closed(self) -> None:
        changed = sqlite3.connect(":memory:")
        changed.execute("PRAGMA user_version = 99")
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            QUEUE.LocalizationQueue(changed)
        changed.close()

        current = plan(("sv-SE",))
        original = current.jobs[0]
        payload = original.as_payload()
        payload["release_required"] = False
        unsafe = FakePlan(current.plan_id, [FakeJob(original.job_id, payload)])
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.queue.enqueue_plan(unsafe, now=100)

    def test_payload_tampering_is_failed_before_worker_receives_it(self) -> None:
        current = plan(("sv-SE",))
        self.queue.enqueue_plan(current, now=100)
        self.connection.execute(
            "UPDATE localization_jobs SET payload_json = '{}' WHERE job_id = ?",
            (current.jobs[0].job_id,),
        )
        self.connection.commit()
        with self.assertRaises(QUEUE.LocalizationQueueBlocked):
            self.queue.claim("worker-a", now=100, lease_seconds=10)
        status = self.queue.status(current.jobs[0].job_id)
        self.assertEqual(status.status, "failed")
        self.assertEqual(status.last_error_code, "payload_integrity")


if __name__ == "__main__":
    unittest.main()
