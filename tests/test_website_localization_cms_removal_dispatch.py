from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests import test_website_localization_cms_client as support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REMOVAL = load(
    "blun_test_website_localization_cms_removal_dispatch",
    ROOT / "integrations" / "website_localization_cms_removal_dispatch.py",
)


def cancellation_response(value=None, *, newly=True):
    value = support.cancellation() if value is None else value
    return {
        "schema": support.API.API_SCHEMA,
        "cancellation_id": value["cancellation_id"],
        "event_id": value["event_id"],
        "status": "cancelled",
        "newly_cancelled": newly,
    }


def tombstone_response(value=None, *, newly=True):
    value = support.tombstone() if value is None else value
    return {
        "schema": support.API.API_SCHEMA,
        "tombstone_id": value["tombstone_id"],
        "event_id": value["event_id"],
        "delivery_id": "blun-cms-tombstone-" + "a" * 64,
        "status": "pending",
        "newly_requested": newly,
    }


class ScriptedClient:
    timeout = 30.0

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def _call(self, operation, request):
        self.calls.append((operation, copy.deepcopy(request)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return copy.deepcopy(outcome)

    def cancel(self, request):
        return self._call("cancellation", request)

    def request_tombstone(self, request):
        return self._call("tombstone", request)


class DurableCMSRemovalDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.dispatcher = REMOVAL.DurableCMSRemovalDispatcher(
            self.connection, base_delay_seconds=5, max_delay_seconds=20,
        )

    def tearDown(self):
        self.connection.close()

    @staticmethod
    def real_client():
        queue_connection = sqlite3.connect(":memory:")
        queue = support.CMS._QUEUE.LocalizationQueue(queue_connection)
        release_connection = sqlite3.connect(":memory:")
        release = support.CMS._RELEASE.LocalizationReleaseStore(
            release_connection, queue,
        )
        cms_connection = sqlite3.connect(":memory:")
        bridge = support.CMS.WebsiteLocalizationCMSBridge(
            cms_connection, queue, release,
        )
        authority = support.Authority()
        api = support.API.WebsiteLocalizationAPI(
            bridge, authority, clock=lambda: 100,
            approval_authority=authority,
            publication_authority=authority,
        )
        transport = support.WSGITransport(api)
        client = support.CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, authority,
            transport=transport, clock=lambda: 100,
        )
        return (
            client, bridge, transport,
            (cms_connection, release_connection, queue_connection),
        )

    def test_cancellation_replays_after_remote_acceptance_crash(self):
        client, _bridge, transport, connections = self.real_client()
        try:
            source = support.event()
            client.submit_change(source)
            request = support.cancellation(source)
            queued = self.dispatcher.enqueue(request, now=100)

            first = self.dispatcher.claim(
                "source-worker-1", now=100, lease_seconds=10,
            )
            accepted = client.cancel(json.loads(first.payload_json))
            replay = self.dispatcher.claim(
                "source-worker-2", now=110, lease_seconds=10,
            )
            replayed = client.cancel(json.loads(replay.payload_json))
            completed = self.dispatcher.complete(replay, replayed, now=110)

            self.assertEqual(queued.status, "pending")
            self.assertTrue(accepted["newly_cancelled"])
            self.assertFalse(replayed["newly_cancelled"])
            self.assertEqual(first.payload_sha256, replay.payload_sha256)
            self.assertEqual(completed.status, "succeeded")
            self.assertEqual(completed.remote_status, "cancelled")
            self.assertFalse(completed.remote_new)
            self.assertEqual(len(transport.calls), 3)
            self.assertEqual(self.dispatcher.health(now=110).status, "ok")
            rendered = repr((queued, completed, self.dispatcher.health(now=110)))
            self.assertNotIn(source["localization"]["source_text"], rendered)
        finally:
            for connection in connections:
                connection.close()

    def test_tombstone_replays_exactly_through_signed_api(self):
        client, bridge, transport, connections = self.real_client()
        seen = {}
        accepted_count = 0

        def accept(value, _signature, _verifier, _authority, **_options):
            nonlocal accepted_count
            previous = seen.setdefault(value["tombstone_id"], copy.deepcopy(value))
            self.assertEqual(previous, value)
            accepted_count += 1
            return support.CMS.TombstoneAccepted(
                value["tombstone_id"], value["event_id"],
                "blun-cms-tombstone-" + "a" * 64,
                "pending", accepted_count == 1,
            )

        bridge.request_tombstone = accept
        try:
            request = support.tombstone()
            self.dispatcher.enqueue(request, now=100)
            first = self.dispatcher.claim(
                "source-worker-1", now=100, lease_seconds=10,
            )
            accepted = client.request_tombstone(json.loads(first.payload_json))
            replay = self.dispatcher.claim(
                "source-worker-2", now=110, lease_seconds=10,
            )
            replayed = client.request_tombstone(json.loads(replay.payload_json))
            completed = self.dispatcher.complete(replay, replayed, now=110)

            self.assertTrue(accepted["newly_requested"])
            self.assertFalse(replayed["newly_requested"])
            self.assertEqual(completed.status, "succeeded")
            self.assertEqual(completed.remote_status, "pending")
            self.assertEqual(
                completed.remote_delivery_id,
                "blun-cms-tombstone-" + "a" * 64,
            )
            self.assertEqual(len(transport.calls), 2)
        finally:
            for connection in connections:
                connection.close()

    def test_both_operations_use_one_fifo_and_their_exact_client_method(self):
        cancelled = support.cancellation()
        deleted = support.tombstone()
        self.dispatcher.enqueue(cancelled, now=100)
        self.dispatcher.enqueue(deleted, now=101)
        client = ScriptedClient(
            cancellation_response(cancelled), tombstone_response(deleted),
        )

        first = self.dispatcher.run_once(client, "worker-1", now=101)
        second = self.dispatcher.run_once(client, "worker-1", now=101)

        self.assertEqual(first.operation, "cancellation")
        self.assertEqual(second.operation, "tombstone")
        self.assertEqual(client.calls, [
            ("cancellation", cancelled), ("tombstone", deleted),
        ])
        health = self.dispatcher.health(now=101)
        self.assertEqual(health.operations, {"cancellation": 1, "tombstone": 1})

    def test_retry_is_bounded_due_and_preserves_exact_operation(self):
        request = support.tombstone()
        self.dispatcher.enqueue(request, max_attempts=3, now=100)
        client = ScriptedClient(
            support.CLIENT.CMSClientFailed("network", retryable=True),
            tombstone_response(request),
        )

        first = self.dispatcher.run_once(client, "worker-1", now=100)
        early = self.dispatcher.run_once(client, "worker-1", now=104)
        second = self.dispatcher.run_once(client, "worker-1", now=105)

        self.assertEqual((first.status, first.next_attempt_at), (
            "retry_wait", 105,
        ))
        self.assertIsNone(early)
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(client.calls, [
            ("tombstone", request), ("tombstone", request),
        ])

    def test_exact_enqueue_is_idempotent_and_collisions_fail_closed(self):
        request = support.cancellation()
        original = copy.deepcopy(request)
        first = self.dispatcher.enqueue(request, now=100)
        request["website_version"] = "changed-after-enqueue"

        replay = self.dispatcher.enqueue(original, now=101)
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as collision:
            self.dispatcher.enqueue(request, now=101)

        same_id_other_operation = support.tombstone()
        same_id_other_operation["tombstone_id"] = original["cancellation_id"]
        other = self.dispatcher.enqueue(same_id_other_operation, now=102)
        self.assertEqual(first.payload_sha256, replay.payload_sha256)
        self.assertEqual(collision.exception.code, "removal.idempotency_collision")
        self.assertEqual(other.operation, "tombstone")
        claim = self.dispatcher.claim("worker-1", now=102)
        self.assertNotIn("changed-after-enqueue", claim.payload_json)
        with self.assertRaises(REMOVAL.CMSRemovalBlocked):
            self.dispatcher.enqueue({"schema": "wrong"}, now=102)

    def test_expired_claim_replays_but_stale_completion_cannot_win(self):
        request = support.cancellation()
        self.dispatcher.enqueue(request, max_attempts=3, now=100)
        first = self.dispatcher.claim(
            "worker-1", now=100, lease_seconds=10,
        )
        self.assertIsNone(self.dispatcher.claim(
            "worker-2", now=109, lease_seconds=10,
        ))

        second = self.dispatcher.claim(
            "worker-2", now=110, lease_seconds=10,
        )
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as stale:
            self.dispatcher.complete(
                first, cancellation_response(request), now=110,
            )
        self.assertEqual(stale.exception.code, "removal.claim_lost")
        completed = self.dispatcher.complete(
            second, cancellation_response(request), now=110,
        )
        self.assertEqual((second.attempt, completed.status), (2, "succeeded"))

    def test_terminal_failure_response_binding_and_attempt_ceiling(self):
        request = support.cancellation()
        self.dispatcher.enqueue(request, max_attempts=1, now=100)
        outcome = self.dispatcher.run_once(
            ScriptedClient(support.CLIENT.CMSClientFailed(
                "network", retryable=True,
            )),
            "worker-1", now=100,
        )
        self.assertEqual((outcome.status, outcome.error_code), (
            "failed", "network",
        ))

        other = support.tombstone(support.event(
            event_id="event-2", source_revision="cms-2",
        ))
        self.dispatcher.enqueue(other, now=101)
        invalid = tombstone_response(other)
        invalid["event_id"] = "event-other"
        outcome = self.dispatcher.run_once(
            ScriptedClient(invalid), "worker-1", now=101,
        )
        self.assertEqual((outcome.status, outcome.error_code), (
            "failed", "removal.response_invalid",
        ))
        self.assertEqual(self.dispatcher.health(now=101).status, "blocked")

    def test_mutated_claim_and_tampered_payload_block_before_network(self):
        request = support.cancellation()
        self.dispatcher.enqueue(request, now=100)
        claim = self.dispatcher.claim("worker-1", now=100)
        altered = replace(claim, event_id="event-other")
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as changed:
            self.dispatcher.complete(
                altered, cancellation_response(request), now=100,
            )
        self.assertEqual(changed.exception.code, "removal.claim_invalid")
        malformed = replace(claim, payload_json="{")
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as malformed_error:
            self.dispatcher.complete(
                malformed, cancellation_response(request), now=100,
            )
        self.assertEqual(
            malformed_error.exception.code, "removal.claim_invalid",
        )

        other = support.tombstone(support.event(
            event_id="event-2", source_revision="cms-2",
        ))
        self.dispatcher.enqueue(other, now=101)
        self.connection.execute("""
            UPDATE cms_source_removal_outbox
            SET payload_json = replace(payload_json, 'web-1', 'web-9')
            WHERE operation = 'tombstone'
        """)
        self.connection.commit()
        client = ScriptedClient(tombstone_response(other))
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as health:
            self.dispatcher.health(now=101)
        self.assertEqual(health.exception.code, "removal.state_invalid")
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as dispatch:
            self.dispatcher.run_once(client, "worker-1", now=101)
        self.assertEqual(dispatch.exception.code, "removal.payload_integrity")
        self.assertEqual(client.calls, [])

    def test_completed_acknowledgement_tamper_blocks_health_and_status(self):
        request = support.cancellation()
        self.dispatcher.enqueue(request, now=100)
        completed = self.dispatcher.run_once(
            ScriptedClient(cancellation_response(request, newly=True)),
            "worker-1", now=100,
        )
        self.assertEqual(completed.status, "succeeded")
        self.connection.execute("""
            UPDATE cms_source_removal_outbox
            SET remote_new = 0
            WHERE operation = 'cancellation' AND request_id = 'cancel-1'
        """)
        self.connection.commit()

        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as status:
            self.dispatcher.status("cancellation", "cancel-1", now=100)
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as health:
            self.dispatcher.health(now=100)
        self.assertEqual(status.exception.code, "removal.state_invalid")
        self.assertEqual(health.exception.code, "removal.state_invalid")

    def test_two_connections_lease_one_removal_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "source-removals.sqlite3"
            first_connection = sqlite3.connect(database)
            second_connection = sqlite3.connect(database)
            try:
                first = REMOVAL.DurableCMSRemovalDispatcher(first_connection)
                second = REMOVAL.DurableCMSRemovalDispatcher(second_connection)
                first.enqueue(support.cancellation(), now=100)

                claim = first.claim("worker-1", now=100)
                other = second.claim("worker-2", now=100)

                self.assertIsNotNone(claim)
                self.assertIsNone(other)
            finally:
                second_connection.close()
                first_connection.close()

    def test_schema_drift_blocks_status_and_dispatch(self):
        self.dispatcher.enqueue(support.cancellation(), now=100)
        self.connection.execute(
            "ALTER TABLE cms_source_removal_outbox ADD COLUMN unexpected TEXT"
        )
        self.connection.commit()

        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as status:
            self.dispatcher.status("cancellation", "cancel-1", now=100)
        with self.assertRaises(REMOVAL.CMSRemovalBlocked) as dispatch:
            self.dispatcher.run_once(
                ScriptedClient(cancellation_response()),
                "worker-1", now=100,
            )
        self.assertEqual(status.exception.code, "removal.schema_altered")
        self.assertEqual(dispatch.exception.code, "removal.schema_altered")


if __name__ == "__main__":
    unittest.main()
