from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RECEIVER = load(
    "blun_test_terminal_receiver_processing",
    ROOT
    / "integrations"
    / "website_localization_cms_terminal_notification_receiver.py",
)


def notification(*, event_id="cms-event-184", sequence=42):
    value = {
        "schema": RECEIVER.NOTIFICATION_SCHEMA,
        "event_id": event_id,
        "site_id": "public-site",
        "plan_id": f"plan-{event_id}",
        "website_version": f"release-{sequence}",
        "source_sequence": sequence,
        "job_count": 23,
        "change_sha256": "a" * 64,
        "lifecycle_binding_sha256": "b" * 64,
        "terminal_status": "published",
        "lifecycle_sha256": "c" * 64,
    }
    value["notification_id"] = "terminal-" + hashlib.sha256(
        RECEIVER._canonical(value)
    ).hexdigest()
    return value


def accept(inbox, payload, *, now=100):
    body = RECEIVER._canonical(payload)
    return inbox.accept(
        payload, body, hashlib.sha256(body).hexdigest(), now=now,
    )


def processing_ack(payload):
    body = RECEIVER._canonical(payload)
    return {
        "schema": RECEIVER.PROCESSING_ACK_SCHEMA,
        "notification_id": payload["notification_id"],
        "event_id": payload["event_id"],
        "site_id": payload["site_id"],
        "status": "processed",
        "notification_sha256": hashlib.sha256(body).hexdigest(),
    }


class TerminalNotificationProcessingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.inbox = RECEIVER.DurableCMSTerminalNotificationInbox(
            self.connection,
            processing_max_attempts=2,
            processing_base_delay_seconds=2,
            processing_max_delay_seconds=2,
        )

    def tearDown(self):
        self.connection.close()

    def test_receive_atomically_registers_pending_processing(self):
        payload = notification()
        accept(self.inbox, payload)

        status = self.inbox.processing_status(payload["event_id"], now=100)
        health = self.inbox.health(now=100)

        self.assertEqual((status.status, status.attempts, status.max_attempts), (
            "pending", 0, 2,
        ))
        self.assertEqual((health.status, health.received, health.due), (
            "ok", 1, 1,
        ))
        self.assertEqual(health.counts, {
            "pending": 1,
            "leased": 0,
            "retry_wait": 0,
            "succeeded": 0,
            "failed": 0,
        })

    def test_successful_callback_is_exactly_bound_and_persisted(self):
        payload = notification()
        accept(self.inbox, payload)
        seen = []

        outcome = self.inbox.run_next_processing(
            lambda received: seen.append(received) or processing_ack(received),
            "cms-consumer",
            now=100,
            lease_seconds=30,
        )

        self.assertEqual(seen, [payload])
        self.assertEqual((outcome.status, outcome.attempt, outcome.error_code), (
            "succeeded", 1, None,
        ))
        restarted = RECEIVER.DurableCMSTerminalNotificationInbox(
            self.connection,
            processing_max_attempts=9,
        )
        status = restarted.processing_status(payload["event_id"], now=101)
        self.assertEqual((status.status, status.max_attempts, status.processed_at), (
            "succeeded", 2, 100,
        ))
        self.assertIsNone(restarted.claim_processing("other", now=101))

    def test_retry_backoff_and_attempt_ceiling_are_durable(self):
        payload = notification()
        accept(self.inbox, payload)

        def unavailable(_payload):
            raise RECEIVER.TerminalNotificationProcessingFailure(
                "cms_backend.unavailable", retryable=True,
            )

        first = self.inbox.run_next_processing(
            unavailable, "worker", now=100, lease_seconds=10,
        )
        waiting = self.inbox.processing_status(payload["event_id"], now=101)
        self.assertEqual((first.status, first.error_code), (
            "retry_wait", "cms_backend.unavailable",
        ))
        self.assertEqual(waiting.next_attempt_at, 102)
        self.assertIsNone(self.inbox.run_next_processing(
            unavailable, "worker", now=101, lease_seconds=10,
        ))

        final = self.inbox.run_next_processing(
            unavailable, "worker", now=102, lease_seconds=10,
        )
        self.assertEqual((final.status, final.attempt), ("failed", 2))
        health = self.inbox.health(now=102)
        self.assertEqual((health.status, health.failed), ("blocked", 1))

    def test_expired_lease_recovers_and_stale_owner_cannot_finish(self):
        payload = notification()
        accept(self.inbox, payload)
        first = self.inbox.claim_processing(
            "first", now=100, lease_seconds=5,
        )
        self.assertIsNone(self.inbox.claim_processing(
            "second", now=104, lease_seconds=20,
        ))

        second = self.inbox.claim_processing(
            "second", now=105, lease_seconds=20,
        )
        self.assertEqual((second.attempt, second.worker_id), (2, "second"))
        with self.assertRaises(
            RECEIVER.TerminalNotificationReceiverBlocked
        ) as stale:
            self.inbox.complete_processing(
                first, processing_ack(payload), now=106,
            )
        self.assertEqual(stale.exception.code, (
            "notification_receiver.processing_claim_lost"
        ))
        completed = self.inbox.complete_processing(
            second, processing_ack(payload), now=106,
        )
        self.assertEqual(completed.status, "succeeded")

    def test_expired_lease_recovers_after_database_restart(self):
        self.connection.close()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "terminal.sqlite3")
            first_connection = sqlite3.connect(path)
            first = RECEIVER.DurableCMSTerminalNotificationInbox(
                first_connection, processing_max_attempts=3,
            )
            payload = notification()
            accept(first, payload)
            first.claim_processing("first", now=100, lease_seconds=5)
            first_connection.close()

            self.connection = sqlite3.connect(path)
            second = RECEIVER.DurableCMSTerminalNotificationInbox(
                self.connection, processing_max_attempts=9,
            )
            blocked = second.health(now=105)
            self.assertEqual((blocked.status, blocked.expired_leases), (
                "blocked", 1,
            ))
            recovered = second.claim_processing(
                "second", now=105, lease_seconds=20,
            )
            self.assertEqual((recovered.attempt, recovered.max_attempts), (2, 3))

    def test_parallel_workers_claim_distinct_notifications_once(self):
        payloads = [
            notification(event_id=f"cms-event-{index}", sequence=index)
            for index in range(1, 5)
        ]
        for payload in payloads:
            accept(self.inbox, payload)

        def claim(index):
            return self.inbox.claim_processing(
                f"worker-{index}", now=100, lease_seconds=30,
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(claim, range(12)))

        claimed = [claim for claim in claims if claim is not None]
        self.assertEqual(len(claimed), 4)
        self.assertEqual(len({claim.notification_id for claim in claimed}), 4)
        self.assertEqual(self.inbox.health(now=100).counts["leased"], 4)

    def test_v1_database_migrates_atomically_without_losing_receipts(self):
        self.connection.close()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE cms_terminal_notification_inbox_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL CHECK (schema_version = 1)
            );
            INSERT INTO cms_terminal_notification_inbox_meta VALUES (1, 1);
            CREATE TABLE cms_terminal_notification_inbox (
                notification_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                site_id TEXT NOT NULL,
                terminal_status TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                received_at REAL NOT NULL
            );
        """)
        payload = notification()
        body = RECEIVER._canonical(payload)
        self.connection.execute("""
            INSERT INTO cms_terminal_notification_inbox VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            payload["notification_id"], payload["event_id"], payload["site_id"],
            payload["terminal_status"], hashlib.sha256(body).hexdigest(),
            body.decode("utf-8"), 75,
        ))
        self.connection.commit()

        migrated = RECEIVER.DurableCMSTerminalNotificationInbox(
            self.connection, processing_max_attempts=4,
        )

        self.assertEqual(self.connection.execute(
            "SELECT schema_version FROM cms_terminal_notification_inbox_meta"
        ).fetchone()[0], 2)
        status = migrated.processing_status(payload["event_id"], now=75)
        self.assertEqual((status.status, status.max_attempts), ("pending", 4))
        self.assertEqual(migrated.status(payload["event_id"]).received_at, 75)

    def test_tampered_v1_database_rolls_migration_back(self):
        self.connection.close()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE cms_terminal_notification_inbox_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL CHECK (schema_version = 1)
            );
            INSERT INTO cms_terminal_notification_inbox_meta VALUES (1, 1);
            CREATE TABLE cms_terminal_notification_inbox (
                notification_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                site_id TEXT NOT NULL,
                terminal_status TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                received_at REAL NOT NULL,
                injected TEXT
            );
        """)
        with self.assertRaises(
            RECEIVER.TerminalNotificationReceiverBlocked
        ) as caught:
            RECEIVER.DurableCMSTerminalNotificationInbox(self.connection)
        self.assertEqual(caught.exception.code, (
            "notification_receiver.schema_altered"
        ))
        self.assertEqual(self.connection.execute(
            "SELECT schema_version FROM cms_terminal_notification_inbox_meta"
        ).fetchone()[0], 1)
        self.assertIsNone(self.connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name = 'cms_terminal_notification_processing'"
        ).fetchone())

    def test_processing_tamper_blocks_health_claim_and_replay(self):
        payload = notification()
        accept(self.inbox, payload)
        self.connection.execute("""
            UPDATE cms_terminal_notification_processing
            SET last_error_code = 'forged.error'
        """)
        self.connection.commit()

        for operation in (
            lambda: self.inbox.health(now=100),
            lambda: self.inbox.claim_processing("worker", now=100),
            lambda: accept(self.inbox, payload),
        ):
            with self.subTest(operation=operation), self.assertRaises(
                RECEIVER.TerminalNotificationReceiverBlocked
            ) as caught:
                operation()
            self.assertEqual(caught.exception.code, (
                "notification_receiver.processing_state_invalid"
            ))

    def test_private_callback_failure_becomes_content_free_terminal_state(self):
        payload = notification()
        accept(self.inbox, payload)

        outcome = self.inbox.run_next_processing(
            lambda _payload: (_ for _ in ()).throw(
                RuntimeError("private backend detail")
            ),
            "worker",
            now=100,
            lease_seconds=10,
        )

        self.assertEqual((outcome.status, outcome.error_code), (
            "failed", "processing_callback_failure",
        ))
        self.assertNotIn("private", repr(outcome))
        row = self.connection.execute(
            "SELECT last_error_code FROM cms_terminal_notification_processing"
        ).fetchone()
        self.assertEqual(row[0], "processing_callback_failure")

    def test_wrong_host_acknowledgement_fails_without_retry(self):
        payload = notification()
        accept(self.inbox, payload)

        outcome = self.inbox.run_next_processing(
            lambda _payload: {
                **processing_ack(payload),
                "event_id": "another-event",
            },
            "worker",
            now=100,
            lease_seconds=10,
        )

        self.assertEqual((outcome.status, outcome.error_code), (
            "failed",
            "notification_receiver.processing_response_invalid",
        ))
        self.assertEqual(self.inbox.health(now=100).status, "blocked")


if __name__ == "__main__":
    unittest.main()
