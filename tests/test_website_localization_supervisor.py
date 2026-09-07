from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
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


SUPERVISOR = load(
    "blun_test_website_localization_supervisor",
    ROOT / "integrations" / "website_localization_supervisor.py",
)


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def tick(*, phase="translation", status="succeeded", error_code=None):
    return {
        "schema": SUPERVISOR.TICK_SCHEMA,
        "phase": phase,
        "status": status,
        "event_id": None,
        "plan_id": None,
        "job_id": "job-1" if phase == "translation" else None,
        "target_locale": "fi-FI" if phase == "translation" else None,
        "delivery_id": None,
        "attempt": 1 if phase == "translation" else None,
        "error_code": error_code,
    }


class LocalizationSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "supervisor.sqlite3"
        self.clock = Clock()
        self.calls = 0

        def run_tick():
            self.calls += 1
            return tick()

        self.connection = sqlite3.connect(self.path)
        self.supervisor = SUPERVISOR.LocalizationServiceSupervisor(
            self.connection,
            run_tick,
            worker_id="service-a",
            policy=SUPERVISOR.SupervisorPolicy(
                lease_seconds=10,
                active_delay_seconds=1,
                idle_delay_seconds=4,
                blocked_base_seconds=5,
                blocked_max_seconds=20,
                stop_poll_seconds=1,
            ),
            clock=self.clock,
            token_factory=lambda: "lease-a",
        )

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def second(self, callback=lambda: tick()):
        connection = sqlite3.connect(self.path)
        supervisor = SUPERVISOR.LocalizationServiceSupervisor(
            connection,
            callback,
            worker_id="service-b",
            policy=self.supervisor.policy,
            clock=self.clock,
            token_factory=lambda: "lease-b",
        )
        return connection, supervisor

    def test_success_persists_content_free_status_and_waits(self):
        outcome = self.supervisor.run_once()

        self.assertEqual((outcome.status, outcome.next_tick_at), ("ran", 101.0))
        self.assertEqual(outcome.tick["target_locale"], "fi-FI")
        status = self.supervisor.status()
        self.assertEqual((status.status, status.revision), ("waiting", 2))
        self.assertEqual((status.last_phase, status.last_status), (
            "translation", "succeeded",
        ))
        self.assertFalse(status.lease_active)
        self.assertNotIn("lease-a", json.dumps(status.as_payload()))
        waiting = self.supervisor.run_once()
        self.assertEqual(waiting.status, "waiting")
        self.assertEqual(self.calls, 1)

    def test_only_one_instance_runs_during_a_live_lease(self):
        observed = []
        connection, other = self.second()

        def nested_tick():
            observed.append(other.run_once())
            return tick()

        self.supervisor.tick = nested_tick
        outcome = self.supervisor.run_once()

        self.assertEqual(outcome.status, "ran")
        self.assertEqual(observed[0].status, "leased")
        self.assertEqual(observed[0].next_tick_at, 110.0)
        connection.close()

    def test_expired_crash_lease_is_recovered_transactionally(self):
        token, skipped = self.supervisor._claim(100.0)
        self.assertEqual(token, "lease-a")
        self.assertIsNone(skipped)
        self.clock.value = 111.0
        connection, other = self.second()

        recovered = other.run_once()

        self.assertEqual(recovered.status, "ran")
        self.assertEqual(other.status().last_status, "succeeded")
        with self.assertRaisesRegex(
            SUPERVISOR.LocalizationSupervisorBlocked, "supervisor.lease_lost"
        ):
            self.supervisor._finish("lease-a", tick(), 112.0)
        connection.close()

    def test_blocked_ticks_use_bounded_exponential_backoff(self):
        self.supervisor.tick = lambda: tick(
            phase="delivery", status="blocked", error_code="cms.unavailable",
        )
        delays = []
        for now in (100.0, 105.0, 115.0, 135.0, 155.0):
            self.clock.value = now
            delays.append(self.supervisor.run_once().next_tick_at - now)

        self.assertEqual(delays, [5.0, 10.0, 20.0, 20.0, 20.0])
        self.assertEqual(self.supervisor.status().consecutive_blocked, 5)

    def test_unhandled_exception_is_redacted_and_fail_closed(self):
        def explode():
            raise RuntimeError("customer text: secret offer")

        self.supervisor.tick = explode
        outcome = self.supervisor.run_once()

        self.assertEqual(outcome.tick["error_code"], "supervisor.tick.unhandled")
        encoded = json.dumps(outcome.as_payload())
        self.assertNotIn("secret offer", encoded)
        self.assertEqual(self.supervisor.status().last_error_code,
                         "supervisor.tick.unhandled")

    def test_malformed_tick_is_redacted_instead_of_persisted(self):
        self.supervisor.tick = lambda: {
            "schema": SUPERVISOR.TICK_SCHEMA,
            "phase": "customer prose is not an identifier",
        }

        outcome = self.supervisor.run_once()

        self.assertEqual(outcome.tick["phase"], "supervisor")
        self.assertEqual(outcome.tick["error_code"], "supervisor.tick.unhandled")

    def test_idle_tick_uses_idle_delay(self):
        self.supervisor.tick = lambda: tick(phase="idle", status="idle")

        outcome = self.supervisor.run_once()

        self.assertEqual(outcome.next_tick_at, 104.0)

    def test_run_forever_stops_between_atomic_ticks(self):
        stopped = False
        sleeps = []

        def sleep(delay):
            nonlocal stopped
            sleeps.append(delay)
            self.clock.value += delay
            stopped = True

        status = self.supervisor.run_forever(
            stop_requested=lambda: stopped, sleeper=sleep,
        )

        self.assertEqual(self.calls, 1)
        self.assertEqual(sleeps, [1.0])
        self.assertIn(status.status, {"waiting", "ready"})

    def test_state_tampering_blocks_before_tick(self):
        self.connection.execute("""
            UPDATE localization_service_supervisor
            SET last_error_code = 'customer secret sentence'
        """)
        self.connection.commit()

        with self.assertRaisesRegex(
            SUPERVISOR.LocalizationSupervisorBlocked, "supervisor.state.invalid"
        ):
            self.supervisor.run_once()
        self.assertEqual(self.calls, 0)

    def test_invalid_policy_and_clock_block(self):
        with self.assertRaisesRegex(
            SUPERVISOR.LocalizationSupervisorBlocked, "supervisor.policy.invalid"
        ):
            SUPERVISOR.SupervisorPolicy(lease_seconds=float("inf")).validated()
        with self.assertRaisesRegex(
            SUPERVISOR.LocalizationSupervisorBlocked, "supervisor.clock.invalid"
        ):
            self.supervisor.run_once(now=float("nan"))


if __name__ == "__main__":
    unittest.main()
