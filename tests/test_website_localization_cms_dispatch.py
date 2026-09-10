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


DISPATCH = load(
    "blun_test_website_localization_cms_dispatch",
    ROOT / "integrations" / "website_localization_cms_dispatch.py",
)


def response(event_id="event-1"):
    return {
        "schema": support.API.API_SCHEMA,
        "event_id": event_id,
        "plan_id": "plan-1",
        "job_count": 2,
        "inserted_jobs": 2,
        "status": "enqueued",
    }


class ScriptedClient:
    timeout = 30.0

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def submit_change(self, change):
        self.calls.append(copy.deepcopy(change))
        outcome = self.outcomes.pop(0) if self.outcomes else response()
        if isinstance(outcome, Exception):
            raise outcome
        return copy.deepcopy(outcome)


class DurableCMSChangeDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.dispatcher = DISPATCH.DurableCMSChangeDispatcher(
            self.connection, base_delay_seconds=5, max_delay_seconds=20,
        )

    def tearDown(self):
        self.connection.close()

    def test_real_client_replays_exact_event_after_post_acceptance_crash(self):
        queue_connection = sqlite3.connect(":memory:")
        release_connection = sqlite3.connect(":memory:")
        cms_connection = sqlite3.connect(":memory:")
        try:
            queue = support.CMS._QUEUE.LocalizationQueue(queue_connection)
            release = support.CMS._RELEASE.LocalizationReleaseStore(
                release_connection, queue,
            )
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
            source = support.event()
            queued = self.dispatcher.enqueue(source, now=100)

            first = self.dispatcher.claim(
                "source-worker-1", now=100, lease_seconds=10,
            )
            accepted = client.submit_change(json.loads(first.payload_json))
            replay = self.dispatcher.claim(
                "source-worker-2", now=110, lease_seconds=10,
            )
            replayed = client.submit_change(json.loads(replay.payload_json))
            completed = self.dispatcher.complete(replay, replayed, now=110)

            self.assertEqual(queued.status, "pending")
            self.assertEqual(accepted["inserted_jobs"], 2)
            self.assertEqual(replayed["inserted_jobs"], 0)
            self.assertEqual(first.payload_sha256, replay.payload_sha256)
            self.assertEqual(completed.status, "succeeded")
            self.assertEqual(len(transport.calls), 2)
            progress = client.status(
                source["event_id"], source["site_id"],
                request_id="dispatch-status-1",
            )
            self.assertEqual(progress["job_count"], 2)
            status = self.dispatcher.status(source["event_id"], now=100)
            self.assertEqual(status.remote_status, "enqueued")
            self.assertEqual(status.remote_job_count, 2)
            self.assertEqual(self.dispatcher.health(now=100).status, "ok")
            rendered = repr((queued, completed, status, self.dispatcher.health(now=110)))
            self.assertNotIn(source["localization"]["source_text"], rendered)
        finally:
            cms_connection.close()
            release_connection.close()
            queue_connection.close()

    def test_retry_is_durable_due_bound_and_reuses_exact_event(self):
        source = support.event()
        self.dispatcher.enqueue(source, max_attempts=3, now=100)
        client = ScriptedClient(
            support.CLIENT.CMSClientFailed("network", retryable=True),
            response(),
        )

        first = self.dispatcher.run_once(client, "worker-1", now=100)
        early = self.dispatcher.run_once(client, "worker-1", now=104)
        second = self.dispatcher.run_once(client, "worker-1", now=105)

        self.assertEqual(first.status, "retry_wait")
        self.assertEqual(first.next_attempt_at, 105)
        self.assertIsNone(early)
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls, [source, source])

    def test_expired_claim_replays_and_stale_completion_cannot_win(self):
        self.dispatcher.enqueue(support.event(), max_attempts=3, now=100)
        first = self.dispatcher.claim("worker-1", now=100, lease_seconds=10)
        self.assertIsNotNone(first)
        self.assertIsNone(
            self.dispatcher.claim("worker-2", now=109, lease_seconds=10)
        )

        second = self.dispatcher.claim("worker-2", now=110, lease_seconds=10)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(second.payload_sha256, first.payload_sha256)
        with self.assertRaises(DISPATCH.CMSDispatchBlocked) as stale:
            self.dispatcher.complete(first, response(), now=110)
        self.assertEqual(stale.exception.code, "dispatch.claim_lost")
        completed = self.dispatcher.complete(second, response(), now=110)
        self.assertEqual(completed.status, "succeeded")

    def test_exact_enqueue_is_idempotent_but_changed_content_collides(self):
        source = support.event()
        original = copy.deepcopy(source)
        first = self.dispatcher.enqueue(source, now=100)
        source["localization"]["source_text"] = "Changed after enqueue"

        replay = self.dispatcher.enqueue(original, now=101)
        with self.assertRaises(DISPATCH.CMSDispatchBlocked) as collision:
            self.dispatcher.enqueue(source, now=101)

        self.assertEqual(first.payload_sha256, replay.payload_sha256)
        self.assertEqual(collision.exception.code, "dispatch.idempotency_collision")
        claim = self.dispatcher.claim("worker-1", now=101)
        self.assertNotIn("Changed after enqueue", claim.payload_json)
        with self.assertRaises(DISPATCH.CMSDispatchBlocked):
            self.dispatcher.enqueue({"event_id": "bad"}, now=101)
        decomposed = support.event(
            event_id="event-decomposed", source_revision="cms-decomposed",
            source_text="Cafe\u0301",
        )
        with self.assertRaises(DISPATCH.CMSDispatchBlocked):
            self.dispatcher.enqueue(decomposed, now=101)

    def test_terminal_client_failure_and_attempt_ceiling_fail_closed(self):
        self.dispatcher.enqueue(support.event(), max_attempts=1, now=100)
        retrying = ScriptedClient(
            support.CLIENT.CMSClientFailed("network", retryable=True),
        )
        outcome = self.dispatcher.run_once(retrying, "worker-1", now=100)
        self.assertEqual((outcome.status, outcome.error_code), (
            "failed", "network",
        ))
        self.assertEqual(self.dispatcher.health(now=100).status, "blocked")

        other = support.event(event_id="event-2", source_revision="cms-2")
        self.dispatcher.enqueue(other, now=101)
        terminal = ScriptedClient(
            support.CLIENT.CMSClientFailed(
                "cms.event.invalid", retryable=False,
            ),
        )
        outcome = self.dispatcher.run_once(terminal, "worker-1", now=101)
        self.assertEqual((outcome.status, outcome.error_code), (
            "failed", "cms.event.invalid",
        ))

    def test_invalid_response_and_mutated_claim_are_terminal_or_rejected(self):
        self.dispatcher.enqueue(support.event(), now=100)
        invalid = response(event_id="other-event")
        outcome = self.dispatcher.run_once(
            ScriptedClient(invalid), "worker-1", now=100,
        )
        self.assertEqual((outcome.status, outcome.error_code), (
            "failed", "dispatch.response_invalid",
        ))

        other = support.event(event_id="event-2", source_revision="cms-2")
        self.dispatcher.enqueue(other, now=101)
        claim = self.dispatcher.claim("worker-1", now=101)
        altered = replace(claim, payload_json=claim.payload_json + " ")
        with self.assertRaises(DISPATCH.CMSDispatchBlocked) as blocked:
            self.dispatcher.complete(altered, response("event-2"), now=101)
        self.assertEqual(blocked.exception.code, "dispatch.claim_invalid")

    def test_tampered_payload_blocks_health_and_is_never_sent(self):
        self.dispatcher.enqueue(support.event(), now=100)
        self.connection.execute("""
            UPDATE cms_source_change_outbox
            SET payload_json = replace(payload_json, '€480', '€980')
            WHERE event_id = 'event-1'
        """)
        self.connection.commit()

        with self.assertRaises(DISPATCH.CMSDispatchBlocked) as health:
            self.dispatcher.health(now=100)
        self.assertEqual(health.exception.code, "dispatch.state_invalid")
        client = ScriptedClient(response())
        with self.assertRaises(DISPATCH.CMSDispatchBlocked) as claim:
            self.dispatcher.run_once(client, "worker-1", now=100)
        self.assertEqual(claim.exception.code, "dispatch.payload_integrity")
        self.assertEqual(client.calls, [])

    def test_two_connections_lease_one_event_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "source-outbox.sqlite3"
            first_connection = sqlite3.connect(database)
            second_connection = sqlite3.connect(database)
            try:
                first = DISPATCH.DurableCMSChangeDispatcher(first_connection)
                second = DISPATCH.DurableCMSChangeDispatcher(second_connection)
                first.enqueue(support.event(), now=100)

                claim = first.claim("worker-1", now=100)
                other = second.claim("worker-2", now=100)

                self.assertIsNotNone(claim)
                self.assertIsNone(other)
            finally:
                second_connection.close()
                first_connection.close()

    def test_schema_drift_blocks_before_status_or_dispatch(self):
        self.dispatcher.enqueue(support.event(), now=100)
        self.connection.execute(
            "ALTER TABLE cms_source_change_outbox ADD COLUMN unexpected TEXT"
        )
        self.connection.commit()

        with self.assertRaises(DISPATCH.CMSDispatchBlocked) as status:
            self.dispatcher.status("event-1", now=100)
        with self.assertRaises(DISPATCH.CMSDispatchBlocked) as dispatch:
            self.dispatcher.run_once(
                ScriptedClient(response()), "worker-1", now=100,
            )
        self.assertEqual(status.exception.code, "dispatch.schema_altered")
        self.assertEqual(dispatch.exception.code, "dispatch.schema_altered")


if __name__ == "__main__":
    unittest.main()
