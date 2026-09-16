from __future__ import annotations

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


MONITOR = load(
    "blun_test_website_localization_health_monitor",
    ROOT / "integrations" / "website_localization_health_monitor.py",
)


def report(*, status="healthy", checked_at=100.0):
    reason = None if status == "healthy" else "release.policy_unavailable"
    return {
        "schema": MONITOR._CLIENT._HTTP.HEALTH_SCHEMA,
        "checked_at": checked_at,
        "status": status,
        "components": [{
            "component": "release",
            "status": status,
            "reasons": [] if reason is None else [reason],
            "counts": {"website_versions": 1},
        }],
        "providers": [],
        "website_versions": [{
            "event_id": "private-event",
            "site_id": "private-site",
            "website_version": "private-version",
            "plan_id": "private-plan",
            "status": "ready" if status == "healthy" else "awaiting_approval",
            "required_locales": 1,
            "approved_locales": 1 if status == "healthy" else 0,
            "queue_counts": {"succeeded": 1},
            "blocked_locales": (
                [] if status == "healthy" else [
                    ["fi-FI", "publication.evidence.policy_unavailable"]
                ]
            ),
        }],
    }


class Snapshot:
    def __init__(self, payload):
        self.payload = payload

    def as_payload(self):
        return self.payload


class Client:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def read(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Snapshot(outcome)


class StopEvent:
    def __init__(self, *, stopped=False, on_wait=None):
        self.stopped = stopped
        self.on_wait = on_wait
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait is not None:
            self.on_wait(self)
        else:
            self.stopped = True
        return self.stopped


class DurableWebsiteLocalizationHealthMonitorTests(unittest.TestCase):
    def monitor(self, client, connection=None, **options):
        return MONITOR.DurableWebsiteLocalizationHealthMonitor(
            connection or sqlite3.connect(":memory:"), client,
            poll_interval_seconds=30, lease_seconds=10,
            base_delay_seconds=5, max_delay_seconds=20,
            max_consecutive_failures=3, **options,
        )

    def test_persists_only_content_free_summary_and_schedules_next_poll(self):
        client = Client([report(status="blocked")])
        monitor = self.monitor(client)

        outcome = monitor.run_once("operator-1", now=100)
        status = monitor.status(now=100)
        stored = monitor.connection.execute(
            "SELECT * FROM localization_health_monitor"
        ).fetchone()

        self.assertEqual((outcome.attempted, outcome.report_status), (True, "blocked"))
        self.assertEqual((status.state, status.next_poll_at), ("scheduled", 130.0))
        self.assertEqual(status.last_reason_codes, (
            "publication.evidence.policy_unavailable",
            "release.policy_unavailable",
        ))
        self.assertEqual((
            status.last_component_count, status.last_provider_count,
            status.last_website_version_count,
        ), (1, 0, 1))
        self.assertEqual(len(status.last_report_sha256), 64)
        self.assertNotIn("private-site", tuple(stored))
        self.assertNotIn("private-event", tuple(stored))
        self.assertNotIn("fi-FI", tuple(stored))

    def test_not_due_does_not_call_client(self):
        client = Client([report(), report(checked_at=130)])
        monitor = self.monitor(client)
        monitor.run_once("operator-1", now=100)

        outcome = monitor.run_once("operator-2", now=129)

        self.assertFalse(outcome.attempted)
        self.assertEqual(client.calls, 1)

    def test_retryable_failures_back_off_and_stop_at_bound(self):
        failures = [
            MONITOR._CLIENT.HealthClientFailed(
                "health_client.network", retryable=True,
            ) for _ in range(3)
        ]
        client = Client(failures)
        monitor = self.monitor(client)

        first = monitor.run_once("operator", now=100)
        not_due = monitor.run_once("operator", now=104)
        second = monitor.run_once("operator", now=105)
        third = monitor.run_once("operator", now=115)

        self.assertEqual((first.state, first.next_poll_at), ("retry_wait", 105.0))
        self.assertFalse(not_due.attempted)
        self.assertEqual((second.state, second.next_poll_at), ("retry_wait", 115.0))
        self.assertEqual((third.state, third.consecutive_failures), ("failed", 3))
        self.assertEqual(client.calls, 3)
        self.assertFalse(monitor.run_once("operator", now=999).attempted)

    def test_nonretryable_authentication_failure_is_terminal_immediately(self):
        client = Client([MONITOR._CLIENT.HealthClientFailed(
            "health.http.authentication_failed", retryable=False,
        )])
        monitor = self.monitor(client)

        outcome = monitor.run_once("operator", now=100)

        self.assertEqual((outcome.state, outcome.error_code), (
            "failed", "health.http.authentication_failed",
        ))
        self.assertEqual(outcome.consecutive_failures, 1)

    def test_expired_lease_is_recovered_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            first_connection = sqlite3.connect(path)
            first = self.monitor(Client([report()]), first_connection)
            first.connection.execute("""
                UPDATE localization_health_monitor
                SET state = 'leased', poll_attempts = 1,
                    lease_owner = 'crashed', lease_token = 'lost',
                    lease_expires_at = 110, updated_at = 100
            """)
            first.connection.commit()
            first.connection.close()

            client = Client([report(checked_at=111)])
            second = self.monitor(client, sqlite3.connect(path))

            active = second.run_once("replacement", now=109)
            recovered = second.run_once("replacement", now=111)

            self.assertFalse(active.attempted)
            self.assertEqual((recovered.attempted, recovered.attempt), (True, 2))
            self.assertEqual(client.calls, 1)

    def test_operator_can_rearm_terminal_state_but_not_live_lease(self):
        client = Client([
            MONITOR._CLIENT.HealthClientFailed(
                "health.http.authentication_failed", retryable=False,
            ),
            report(checked_at=200),
        ])
        monitor = self.monitor(client)
        monitor.run_once("operator", now=100)

        status = monitor.rearm(now=200)
        recovered = monitor.run_once("operator", now=200)

        self.assertEqual(status.state, "scheduled")
        self.assertEqual(recovered.report_status, "healthy")
        monitor.connection.execute("""
            UPDATE localization_health_monitor
            SET state = 'leased', lease_owner = 'active', lease_token = 'token',
                lease_expires_at = 240, updated_at = 210
        """)
        monitor.connection.commit()
        with self.assertRaises(MONITOR.HealthMonitorBlocked):
            monitor.rearm(now=220)

    def test_tampered_schema_and_state_fail_closed_without_client_call(self):
        client = Client([report()])
        monitor = self.monitor(client)
        monitor.connection.execute(
            "UPDATE localization_health_monitor SET last_reason_codes_json = ?",
            ('["z.reason","a.reason"]',),
        )
        monitor.connection.commit()

        with self.assertRaises(MONITOR.HealthMonitorBlocked) as caught:
            monitor.run_once("operator", now=100)

        self.assertEqual(str(caught.exception), "health_monitor.state_invalid")
        self.assertEqual(client.calls, 0)

    def test_invalid_client_result_is_terminal_and_never_persisted(self):
        client = Client([{"source_text": "secret"}])
        monitor = self.monitor(client)

        outcome = monitor.run_once("operator", now=100)
        status = monitor.status(now=100)

        self.assertEqual((outcome.state, outcome.error_code), (
            "failed", "health_monitor.client_invalid",
        ))
        self.assertIsNone(status.last_report_sha256)
        self.assertNotIn(
            "secret", tuple(monitor.connection.execute(
                "SELECT * FROM localization_health_monitor"
            ).fetchone()),
        )

    def test_health_contract_tracks_due_success_degradation_and_blocking(self):
        client = Client([
            report(), report(status="degraded", checked_at=130),
            report(status="blocked", checked_at=160),
        ])
        monitor = self.monitor(client)

        initial = monitor.health(now=100).as_payload()
        monitor.run_once("operator", now=100)
        healthy = monitor.health(now=101).as_payload()
        overdue = monitor.health(now=130).as_payload()
        monitor.run_once("operator", now=130)
        degraded = monitor.health(now=131).as_payload()
        monitor.run_once("operator", now=160)
        blocked = monitor.health(now=161).as_payload()

        self.assertEqual((initial["status"], initial["due"]), ("degraded", True))
        self.assertEqual(initial["poller_reasons"], [
            "health_monitor.no_report", "health_monitor.poll_due",
        ])
        self.assertEqual((healthy["status"], healthy["due"]), ("healthy", False))
        self.assertEqual(healthy["last_report"]["status"], "healthy")
        self.assertEqual((overdue["status"], overdue["poller_reasons"]), (
            "degraded", ["health_monitor.poll_due"],
        ))
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["last_report"]["reason_codes"], [
            "publication.evidence.policy_unavailable",
            "release.policy_unavailable",
        ])
        serialized = str(blocked)
        for private_value in (
            "private-site", "private-event", "private-version", "fi-FI",
        ):
            self.assertNotIn(private_value, serialized)

    def test_health_marks_retry_terminal_and_expired_lease_states(self):
        client = Client([
            MONITOR._CLIENT.HealthClientFailed(
                "health_client.network", retryable=True,
            ),
        ])
        monitor = self.monitor(client)
        monitor.run_once("operator", now=100)

        retrying = monitor.health(now=101).as_payload()
        monitor.connection.execute("""
            UPDATE localization_health_monitor
            SET state = 'leased', lease_owner = 'crashed',
                lease_token = 'token', lease_expires_at = 102, updated_at = 101
        """)
        monitor.connection.commit()
        expired = monitor.health(now=103).as_payload()
        monitor.connection.execute("""
            UPDATE localization_health_monitor
            SET state = 'failed', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, updated_at = 104
        """)
        monitor.connection.commit()
        failed = monitor.health(now=104).as_payload()

        self.assertEqual((retrying["status"], retrying["poller_reasons"]), (
            "degraded", ["health_monitor.no_report", "health_monitor.retry_wait"],
        ))
        self.assertEqual((expired["due"], expired["lease_expired"]), (True, True))
        self.assertIn("health_monitor.lease_expired", expired["poller_reasons"])
        self.assertEqual((failed["status"], failed["poller_reasons"]), (
            "blocked", ["health_monitor.failed", "health_monitor.no_report"],
        ))

    def test_run_forever_polls_once_and_waits_interruptibly(self):
        client = Client([report()])
        monitor = self.monitor(client)
        stop = StopEvent()

        monitor.run_forever(
            "operator-loop", clock=lambda: 100, stop_event=stop,
        )

        self.assertEqual(client.calls, 1)
        self.assertEqual(stop.waits, [30.0])
        self.assertEqual(monitor.status(now=100).poll_attempts, 1)

    def test_run_forever_honours_preexisting_stop_without_state_access(self):
        client = Client([report()])
        monitor = self.monitor(client)
        monitor.connection.execute(
            "UPDATE localization_health_monitor SET last_reason_codes_json = 'bad'"
        )
        monitor.connection.commit()

        monitor.run_forever(
            "operator-loop", clock=lambda: 100,
            stop_event=StopEvent(stopped=True),
        )

        self.assertEqual(client.calls, 0)

    def test_run_forever_rejects_ambiguous_host_controls(self):
        monitor = self.monitor(Client([report()]))
        invalid_controls = (
            {"clock": None, "stop_event": StopEvent()},
            {"clock": lambda: 100, "stop_event": object()},
            {"clock": lambda: 100, "stop_event": StopEvent(),
             "maximum_wait_seconds": True},
        )
        for options in invalid_controls:
            with self.subTest(options=options):
                with self.assertRaises(MONITOR.HealthMonitorBlocked):
                    monitor.run_forever("operator-loop", **options)


if __name__ == "__main__":
    unittest.main()
