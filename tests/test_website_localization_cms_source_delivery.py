from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path

from tests import test_website_localization_cms_client as cms_support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DELIVERY = load(
    "blun_test_website_localization_cms_source_delivery",
    ROOT / "integrations" / "website_localization_cms_source_delivery.py",
)


class ClientFailure(RuntimeError):
    cms_source_client_failure = True

    def __init__(self, code="source_client.network", *, retryable=True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ScriptedClient:
    def __init__(self, digest="a" * 64):
        self.expected_capabilities_sha256 = digest
        self.timeout = 30
        self.calls = []
        self.failures = []
        self.events = {}

    @staticmethod
    def _canonical(value):
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _submit(self, operation, value, max_attempts):
        copied = copy.deepcopy(value)
        self.calls.append((operation, copied, max_attempts))
        if self.failures:
            raise self.failures.pop(0)
        if operation == "change":
            request_id = copied["event_id"]
            schema = "blun.cms-source-change-enqueue-response.v2"
            self.events[copied["event_id"]] = copied
        else:
            if copied["schema"].endswith("cancellation.v1"):
                operation = "cancellation"
                request_id = copied["cancellation_id"]
            else:
                operation = "tombstone"
                request_id = copied["tombstone_id"]
            schema = "blun.cms-source-removal-enqueue-response.v2"
        return {
            "schema": schema,
            "operation": operation,
            "request_id": request_id,
            "event_id": copied["event_id"],
            "payload_sha256": hashlib.sha256(
                self._canonical(copied)
            ).hexdigest(),
            "status": "pending",
            "attempts": 0,
            "max_attempts": max_attempts,
            "capabilities_sha256": self.expected_capabilities_sha256,
        }

    def submit_change(self, value, *, max_attempts):
        return self._submit("change", value, max_attempts)

    def submit_removal(self, value, *, max_attempts):
        return self._submit("removal", value, max_attempts)

    def status(self, event_id, site_id):
        self.calls.append(("status", event_id, site_id))
        change = self.events[event_id]
        return {
            "schema": "blun.cms-source-status-response.v4",
            "status": {
                "schema": "blun.cms-source-service-status.v3",
                "event_id": event_id,
                "site_id": site_id,
                "website_version": change["website_version"],
                "source_sequence": change["source_sequence"],
                "change_sha256": "b" * 64,
                "dispatch_status": "succeeded",
                "dispatch_attempts": 1,
                "dispatch_max_attempts": 5,
                "dispatch_error_code": None,
                "plan_id": "plan-" + event_id,
                "job_count": 2,
                "lifecycle_state": "watching",
                "lifecycle_poll_attempts": 1,
                "lifecycle_error_code": None,
                "remote_status": "processing",
                "lifecycle_sha256": "c" * 64,
                "required_locales": ["fi-FI", "mt-MT"],
                "approved_locales": [],
                "blocked_locales": [],
                "queue_counts": {
                    "failed": 0,
                    "leased": 0,
                    "pending": 2,
                    "retry_wait": 0,
                    "succeeded": 0,
                },
                "notification_state": "awaiting_terminal",
                "notification_id": None,
                "notification_sha256": None,
                "notification_attempts": 0,
                "notification_max_attempts": None,
                "notification_error_code": None,
                "terminal_processing_state": "disabled",
                "terminal_processing_poll_attempts": 0,
                "terminal_processing_failures": 0,
                "terminal_processing_error_code": None,
                "receiver_processing_state": None,
                "receiver_processing_attempts": None,
                "receiver_processing_max_attempts": None,
                "receiver_processing_error_code": None,
                "receiver_processed_at": None,
            },
            "capabilities_sha256": self.expected_capabilities_sha256,
        }

    def readiness(self):
        self.calls.append(("readiness",))
        return {
            "schema": "blun.cms-source-readiness-response.v2",
            "readiness": {
                "schema": "blun.cms-source-worker-readiness.v1",
                "status": "ready",
                "worker_state": "running",
                "service_status": "ok",
                "error_code": None,
            },
            "capabilities_sha256": self.expected_capabilities_sha256,
        }


class SourceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.connection = sqlite3.connect(":memory:")
        self.client = ScriptedClient()
        self.outbox = DELIVERY.DurableCMSSourceDeliveryOutbox(
            self.connection,
            self.client,
            clock=lambda: self.now,
            base_delay_seconds=5,
            max_delay_seconds=20,
        )

    def tearDown(self):
        self.connection.close()

    def test_change_is_persisted_before_one_bound_delivery(self):
        change = cms_support.event()
        queued = self.outbox.enqueue_change(
            change, source_max_attempts=4, delivery_max_attempts=3,
        )

        self.assertEqual((queued.status, queued.attempts), ("pending", 0))
        outcome = self.outbox.run_once("worker-1", lease_seconds=60)

        self.assertEqual((outcome.status, outcome.attempt), ("succeeded", 1))
        self.assertEqual(self.client.calls, [("change", change, 4)])
        stored = self.outbox.status("change", change["event_id"])
        self.assertIsNotNone(stored.response_sha256)
        self.assertEqual(self.outbox.health().status, "ok")

    def test_source_status_requires_exact_succeeded_submission(self):
        change = cms_support.event()
        payload_hash = hashlib.sha256(
            self.client._canonical(change)
        ).hexdigest()
        self.outbox.enqueue_change(change)

        with self.assertRaisesRegex(
            DELIVERY.CMSSourceDeliveryBlocked,
            "source_delivery.source_status_unavailable",
        ):
            self.outbox.source_status(
                change["event_id"], change["site_id"], payload_hash,
            )
        self.assertEqual(self.client.calls, [])

        self.outbox.run_once("worker-1", lease_seconds=60)
        response = self.outbox.source_status(
            change["event_id"], change["site_id"], payload_hash,
        )

        self.assertEqual(response["status"]["remote_status"], "processing")
        self.assertEqual(response["status"]["required_locales"], [
            "fi-FI", "mt-MT",
        ])
        self.assertEqual(self.client.calls[-1], (
            "status", change["event_id"], change["site_id"],
        ))
        with self.assertRaisesRegex(
            DELIVERY.CMSSourceDeliveryBlocked,
            "source_delivery.status_not_found",
        ):
            self.outbox.source_status(
                change["event_id"], change["site_id"], "f" * 64,
            )

    def test_source_readiness_is_exact_content_free_and_read_only(self):
        response = self.outbox.source_readiness()

        self.assertEqual(response["readiness"], {
            "schema": "blun.cms-source-worker-readiness.v1",
            "status": "ready",
            "worker_state": "running",
            "service_status": "ok",
            "error_code": None,
        })
        self.assertEqual(response["capabilities_sha256"], "a" * 64)
        self.assertEqual(self.client.calls, [("readiness",)])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cms_source_delivery_outbox"
            ).fetchone()[0],
            0,
        )

        self.client.readiness = lambda: {
            "schema": "blun.cms-source-readiness-response.v2",
            "readiness": {
                "schema": "blun.cms-source-worker-readiness.v1",
                "status": "ready",
            },
            "capabilities_sha256": "a" * 64,
        }
        with self.assertRaisesRegex(
            DELIVERY.CMSSourceDeliveryBlocked,
            "source_delivery.source_readiness_invalid",
        ):
            self.outbox.source_readiness()

    def test_removals_are_delivered_before_older_changes(self):
        change = cms_support.event()
        removal = cms_support.cancellation(change)
        self.outbox.enqueue_change(change, now=100)
        self.outbox.enqueue_removal(removal, now=101)
        self.now = 101

        first = self.outbox.run_once("worker-1", lease_seconds=60)
        second = self.outbox.run_once("worker-1", lease_seconds=60)

        self.assertEqual((first.operation, second.operation), (
            "cancellation", "change",
        ))

    def test_exact_replay_converges_and_changed_binding_collides(self):
        change = cms_support.event()
        first = self.outbox.enqueue_change(
            change, source_max_attempts=4, delivery_max_attempts=3,
        )
        replay = self.outbox.enqueue_change(
            copy.deepcopy(change), source_max_attempts=4,
            delivery_max_attempts=3,
        )
        altered = copy.deepcopy(change)
        altered["localization"]["source_text"] += " Changed."

        self.assertEqual(first, replay)
        with self.assertRaises(DELIVERY.CMSSourceDeliveryBlocked) as caught:
            self.outbox.enqueue_change(
                altered, source_max_attempts=4, delivery_max_attempts=3,
            )
        self.assertEqual(
            caught.exception.code, "source_delivery.idempotency_collision",
        )
        with self.assertRaises(DELIVERY.CMSSourceDeliveryBlocked) as caught:
            self.outbox.enqueue_change(
                change, source_max_attempts=5, delivery_max_attempts=3,
            )
        self.assertEqual(
            caught.exception.code, "source_delivery.idempotency_collision",
        )

    def test_retryable_failure_waits_then_succeeds(self):
        change = cms_support.event()
        self.client.failures.append(ClientFailure())
        self.outbox.enqueue_change(change, delivery_max_attempts=3)

        first = self.outbox.run_once("worker-1", lease_seconds=60)
        early = self.outbox.run_once("worker-1", lease_seconds=60)
        self.now = 105
        second = self.outbox.run_once("worker-1", lease_seconds=60)

        self.assertEqual(first.status, "retry_wait")
        self.assertEqual(first.next_attempt_at, 105)
        self.assertIsNone(early)
        self.assertEqual((second.status, second.attempt), ("succeeded", 2))
        self.assertEqual(len(self.client.calls), 2)

    def test_permanent_or_exhausted_failure_blocks_health(self):
        change = cms_support.event()
        self.client.failures.append(
            ClientFailure("source_client.authentication", retryable=False)
        )
        self.outbox.enqueue_change(change, delivery_max_attempts=3)

        outcome = self.outbox.run_once("worker-1", lease_seconds=60)
        health = self.outbox.health()

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "source_client.authentication")
        self.assertEqual((health.status, health.failed), ("blocked", 1))
        self.assertEqual(health.error_code, "source_delivery.failed")

    def test_retryable_failure_stops_at_delivery_ceiling(self):
        change = cms_support.event()
        self.client.failures.extend((ClientFailure(), ClientFailure()))
        self.outbox.enqueue_change(change, delivery_max_attempts=2)

        first = self.outbox.run_once("worker-1", lease_seconds=60)
        self.now = first.next_attempt_at
        second = self.outbox.run_once("worker-1", lease_seconds=60)

        self.assertEqual((first.status, second.status), (
            "retry_wait", "failed",
        ))
        self.assertEqual(second.attempt, 2)
        self.assertEqual(self.outbox.health().error_code, "source_delivery.failed")

    def test_invalid_acknowledgement_is_terminal(self):
        change = cms_support.event()
        original = self.client.submit_change

        def altered(value, *, max_attempts):
            response = original(value, max_attempts=max_attempts)
            response["event_id"] = "other-event"
            return response

        self.client.submit_change = altered
        self.outbox.enqueue_change(change)

        outcome = self.outbox.run_once("worker-1", lease_seconds=60)

        self.assertEqual((outcome.status, outcome.error_code), (
            "failed", "source_delivery.response_invalid",
        ))
        self.assertEqual(len(self.client.calls), 1)

    def test_crash_after_remote_acceptance_replays_same_identity(self):
        change = cms_support.event()
        self.outbox.enqueue_change(change)
        abandoned = self.outbox.claim("crashed-worker", lease_seconds=60)
        accepted = self.client.submit_change(
            json.loads(abandoned.payload_json),
            max_attempts=abandoned.source_max_attempts,
        )
        self.assertEqual(accepted["request_id"], change["event_id"])

        self.now = 161
        replayed = self.outbox.run_once("recovery-worker", lease_seconds=60)

        self.assertEqual((replayed.status, replayed.attempt), ("succeeded", 2))
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.client.calls[0], self.client.calls[1])

    def test_expired_lease_cannot_complete_after_takeover(self):
        change = cms_support.event()
        self.outbox.enqueue_change(change)
        stale = self.outbox.claim("worker-1", lease_seconds=60)
        response = self.client.submit_change(
            json.loads(stale.payload_json),
            max_attempts=stale.source_max_attempts,
        )
        self.now = 161
        current = self.outbox.claim("worker-2", lease_seconds=60)

        with self.assertRaises(DELIVERY.CMSSourceDeliveryBlocked) as caught:
            self.outbox.complete(stale, response)
        self.assertEqual(caught.exception.code, "source_delivery.claim_lost")
        self.assertEqual(current.attempt, 2)

    def test_tampered_payload_blocks_without_network_access(self):
        change = cms_support.event()
        self.outbox.enqueue_change(change)
        self.connection.execute("""
            UPDATE cms_source_delivery_outbox
            SET payload_sha256 = ? WHERE request_id = ?
        """, ("0" * 64, change["event_id"]))
        self.connection.commit()

        health = self.outbox.health()
        with self.assertRaises(DELIVERY.CMSSourceDeliveryBlocked) as caught:
            self.outbox.run_once("worker-1", lease_seconds=60)

        self.assertEqual((health.status, health.error_code), (
            "blocked", "source_delivery.integrity",
        ))
        self.assertEqual(caught.exception.code, "source_delivery.integrity")
        self.assertEqual(self.client.calls, [])

    def test_contract_change_blocks_active_rows(self):
        change = cms_support.event()
        self.outbox.enqueue_change(change)
        replacement = ScriptedClient("b" * 64)
        restarted = DELIVERY.DurableCMSSourceDeliveryOutbox(
            self.connection, replacement, clock=lambda: self.now,
        )

        health = restarted.health()
        with self.assertRaises(DELIVERY.CMSSourceDeliveryBlocked) as caught:
            restarted.run_once("worker-1", lease_seconds=60)

        self.assertEqual(health.contract_mismatches, 1)
        self.assertEqual(caught.exception.code, "source_delivery.contract_changed")
        self.assertEqual(replacement.calls, [])

    def test_too_short_lease_blocks_before_claim(self):
        self.outbox.enqueue_removal(cms_support.tombstone())

        with self.assertRaises(DELIVERY.CMSSourceDeliveryBlocked) as caught:
            self.outbox.run_once("worker-1", lease_seconds=30)

        self.assertEqual(caught.exception.code, "source_delivery.lease_too_short")
        status = self.outbox.status("tombstone", "tombstone-1")
        self.assertEqual((status.status, status.attempts), ("pending", 0))
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
