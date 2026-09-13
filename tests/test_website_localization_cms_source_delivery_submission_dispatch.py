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

from tests import test_website_localization_cms_client as cms_support
from tests import (
    test_website_localization_cms_source_delivery_submission_client
    as client_support,
)


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DISPATCH = load(
    "blun_test_website_localization_submission_dispatch",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_dispatch.py",
)


class ScriptedClient:
    timeout = 30.0

    def __init__(self, digest, *outcomes):
        self.expected_capabilities_sha256 = digest
        self.outcomes = list(outcomes)
        self.calls = []

    def _call(self, operation, payload, **budgets):
        self.calls.append((operation, copy.deepcopy(payload), dict(budgets)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return copy.deepcopy(outcome)

    def submit_change(self, payload, **budgets):
        return self._call("change", payload, **budgets)

    def submit_removal(self, payload, **budgets):
        return self._call("removal", payload, **budgets)


class DurableSourceDeliverySubmissionDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.support = client_support.SourceDeliverySubmissionClientTests(
            methodName="runTest",
        )
        self.support.setUp()
        self.connection = sqlite3.connect(":memory:")
        self.dispatcher = DISPATCH.DurableCMSSourceDeliverySubmissionDispatcher(
            self.connection, self.support.digest,
            base_delay_seconds=5, max_delay_seconds=20,
        )

    def tearDown(self):
        self.connection.close()
        self.support.tearDown()

    def test_real_public_client_delivers_all_operations_durably(self):
        change = cms_support.event()
        cancellation = cms_support.cancellation(change)
        tombstone = cms_support.tombstone(change)
        expected = (
            ("change", change["event_id"]),
            ("cancellation", cancellation["cancellation_id"]),
            ("tombstone", tombstone["tombstone_id"]),
        )
        for payload in (change, cancellation, tombstone):
            queued = self.dispatcher.enqueue(
                payload, source_max_attempts=3,
                delivery_max_attempts=4, client_max_attempts=2, now=100,
            )
            self.assertEqual(queued.status, "pending")

        outcomes = []
        while True:
            outcome = self.dispatcher.run_once(
                self.support.client, "cms-worker", now=100,
            )
            if outcome is None:
                break
            outcomes.append(outcome)

        self.assertEqual(len(outcomes), 3)
        self.assertEqual(
            [outcome.operation for outcome in outcomes],
            ["cancellation", "tombstone", "change"],
        )
        for operation, request_id in expected:
            status = self.dispatcher.status(operation, request_id, now=100)
            self.assertEqual(status.status, "accepted")
            self.assertEqual(status.remote_status, "pending")
            self.assertEqual(status.source_max_attempts, 3)
            self.assertEqual(status.delivery_max_attempts, 4)
            self.assertEqual(
                status.remote_capabilities_sha256,
                self.support.runtime.submission_capabilities().as_payload()[
                    "website_capability_binding"
                ]["delivery_capabilities_sha256"],
            )
        health = self.dispatcher.health(now=100)
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.counts["accepted"], 3)
        self.assertEqual(health.operations, {
            "change": 1, "cancellation": 1, "tombstone": 1,
        })
        rendered = repr((outcomes, health))
        self.assertNotIn(change["localization"]["source_text"], rendered)
        self.assertNotIn("target_text", rendered)

        self.connection.execute("""
            UPDATE cms_public_submission_outbox
            SET response_json = replace(response_json, '"status":"pending"',
                                                    '"status":"failed"')
            WHERE operation = 'change'
        """)
        self.connection.commit()
        with self.assertRaises(
            DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
        ) as tampered:
            self.dispatcher.health(now=100)
        self.assertEqual(
            tampered.exception.code,
            "source_delivery_submission_dispatch.state_invalid",
        )

    def test_post_acceptance_crash_replays_exact_idempotent_request(self):
        change = cms_support.event()
        self.dispatcher.enqueue(
            change, source_max_attempts=3, delivery_max_attempts=4,
            client_max_attempts=3, now=100,
        )
        first = self.dispatcher.claim(
            "worker-1", now=100, lease_seconds=40,
        )
        accepted = self.support.client.submit_change(
            json.loads(first.payload_json), source_max_attempts=3,
            delivery_max_attempts=4,
        )

        second = self.dispatcher.claim(
            "worker-2", now=140, lease_seconds=40,
        )
        replayed = self.support.client.submit_change(
            json.loads(second.payload_json), source_max_attempts=3,
            delivery_max_attempts=4,
        )
        completed = self.dispatcher.complete(second, replayed, now=140)

        self.assertEqual(accepted, replayed)
        self.assertEqual(first.payload_json, second.payload_json)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(completed.status, "accepted")
        with self.assertRaises(
            DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
        ) as stale:
            self.dispatcher.complete(first, accepted, now=140)
        self.assertEqual(
            stale.exception.code,
            "source_delivery_submission_dispatch.claim_lost",
        )

    def test_retryable_failure_is_due_bound_and_attempt_limited(self):
        change = cms_support.event()
        accepted = self.support.client.submit_change(
            change, source_max_attempts=3, delivery_max_attempts=4,
        )
        failure = client_support.CLIENT.CMSSourceDeliverySubmissionClientBlocked(
            "source_delivery_submission_client.network", retryable=True,
        )
        client = ScriptedClient(self.support.digest, failure, accepted)
        self.dispatcher.enqueue(
            change, source_max_attempts=3, delivery_max_attempts=4,
            client_max_attempts=2, now=100,
        )

        first = self.dispatcher.run_once(client, "worker", now=100)
        early = self.dispatcher.run_once(client, "worker", now=104)
        second = self.dispatcher.run_once(client, "worker", now=105)

        self.assertEqual((first.status, first.next_attempt_at), (
            "retry_wait", 105,
        ))
        self.assertIsNone(early)
        self.assertEqual(second.status, "accepted")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0], client.calls[1])

    def test_terminal_failure_and_retry_ceiling_stay_failed(self):
        change = cms_support.event()
        terminal = client_support.CLIENT.CMSSourceDeliverySubmissionClientBlocked(
            "source_delivery_submission_client.redirect", retryable=False,
        )
        self.dispatcher.enqueue(change, client_max_attempts=2, now=100)
        outcome = self.dispatcher.run_once(
            ScriptedClient(self.support.digest, terminal), "worker", now=100,
        )
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(self.dispatcher.health(now=100).status, "blocked")

        other = cms_support.event(
            event_id="event-2", source_revision="cms-2",
        )
        transient = client_support.CLIENT.CMSSourceDeliverySubmissionClientBlocked(
            "source_delivery_submission_client.network", retryable=True,
        )
        self.dispatcher.enqueue(other, client_max_attempts=1, now=101)
        outcome = self.dispatcher.run_once(
            ScriptedClient(self.support.digest, transient), "worker", now=101,
        )
        self.assertEqual((outcome.status, outcome.attempt), ("failed", 1))

        invalid = cms_support.event(
            event_id="event-3", source_revision="cms-3",
        )
        accepted = self.support.client.submit_change(invalid)
        accepted["schema"] = "blun.injected-response.v1"
        self.dispatcher.enqueue(invalid, now=102)
        outcome = self.dispatcher.run_once(
            ScriptedClient(self.support.digest, accepted), "worker", now=102,
        )
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            outcome.error_code,
            "source_delivery_submission_dispatch.response_invalid",
        )

    def test_idempotency_binds_payload_and_all_three_retry_budgets(self):
        change = cms_support.event()
        original = copy.deepcopy(change)
        first = self.dispatcher.enqueue(
            change, source_max_attempts=2, delivery_max_attempts=3,
            client_max_attempts=4, now=100,
        )
        replay = self.dispatcher.enqueue(
            original, source_max_attempts=2, delivery_max_attempts=3,
            client_max_attempts=4, now=101,
        )
        self.assertEqual(first, replay)

        change["localization"]["source_text"] = "Changed after enqueue"
        with self.assertRaises(
            DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
        ) as collision:
            self.dispatcher.enqueue(
                change, source_max_attempts=2, delivery_max_attempts=3,
                client_max_attempts=4, now=101,
            )
        self.assertEqual(
            collision.exception.code,
            "source_delivery_submission_dispatch.idempotency_collision",
        )
        with self.assertRaises(
            DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
        ):
            self.dispatcher.enqueue(
                original, source_max_attempts=2, delivery_max_attempts=4,
                client_max_attempts=4, now=101,
            )

    def test_capability_drift_blocks_before_claim_or_network(self):
        change = cms_support.event()
        self.dispatcher.enqueue(change, now=100)
        client = ScriptedClient("e" * 64)

        with self.assertRaises(
            DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
        ) as blocked:
            self.dispatcher.run_once(client, "worker", now=100)

        self.assertEqual(
            blocked.exception.code,
            "source_delivery_submission_dispatch.client_invalid",
        )
        self.assertEqual(client.calls, [])
        self.assertEqual(
            self.dispatcher.status("change", "event-1", now=100).attempts, 0,
        )

    def test_tampering_and_stale_claims_never_reach_the_client(self):
        change = cms_support.event()
        self.dispatcher.enqueue(change, now=100)
        self.connection.execute("""
            UPDATE cms_public_submission_outbox
            SET payload_json = replace(payload_json, '€480', '€980')
        """)
        self.connection.commit()
        client = ScriptedClient(self.support.digest)

        with self.assertRaises(
            DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
        ) as health:
            self.dispatcher.health(now=100)
        with self.assertRaises(
            DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
        ) as run:
            self.dispatcher.run_once(client, "worker", now=100)
        self.assertEqual(
            health.exception.code,
            "source_delivery_submission_dispatch.state_invalid",
        )
        self.assertEqual(
            run.exception.code,
            "source_delivery_submission_dispatch.payload_integrity",
        )
        self.assertEqual(client.calls, [])

    def test_restart_binding_and_two_workers_preserve_one_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "public-submissions.sqlite3"
            first_connection = sqlite3.connect(database)
            second_connection = sqlite3.connect(database)
            try:
                first = DISPATCH.DurableCMSSourceDeliverySubmissionDispatcher(
                    first_connection, self.support.digest,
                )
                second = DISPATCH.DurableCMSSourceDeliverySubmissionDispatcher(
                    second_connection, self.support.digest,
                )
                first.enqueue(cms_support.event(), now=100)
                claim = first.claim("worker-1", now=100)
                other = second.claim("worker-2", now=100)

                self.assertIsNotNone(claim)
                self.assertIsNone(other)
                with self.assertRaises(
                    DISPATCH.CMSSourceDeliverySubmissionDispatchBlocked,
                ) as drift:
                    DISPATCH.DurableCMSSourceDeliverySubmissionDispatcher(
                        second_connection, "e" * 64,
                    )
                self.assertEqual(
                    drift.exception.code,
                    "source_delivery_submission_dispatch.schema_altered",
                )
            finally:
                second_connection.close()
                first_connection.close()


if __name__ == "__main__":
    unittest.main()
