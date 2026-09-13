from __future__ import annotations

import importlib.util
import sqlite3
import sys
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
    "blun_test_website_localization_cms_terminal_processing_monitor",
    ROOT / "integrations" / "website_localization_cms_terminal_processing_monitor.py",
)


def notification(**overrides):
    value = {
        "schema": MONITOR.NOTIFICATION_SCHEMA,
        "notification_id": "terminal-" + "a" * 64,
        "event_id": "cms-event-184",
        "site_id": "public-site",
        "plan_id": "plan-cms-event-184",
        "website_version": "release-42",
        "source_sequence": 42,
        "job_count": 23,
        "change_sha256": "d" * 64,
        "lifecycle_binding_sha256": "e" * 64,
        "terminal_status": "published",
        "lifecycle_sha256": "f" * 64,
    }
    value.update(overrides)
    return value


def response(**overrides):
    value = {
        "schema": MONITOR.STATUS_SCHEMA,
        "notification_id": "terminal-" + "a" * 64,
        "event_id": "cms-event-184",
        "site_id": "public-site",
        "terminal_status": "published",
        "notification_sha256": MONITOR._canonical_sha256(notification()),
        "processing_status": "pending",
        "attempts": 0,
        "max_attempts": 5,
        "next_attempt_at": 100.0,
        "lease_expires_at": None,
        "lease_expired": False,
        "last_error_code": None,
        "processed_at": None,
        "capabilities_sha256": "c" * 64,
    }
    value.update(overrides)
    return value


class RetryableError(RuntimeError):
    cms_notification_failure = True
    retryable = True
    code = "terminal_receiver_client.network"


class DurableTerminalProcessingMonitorTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.monitor = MONITOR.DurableTerminalProcessingMonitor(
            self.connection,
            poll_interval_seconds=10,
            base_delay_seconds=2,
            max_delay_seconds=8,
        )
        self.notification_sha256 = MONITOR._canonical_sha256(notification())
        self.monitor.register(
            notification(), self.notification_sha256, now=100,
        )

    def tearDown(self):
        self.connection.close()

    def test_pending_processing_is_repolled_then_succeeds(self):
        calls = []

        def reader(event_id, site_id):
            calls.append((event_id, site_id))
            if len(calls) == 1:
                return response()
            return response(
                processing_status="succeeded",
                attempts=1,
                processed_at=109.0,
            )

        first = self.monitor.run_once(
            reader, "observer", now=100, lease_seconds=30,
        )
        self.assertEqual(first.status, "watching")
        self.assertIsNone(self.monitor.run_once(
            reader, "observer", now=109, lease_seconds=30,
        ))
        second = self.monitor.run_once(
            reader, "observer", now=110, lease_seconds=30,
        )

        self.assertEqual(second.status, "succeeded")
        self.assertEqual(calls, [
            ("cms-event-184", "public-site"),
            ("cms-event-184", "public-site"),
        ])
        self.assertEqual(self.monitor.health(now=110).status, "ok")

    def test_exact_binding_mismatch_fails_closed(self):
        outcome = self.monitor.run_once(
            lambda _event, _site: response(site_id="another-site"),
            "observer", now=100, lease_seconds=30,
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            outcome.error_code,
            "terminal_processing_monitor.response_invalid",
        )
        self.assertEqual(self.monitor.health(now=100).status, "blocked")

    def test_remote_failure_is_visible_without_private_data(self):
        outcome = self.monitor.run_once(
            lambda _event, _site: response(
                processing_status="failed",
                attempts=5,
                last_error_code="cms.apply_failed",
            ),
            "observer", now=100, lease_seconds=30,
        )
        status = self.monitor.status("cms-event-184", now=100)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            status.last_error_code,
            "terminal_processing_monitor.remote_failed",
        )
        self.assertEqual(status.receiver_error_code, "cms.apply_failed")
        self.assertNotIn("private", repr(status))

    def test_retryable_reader_failure_is_bounded_and_crash_safe(self):
        def reader(_event, _site):
            raise RetryableError("private transport detail")

        first = self.monitor.run_once(
            reader, "observer-a", now=100, lease_seconds=30,
        )
        self.assertEqual(first.status, "retry_wait")
        self.assertEqual(
            self.monitor.status("cms-event-184", now=100).failures, 1,
        )
        self.assertIsNone(self.monitor.run_once(
            reader, "observer-b", now=101, lease_seconds=30,
        ))

        claim = self.monitor.claim("observer-a", now=102, lease_seconds=5)
        self.assertIsNotNone(claim)
        recovered = self.monitor.run_once(
            reader, "observer-b", now=107, lease_seconds=30,
        )
        self.assertEqual(recovered.status, "retry_wait")
        self.assertNotIn("private transport detail", repr(recovered))

    def test_registration_is_idempotent_and_tampering_blocks(self):
        first = self.monitor.register(
            notification(), self.notification_sha256, now=101,
        )
        self.assertEqual(first.status, "pending")
        with self.assertRaises(MONITOR.TerminalProcessingMonitorBlocked):
            self.monitor.register(
                notification(site_id="another-site"),
                self.notification_sha256,
                now=101,
            )

        self.connection.execute(
            "UPDATE cms_source_terminal_processing SET notification_sha256 = ?",
            ("not-a-hash",),
        )
        with self.assertRaises(MONITOR.TerminalProcessingMonitorBlocked):
            self.monitor.health(now=101)


if __name__ == "__main__":
    unittest.main()
