from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load(
    "blun_test_website_localization_benchmark_watcher_recovery_runner",
    ROOT / "integrations"
    / "website_localization_benchmark_watcher_recovery_runner.py",
)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class OpenAPI:
    contract_sha256: str = "a" * 64
    openapi_sha256: str = "b" * 64


@dataclass(frozen=True)
class Status:
    checked_at: float = 1500.0
    state: str = "failed"
    rearmable: bool = True
    generation: dict | None = None

    def __post_init__(self):
        if self.generation is None and self.rearmable:
            object.__setattr__(self, "generation", {
                "attempts": 20,
                "failed_at": 1000.0,
                "error_code": "benchmark_client.network",
            })


@dataclass(frozen=True)
class Rearm:
    request_sha256: str
    receipt: dict

    def as_payload(self):
        return dict(self.receipt)


class ClientFailure(RuntimeError):
    benchmark_watcher_control_client_failure = True

    def __init__(self, code, retryable):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class Client:
    origin = "https://control.example"

    def __init__(self, *, openapi=None, statuses=None, rearms=None):
        self.openapi_results = list(openapi or [OpenAPI()])
        self.status_results = list(statuses or [Status()])
        self.rearm_results = list(rearms or [])
        self.calls = []

    @staticmethod
    def _take(results):
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def openapi(self):
        self.calls.append(("openapi",))
        return self._take(self.openapi_results)

    def status(self):
        self.calls.append(("status",))
        return self._take(self.status_results)

    def rearm(self, **kwargs):
        self.calls.append(("rearm", kwargs))
        if self.rearm_results:
            result = self._take(self.rearm_results)
            if callable(result):
                return result(kwargs)
            return result
        request = RUNNER._CLIENT._request(
            kwargs["request_id"], kwargs["expected_attempts"],
            kwargs["expected_failed_at"], kwargs["expected_error_code"],
        )
        request_sha256 = hashlib.sha256(
            RUNNER._CLIENT._canonical(request)
        ).hexdigest()
        return Rearm(request_sha256, {
            "schema": RUNNER._CONTROL.RECEIPT_SCHEMA,
            "request_sha256": request_sha256,
            "previous_state": "failed",
            "previous_attempts": kwargs["expected_attempts"],
            "previous_error_code": kwargs["expected_error_code"],
            "failed_at": kwargs["expected_failed_at"],
            "state": "pending",
            "rearmed_at": 1500.0,
        })


class Event:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.waits.append(delay)
        return self.stopped


class RecoveryRunnerTests(unittest.TestCase):
    def runner(self, client=None, connection=None, **options):
        return RUNNER.DurableBenchmarkWatcherRecoveryRunner(
            connection or sqlite3.connect(":memory:"), client or Client(),
            lease_seconds=10, base_delay_seconds=5,
            max_delay_seconds=20, max_attempts=8, **options,
        )

    def test_three_durable_phases_complete_one_exact_recovery(self):
        client = Client()
        runner = self.runner(client)
        initial = runner.start("operator-remediation-1", now=100)

        discovered = runner.run_once("worker-1", now=100)
        observed = runner.run_once("worker-1", now=100)
        completed = runner.run_once("worker-1", now=100)
        repeated = runner.run_once("worker-2", now=101)
        status = runner.status(now=101)

        self.assertEqual((initial.state, initial.phase), ("pending", "openapi"))
        self.assertEqual((discovered.state, discovered.phase), ("pending", "status"))
        self.assertEqual((observed.state, observed.phase), ("pending", "rearm"))
        self.assertEqual((completed.state, completed.phase), ("succeeded", "rearm"))
        self.assertFalse(repeated.attempted)
        self.assertEqual(status.attempts, 3)
        self.assertEqual(status.contract_sha256, "a" * 64)
        self.assertEqual(status.openapi_sha256, "b" * 64)
        self.assertTrue(status.generation_observed)
        self.assertIsNotNone(status.request_sha256)
        self.assertIsNotNone(status.receipt_sha256)
        self.assertEqual([call[0] for call in client.calls], [
            "openapi", "status", "rearm",
        ])
        rearm = client.calls[2][1]
        self.assertEqual(rearm["expected_attempts"], 20)
        self.assertEqual(rearm["expected_failed_at"], 1000.0)
        self.assertEqual(rearm["expected_error_code"], "benchmark_client.network")
        self.assertRegex(rearm["request_id"], r"^watcher-recovery-[0-9a-f]{64}$")

        row = runner.connection.execute(
            "SELECT operation_sha256, request_id, receipt_sha256 "
            "FROM benchmark_watcher_recovery_runner"
        ).fetchone()
        self.assertNotIn("operator-remediation-1", tuple(row))

    def test_nonfailed_status_finishes_without_rearm(self):
        client = Client(statuses=[Status(
            state="pending", rearmable=False, generation=None,
        )])
        runner = self.runner(client)
        runner.start("operator-remediation-1", now=100)
        runner.run_once("worker", now=100)
        outcome = runner.run_once("worker", now=100)

        self.assertEqual(outcome.state, "not_required")
        self.assertEqual(runner.status(now=100).completed_at, 100.0)
        self.assertEqual([call[0] for call in client.calls], ["openapi", "status"])

    def test_retryable_rearm_reuses_durable_generation_and_identity(self):
        lost = ClientFailure("benchmark_watcher.control_client.network", True)
        client = Client(rearms=[lost])
        runner = self.runner(client)
        runner.start("operator-remediation-1", now=100)
        runner.run_once("worker", now=100)
        runner.run_once("worker", now=100)

        first = runner.run_once("worker", now=100)
        early = runner.run_once("other", now=119)
        client.rearm_results.append(lambda kwargs: Client().rearm(**kwargs))
        recovered = runner.run_once("other", now=120)

        self.assertEqual((first.state, first.next_attempt_at), ("retry_wait", 120.0))
        self.assertFalse(early.attempted)
        self.assertEqual(recovered.state, "succeeded")
        rearm_calls = [call[1] for call in client.calls if call[0] == "rearm"]
        self.assertEqual(len(rearm_calls), 2)
        self.assertEqual(rearm_calls[0], rearm_calls[1])

    def test_crash_leaves_lease_and_restart_recovers_after_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.sqlite3"
            crashing = Client(openapi=[KeyboardInterrupt()])
            first = self.runner(crashing, sqlite3.connect(path))
            first.start("operator-remediation-1", now=100)
            with self.assertRaises(KeyboardInterrupt):
                first.run_once("crashed", now=100)
            first.connection.close()

            replacement_client = Client()
            replacement = self.runner(
                replacement_client, sqlite3.connect(path),
            )
            waiting = replacement.run_once("replacement", now=109)
            recovered = replacement.run_once("replacement", now=110)

            self.assertFalse(waiting.attempted)
            self.assertEqual(recovered.phase, "status")
            self.assertEqual(replacement_client.calls, [("openapi",)])
            self.assertEqual(replacement.status(now=110).attempts, 2)

    def test_active_lease_prevents_parallel_network_access(self):
        client = None
        runner = None

        def inspect_lease():
            current = runner.status(now=100)
            self.assertEqual(current.state, "leased")
            self.assertFalse(current.lease_expired)
            nested = runner.run_once("parallel", now=100)
            self.assertFalse(nested.attempted)
            return OpenAPI()

        class InspectingClient(Client):
            def openapi(self):
                self.calls.append(("openapi",))
                return inspect_lease()

        client = InspectingClient()
        runner = self.runner(client)
        runner.start("operator-remediation-1", now=100)
        runner.run_once("owner", now=100)
        self.assertEqual(client.calls, [("openapi",)])

    def test_terminal_and_exhausted_failures_remain_closed(self):
        terminal = ClientFailure(
            "benchmark_watcher.control_client.openapi_mismatch", False,
        )
        client = Client(openapi=[terminal])
        runner = self.runner(client)
        runner.start("operator-remediation-1", now=100)
        outcome = runner.run_once("worker", now=100)
        self.assertEqual((outcome.state, outcome.error_code), (
            "failed", "benchmark_watcher.control_client.openapi_mismatch",
        ))
        self.assertFalse(runner.run_once("worker", now=200).attempted)

        retryable = ClientFailure("benchmark_watcher.control_client.network", True)
        client2 = Client(rearms=[retryable] * 3)
        runner2 = RUNNER.DurableBenchmarkWatcherRecoveryRunner(
            sqlite3.connect(":memory:"), client2, lease_seconds=10,
            base_delay_seconds=1, max_delay_seconds=1, max_attempts=5,
        )
        runner2.start("operator-remediation-2", now=0)
        runner2.run_once("worker", now=0)
        runner2.run_once("worker", now=0)
        runner2.run_once("worker", now=0)
        runner2.run_once("worker", now=1)
        exhausted = runner2.run_once("worker", now=2)
        self.assertEqual(exhausted.state, "failed")
        self.assertEqual(exhausted.attempt, 5)

    def test_retry_budget_reserves_status_and_rearm_phases(self):
        retryable = ClientFailure(
            "benchmark_watcher.control_client.network", True,
        )
        client = Client(openapi=[retryable, OpenAPI()])
        runner = RUNNER.DurableBenchmarkWatcherRecoveryRunner(
            sqlite3.connect(":memory:"), client, lease_seconds=10,
            base_delay_seconds=1, max_delay_seconds=1, max_attempts=4,
        )
        runner.start("operator-remediation-1", now=0)
        runner.run_once("worker", now=0)
        runner.run_once("worker", now=1)
        runner.run_once("worker", now=1)
        completed = runner.run_once("worker", now=1)

        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(completed.attempt, 4)

    def test_start_is_idempotent_but_changed_operation_conflicts(self):
        runner = self.runner()
        first = runner.start("operator-remediation-1", now=100)
        repeated = runner.start("operator-remediation-1", now=200)
        self.assertEqual(first, repeated)
        with self.assertRaisesRegex(
            RUNNER.BenchmarkWatcherRecoveryRunnerBlocked,
            "benchmark_watcher.recovery_runner.operation_conflict",
        ):
            runner.start("operator-remediation-2", now=200)

    def test_binding_and_state_tampering_fail_closed(self):
        runner = self.runner()
        runner.start("operator-remediation-1", now=100)
        runner.connection.execute(
            "UPDATE benchmark_watcher_recovery_runner "
            "SET operation_sha256 = ? WHERE singleton = 1", ("f" * 64,),
        )
        runner.connection.commit()
        with self.assertRaisesRegex(
            RUNNER.BenchmarkWatcherRecoveryRunnerBlocked,
            "benchmark_watcher.recovery_runner.state_invalid",
        ):
            runner.status(now=100)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.sqlite3"
            original = self.runner(Client(), sqlite3.connect(path))
            original.start("operator-remediation-1", now=100)
            original.connection.close()
            changed = Client()
            changed.origin = "https://other-control.example"
            with self.assertRaisesRegex(
                RUNNER.BenchmarkWatcherRecoveryRunnerBlocked,
                "benchmark_watcher.recovery_runner.state_invalid",
            ):
                self.runner(changed, sqlite3.connect(path))

    def test_run_forever_advances_all_phases_and_stops_at_success(self):
        client = Client()
        runner = self.runner(client)
        runner.start("operator-remediation-1", now=100)
        event = Event()
        times = iter((100.0, 100.0, 100.0))

        final = runner.run_forever(
            "worker", clock=lambda: next(times), stop_event=event,
            maximum_wait_seconds=1,
        )

        self.assertEqual(final.state, "succeeded")
        self.assertEqual(len(event.waits), 2)


if __name__ == "__main__":
    unittest.main()
