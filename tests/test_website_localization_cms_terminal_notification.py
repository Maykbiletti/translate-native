from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


NOTIFY = load(
    "blun_test_website_localization_cms_terminal_notification",
    ROOT / "integrations" / "website_localization_cms_terminal_notification.py",
)


def lifecycle(**overrides):
    values = {
        "state": "terminal",
        "event_id": "cms-event-184",
        "site_id": "public-site",
        "plan_id": "plan-cms-event-184",
        "website_version": "release-42",
        "source_sequence": 42,
        "job_count": 23,
        "change_sha256": "a" * 64,
        "binding_sha256": "b" * 64,
        "remote_status": "published",
        "lifecycle_sha256": "c" * 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def acknowledgement(payload):
    rendered = NOTIFY._canonical(payload)
    return {
        "schema": NOTIFY.ACK_SCHEMA,
        "notification_id": payload["notification_id"],
        "event_id": payload["event_id"],
        "site_id": payload["site_id"],
        "status": "accepted",
        "notification_sha256": NOTIFY._hash(rendered),
    }


class DurableTerminalNotificationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.outbox = NOTIFY.DurableCMSTerminalNotifier(
            self.connection, base_delay_seconds=2, max_delay_seconds=8,
        )

    def tearDown(self):
        self.connection.close()

    def test_terminal_evidence_is_durable_content_free_and_idempotent(self):
        terminal = lifecycle()
        first = self.outbox.register(terminal, now=100)
        second = self.outbox.register(terminal, now=101)
        row = self.connection.execute(
            "SELECT payload_json FROM cms_source_terminal_notification"
        ).fetchone()

        self.assertEqual(first.notification_id, second.notification_id)
        self.assertEqual(first.status, "pending")
        self.assertEqual(json.loads(row[0])["terminal_status"], "published")
        rendered = row[0]
        self.assertNotIn("source_text", rendered)
        self.assertNotIn("target_text", rendered)

    def test_exact_acknowledgement_completes_one_leased_attempt(self):
        self.outbox.register(lifecycle(), now=100)
        calls = []

        def callback(payload):
            calls.append(copy.deepcopy(payload))
            return acknowledgement(payload)

        outcome = self.outbox.run_once(
            callback, "notification-worker", now=100, lease_seconds=60,
        )

        self.assertEqual((outcome.status, outcome.attempt), ("succeeded", 1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.outbox.health(now=100).status, "ok")

    def test_lost_response_retries_same_identity_with_bounded_backoff(self):
        self.outbox.register(lifecycle(), max_attempts=2, now=100)
        calls = []

        def callback(payload):
            calls.append(payload["notification_id"])
            if len(calls) == 1:
                raise NOTIFY.TerminalNotificationFailure(
                    "notification.network_unavailable", retryable=True,
                )
            return acknowledgement(payload)

        first = self.outbox.run_once(
            callback, "notification-worker", now=100, lease_seconds=60,
        )
        self.assertEqual((first.status, first.error_code), (
            "retry_wait", "notification.network_unavailable",
        ))
        self.assertIsNone(self.outbox.run_once(
            callback, "notification-worker", now=101, lease_seconds=60,
        ))
        second = self.outbox.run_once(
            callback, "notification-worker", now=102, lease_seconds=60,
        )
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(calls[0], calls[1])

    def test_invalid_ack_and_nonretryable_private_error_fail_closed(self):
        for index, callback in enumerate((
            lambda payload: {**acknowledgement(payload), "status": "queued"},
            lambda _payload: (_ for _ in ()).throw(RuntimeError("private prose")),
        )):
            with self.subTest(index=index):
                connection = sqlite3.connect(":memory:")
                outbox = NOTIFY.DurableCMSTerminalNotifier(connection)
                outbox.register(lifecycle(event_id=f"event-{index}"), now=100)
                outcome = outbox.run_once(
                    callback, "notification-worker", now=100,
                    lease_seconds=60,
                )
                self.assertEqual(outcome.status, "failed")
                self.assertNotIn("private prose", repr(outcome))
                connection.close()

    def test_collision_tampering_and_nonterminal_input_block_before_callback(self):
        self.outbox.register(lifecycle(), now=100)
        with self.assertRaises(NOTIFY.TerminalNotificationBlocked):
            self.outbox.register(
                lifecycle(website_version="release-43"), now=101,
            )
        with self.assertRaises(NOTIFY.TerminalNotificationBlocked):
            self.outbox.register(
                lifecycle(state="watching", remote_status="processing"),
                now=101,
            )

        self.connection.execute(
            "UPDATE cms_source_terminal_notification SET payload_json = ?",
            ("{}",),
        )
        with self.assertRaises(NOTIFY.TerminalNotificationBlocked):
            self.outbox.run_once(
                lambda payload: acknowledgement(payload),
                "notification-worker", now=101, lease_seconds=60,
            )

    def test_immediate_cancelled_terminal_uses_binding_without_fake_snapshot(self):
        status = self.outbox.register(lifecycle(
            remote_status="cancelled", lifecycle_sha256=None,
        ), now=100)
        payload = json.loads(self.connection.execute(
            "SELECT payload_json FROM cms_source_terminal_notification"
        ).fetchone()[0])
        self.assertIsNone(payload["lifecycle_sha256"])
        self.assertEqual(status.status, "pending")


if __name__ == "__main__":
    unittest.main()
