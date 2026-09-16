from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


WATCHER = load(
    "blun_test_website_localization_benchmark_watcher",
    ROOT / "integrations" / "website_localization_benchmark_watcher.py",
)

CAMPAIGN_ID = "benchmark-campaign-" + "a" * 64
POLICY_SHA256 = "b" * 64
SUITE_SHA256 = "c" * 64


def campaign(*, valid_until=1000.0):
    return {
        "campaign_id": CAMPAIGN_ID,
        "policy_sha256": POLICY_SHA256,
        "suite_sha256": SUITE_SHA256,
        "valid_until": valid_until,
        "work_count": 2,
        "counts": {
            "pending": 0, "leased": 0, "retry_wait": 0,
            "succeeded": 2, "failed": 0,
        },
        "error_counts": {},
        "complete": True,
        "blocked": False,
        "report_finalization": {
            "status": "succeeded", "attempt": 1, "max_attempts": 3,
            "next_attempt_at": 100.0, "error_code": None,
        },
    }


def report(*, status="PASS", secret="private-source-text"):
    allowed = status == "PASS"
    return {
        "valid_until": 1000.0,
        "status": status,
        "superiority_claim_allowed": allowed,
        "claim_block_reasons": (
            [] if allowed else ["configured_locale_evaluation_failed"]
        ),
        "locales": [{"locale": "mt-MT"}, {"locale": "fi-FI"}],
        "private_fixture": {"source_text": secret},
    }


class Snapshot:
    def __init__(self, value=None, campaign_value=None, digest=None):
        self.report = report() if value is None else value
        self.campaign = campaign() if campaign_value is None else campaign_value
        self.report_sha256 = digest or hashlib.sha256(
            WATCHER._CLIENT._HTTP._canonical_json(self.report)
        ).hexdigest()

    def as_payload(self):
        return self.report


class Client:
    def __init__(self, outcomes, **bindings):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.expected_campaign_id = bindings.get("campaign_id", CAMPAIGN_ID)
        self.expected_policy_sha256 = bindings.get("policy_sha256", POLICY_SHA256)
        self.expected_suite_sha256 = bindings.get("suite_sha256", SUITE_SHA256)

    def report(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome


class ForeignClientFailure(RuntimeError):
    benchmark_client_failure = True

    def __init__(self, code, retryable):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class StopEvent:
    def __init__(self, on_wait=None):
        self.stopped = False
        self.on_wait = on_wait
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait is not None:
            self.on_wait(seconds)
        return self.stopped


class DurableBenchmarkReportWatcherTests(unittest.TestCase):
    def watcher(self, client, connection=None, **options):
        configuration = {
            "lease_seconds": 10,
            "base_delay_seconds": 5,
            "max_delay_seconds": 20,
            "max_attempts": 3,
        }
        configuration.update(options)
        return WATCHER.DurableBenchmarkReportWatcher(
            connection or sqlite3.connect(":memory:"), client,
            **configuration,
        )

    def test_success_persists_only_content_free_summary_and_is_idempotent(self):
        client = Client([Snapshot()])
        watcher = self.watcher(client)

        outcome = watcher.run_once("consumer-1", now=100)
        repeated = watcher.run_once("consumer-2", now=101)
        status = watcher.status(now=101)
        stored = tuple(watcher.connection.execute(
            "SELECT * FROM benchmark_report_watcher"
        ).fetchone())

        self.assertEqual((outcome.state, outcome.report_status), (
            "succeeded", "PASS",
        ))
        self.assertFalse(repeated.attempted)
        self.assertEqual(client.calls, 1)
        self.assertTrue(status.superiority_claim_allowed)
        self.assertEqual(status.locale_count, 2)
        self.assertRegex(status.report_sha256, r"^[0-9a-f]{64}$")
        self.assertNotIn("private-source-text", stored)
        self.assertNotIn("mt-MT", stored)

    def test_restart_preserves_final_result_without_another_remote_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark-watcher.sqlite3"
            first_client = Client([Snapshot()])
            first = self.watcher(first_client, sqlite3.connect(path))
            first.run_once("consumer", now=100)
            expected_sha256 = first.status(now=100).report_sha256
            first.connection.close()

            replacement_client = Client([])
            replacement = self.watcher(
                replacement_client, sqlite3.connect(path),
            )
            outcome = replacement.run_once("replacement", now=200)

            self.assertFalse(outcome.attempted)
            self.assertEqual(outcome.state, "succeeded")
            self.assertEqual(
                replacement.status(now=200).report_sha256,
                expected_sha256,
            )
            self.assertEqual(replacement_client.calls, 0)

    def test_valid_block_report_is_fetched_successfully_but_health_blocks_claim(self):
        value = report(status="BLOCK")
        watcher = self.watcher(Client([Snapshot(value)]))

        outcome = watcher.run_once("consumer", now=100)
        health = watcher.health(now=101).as_payload()

        self.assertEqual((outcome.state, outcome.report_status), (
            "succeeded", "BLOCK",
        ))
        self.assertFalse(outcome.superiority_claim_allowed)
        self.assertEqual((health["status"], health["ready"]), (
            "blocked", True,
        ))
        self.assertEqual(health["watcher_reasons"], [
            "benchmark_watcher.report_blocked",
        ])
        self.assertEqual(health["report"]["block_reasons"], [
            "configured_locale_evaluation_failed",
        ])

    def test_retryable_failures_back_off_and_stop_at_attempt_ceiling(self):
        failures = [
            ForeignClientFailure(
                "benchmark_client.campaign_incomplete", True,
            ) for _ in range(3)
        ]
        client = Client(failures)
        watcher = self.watcher(client)

        first = watcher.run_once("consumer", now=100)
        early = watcher.run_once("consumer", now=104)
        second = watcher.run_once("consumer", now=105)
        third = watcher.run_once("consumer", now=115)
        after = watcher.run_once("consumer", now=999)

        self.assertEqual((first.state, first.next_attempt_at), (
            "retry_wait", 105.0,
        ))
        self.assertFalse(early.attempted)
        self.assertEqual((second.state, second.next_attempt_at), (
            "retry_wait", 115.0,
        ))
        self.assertEqual((third.state, third.attempt), ("failed", 3))
        self.assertFalse(after.attempted)
        self.assertEqual(client.calls, 3)

    def test_nonretryable_client_failure_is_terminal_immediately(self):
        client = Client([
            ForeignClientFailure("benchmark_client.policy_mismatch", False)
        ])
        watcher = self.watcher(client)

        outcome = watcher.run_once("consumer", now=100)

        self.assertEqual((outcome.state, outcome.error_code), (
            "failed", "benchmark_client.policy_mismatch",
        ))

    def test_expired_lease_is_recovered_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark-watcher.sqlite3"
            first = self.watcher(Client([Snapshot()]), sqlite3.connect(path))
            first.connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'leased', lease_owner = 'crashed',
                    lease_token = 'lost', lease_expires_at = 110,
                    updated_at = 100
            """)
            first.connection.commit()
            first.connection.close()

            client = Client([Snapshot()])
            second = self.watcher(client, sqlite3.connect(path))

            active = second.run_once("replacement", now=109)
            recovered = second.run_once("replacement", now=111)

            self.assertFalse(active.attempted)
            self.assertEqual((recovered.state, recovered.attempt), (
                "succeeded", 1,
            ))
            self.assertEqual(client.calls, 1)

    def test_expired_final_attempt_becomes_terminal_without_another_call(self):
        client = Client([Snapshot()])
        watcher = self.watcher(client)
        watcher.connection.execute("""
            UPDATE benchmark_report_watcher
            SET state = 'leased', attempts = 3, lease_owner = 'crashed',
                lease_token = 'lost', lease_expires_at = 110,
                updated_at = 100
        """)
        watcher.connection.commit()

        outcome = watcher.run_once("replacement", now=111)

        self.assertEqual((outcome.attempted, outcome.state), (False, "failed"))
        self.assertEqual(
            outcome.error_code, "benchmark_watcher.attempts_exhausted",
        )
        self.assertEqual(client.calls, 0)

    def test_lost_lease_cannot_complete_a_successful_remote_read(self):
        connection = sqlite3.connect(":memory:")
        watcher = None

        def steal_lease():
            watcher.connection.execute("""
                UPDATE benchmark_report_watcher
                SET lease_owner = 'replacement', lease_token = 'replacement'
                WHERE singleton = 1
            """)
            watcher.connection.commit()
            return Snapshot()

        watcher = self.watcher(Client([steal_lease]), connection)

        with self.assertRaises(WATCHER.BenchmarkWatcherBlocked) as caught:
            watcher.run_once("original", now=100)

        self.assertEqual(str(caught.exception), "benchmark_watcher.lease_lost")
        self.assertIsNone(watcher.status(now=100).report_sha256)

    def test_binding_or_configuration_drift_blocks_restart_before_client_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark-watcher.sqlite3"
            first = self.watcher(Client([Snapshot()]), sqlite3.connect(path))
            first.connection.close()
            scenarios = (
                (Client([], policy_sha256="d" * 64), {}),
                (Client([]), {"lease_seconds": 11}),
            )
            for changed, options in scenarios:
                with self.subTest(options=options):
                    with self.assertRaises(
                        WATCHER.BenchmarkWatcherBlocked
                    ) as caught:
                        self.watcher(
                            changed, sqlite3.connect(path), **options,
                        )
                    self.assertEqual(
                        str(caught.exception),
                        "benchmark_watcher.state_invalid",
                    )
                    self.assertEqual(changed.calls, 0)

    def test_tampered_schema_and_state_fail_closed_without_client_call(self):
        client = Client([Snapshot()])
        watcher = self.watcher(client)
        watcher.connection.execute(
            "UPDATE benchmark_report_watcher SET block_reasons_json = ?",
            ('["z_reason","a_reason"]',),
        )
        watcher.connection.commit()

        with self.assertRaises(WATCHER.BenchmarkWatcherBlocked) as caught:
            watcher.run_once("consumer", now=100)

        self.assertEqual(str(caught.exception), "benchmark_watcher.state_invalid")
        self.assertEqual(client.calls, 0)

    def test_invalid_client_result_is_terminal_without_persisting_content(self):
        invalid = report(secret="must-not-be-stored")
        invalid["superiority_claim_allowed"] = False
        watcher = self.watcher(Client([Snapshot(invalid)]))

        outcome = watcher.run_once("consumer", now=100)
        stored = tuple(watcher.connection.execute(
            "SELECT * FROM benchmark_report_watcher"
        ).fetchone())

        self.assertEqual((outcome.state, outcome.error_code), (
            "failed", "benchmark_watcher.report_invalid",
        ))
        self.assertNotIn("must-not-be-stored", stored)

    def test_operator_rearm_recovers_failure_but_cannot_replace_final_report(self):
        client = Client([
            ForeignClientFailure("benchmark_client.policy_mismatch", False),
            Snapshot(),
        ])
        watcher = self.watcher(client)
        watcher.run_once("consumer", now=100)

        rearmed = watcher.rearm(now=200)
        recovered = watcher.run_once("consumer", now=200)

        self.assertEqual((rearmed.state, rearmed.attempts), ("pending", 0))
        self.assertEqual(recovered.state, "succeeded")
        with self.assertRaises(WATCHER.BenchmarkWatcherBlocked) as caught:
            watcher.rearm(now=300)
        self.assertEqual(str(caught.exception), "benchmark_watcher.result_final")

    def test_health_exposes_due_retry_failure_and_no_private_binding_values(self):
        client = Client([
            ForeignClientFailure("benchmark_client.network", True),
        ])
        watcher = self.watcher(client)

        initial = watcher.health(now=100).as_payload()
        watcher.run_once("consumer", now=100)
        retrying = watcher.health(now=101).as_payload()
        watcher.connection.execute("""
            UPDATE benchmark_report_watcher
            SET state = 'failed', next_attempt_at = 101
            WHERE singleton = 1
        """)
        watcher.connection.commit()
        failed = watcher.health(now=101).as_payload()

        self.assertEqual((initial["status"], initial["due"]), (
            "degraded", True,
        ))
        self.assertEqual(initial["watcher_reasons"], [
            "benchmark_watcher.pending",
        ])
        self.assertEqual(retrying["watcher_reasons"], [
            "benchmark_watcher.retry_wait",
        ])
        self.assertEqual((failed["status"], failed["watcher_reasons"]), (
            "blocked", ["benchmark_watcher.failed"],
        ))
        rendered = str(failed)
        self.assertNotIn(CAMPAIGN_ID, rendered)
        self.assertNotIn(POLICY_SHA256, rendered)
        self.assertNotIn(SUITE_SHA256, rendered)

    def test_run_forever_waits_for_retry_then_returns_final_status(self):
        client = Client([
            ForeignClientFailure("benchmark_client.network", True),
            Snapshot(),
        ])
        watcher = self.watcher(client)
        current = [100.0]

        def advance(seconds):
            current[0] += seconds

        stop = StopEvent(on_wait=advance)

        result = watcher.run_forever(
            "consumer-loop", clock=lambda: current[0], stop_event=stop,
        )

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(stop.waits, [5.0])
        self.assertEqual(client.calls, 2)


if __name__ == "__main__":
    unittest.main()
