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


MONITOR = load(
    "blun_test_website_localization_cms_lifecycle_monitor",
    ROOT / "integrations" / "website_localization_cms_lifecycle_monitor.py",
)


def dispatch_status(source=None, **overrides):
    source = support.event() if source is None else source
    _, _, digest = MONITOR._DISPATCH._change(source)
    value = {
        "event_id": source["event_id"],
        "payload_sha256": digest,
        "status": "succeeded",
        "remote_plan_id": "plan-1",
        "remote_job_count": len(source["localization"]["target_locales"]),
        "remote_status": "enqueued",
        "response_sha256": "a" * 64,
    }
    value.update(overrides)
    return value


def lifecycle(source=None, **overrides):
    source = support.event() if source is None else source
    required = sorted(source["localization"]["target_locales"])
    value = {
        "schema": support.API.LIFECYCLE_RESPONSE_SCHEMA,
        "request_id": "lifecycle-response-1",
        "event_id": source["event_id"],
        "site_id": source["site_id"],
        "plan_id": "plan-1",
        "website_version": source["website_version"],
        "source_sequence": source["source_sequence"],
        "status": "processing",
        "required_locales": required,
        "approved_locales": [],
        "blocked_locales": [],
        "queue_counts": {
            "failed": 0, "leased": 0, "pending": len(required),
            "retry_wait": 0, "succeeded": 0,
        },
        "delivery": None,
        "tombstone": None,
    }
    value.update(overrides)
    return value


class ScriptedClient:
    timeout = 30.0

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def lifecycle(self, event_id, site_id, *, request_id=None):
        self.calls.append((event_id, site_id, request_id))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return copy.deepcopy(outcome)


class DurableCMSLifecycleMonitorTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.monitor = MONITOR.DurableCMSLifecycleMonitor(
            self.connection, poll_interval_seconds=30,
            base_delay_seconds=5, max_delay_seconds=20,
        )

    def tearDown(self):
        self.connection.close()

    def test_dispatch_to_real_lifecycle_client_uses_fresh_signed_reads(self):
        queue_connection = sqlite3.connect(":memory:")
        release_connection = sqlite3.connect(":memory:")
        cms_connection = sqlite3.connect(":memory:")
        dispatch_connection = sqlite3.connect(":memory:")
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
            request_ids = iter(("fresh-lifecycle-1", "fresh-lifecycle-2"))
            client = support.CLIENT.CMSLocalizationHTTPClient(
                "https://localization.example.test", lambda: {}, authority,
                transport=transport, clock=lambda: 100,
                request_id_factory=lambda: next(request_ids),
            )
            source = support.event()
            dispatcher = MONITOR._DISPATCH.DurableCMSChangeDispatcher(
                dispatch_connection,
            )
            dispatcher.enqueue(source, now=100)
            dispatched = dispatcher.run_once(
                client, "dispatch-worker", now=100,
            )
            self.assertEqual(dispatched.status, "succeeded")
            registered = self.monitor.register(
                source, dispatcher.status(source["event_id"], now=100), now=100,
            )

            first = self.monitor.run_once(client, "monitor-worker", now=100)
            second = self.monitor.run_once(client, "monitor-worker", now=130)

            self.assertEqual(registered.state, "pending")
            self.assertEqual((first.state, first.remote_status), (
                "watching", "processing",
            ))
            self.assertEqual((second.state, second.attempt), ("watching", 2))
            signed_reads = [
                json.loads(item.decode("utf-8")) for item in authority.signed
                if json.loads(item.decode("utf-8")).get("schema")
                == support.API.LIFECYCLE_REQUEST_SCHEMA
            ]
            self.assertEqual(
                [item["request_id"] for item in signed_reads],
                ["fresh-lifecycle-1", "fresh-lifecycle-2"],
            )
            self.assertEqual(len(transport.calls), 3)
            self.assertNotIn(
                source["localization"]["source_text"],
                repr(self.monitor.status(source["event_id"], now=130)),
            )
        finally:
            dispatch_connection.close()
            cms_connection.close()
            release_connection.close()
            queue_connection.close()

    def test_retry_backoff_is_bounded_and_success_resets_failure_streak(self):
        source = support.event()
        self.monitor.register(
            source, dispatch_status(source),
            max_consecutive_failures=3, now=100,
        )
        client = ScriptedClient(
            support.CLIENT.CMSClientFailed("network", retryable=True),
            lifecycle(source),
            support.CLIENT.CMSClientFailed("network", retryable=True),
        )

        failed_once = self.monitor.run_once(client, "worker", now=100)
        early = self.monitor.run_once(client, "worker", now=104)
        recovered = self.monitor.run_once(client, "worker", now=105)
        failed_again = self.monitor.run_once(client, "worker", now=135)

        self.assertEqual((failed_once.state, failed_once.next_poll_at), (
            "retry_wait", 105,
        ))
        self.assertIsNone(early)
        self.assertEqual((recovered.state, recovered.consecutive_failures), (
            "watching", 0,
        ))
        self.assertEqual((failed_again.state, failed_again.next_poll_at), (
            "retry_wait", 140,
        ))
        self.assertEqual(client.calls, [
            ("event-1", "site-1", None),
            ("event-1", "site-1", None),
            ("event-1", "site-1", None),
        ])

    def test_retry_ceiling_and_nonretryable_failure_are_terminal(self):
        source = support.event()
        self.monitor.register(
            source, dispatch_status(source),
            max_consecutive_failures=2, now=100,
        )
        client = ScriptedClient(
            support.CLIENT.CMSClientFailed("network", retryable=True),
            support.CLIENT.CMSClientFailed("network", retryable=True),
        )
        self.assertEqual(
            self.monitor.run_once(client, "worker", now=100).state,
            "retry_wait",
        )
        exhausted = self.monitor.run_once(client, "worker", now=105)
        self.assertEqual((exhausted.state, exhausted.error_code), (
            "failed", "network",
        ))
        self.assertEqual(self.monitor.health(now=105).status, "blocked")

        second = support.event(event_id="event-2", source_revision="cms-2")
        self.monitor.register(second, dispatch_status(second), now=106)
        denied = ScriptedClient(
            support.CLIENT.CMSClientFailed(
                "cms.lifecycle.scope_rejected", retryable=False,
            )
        )
        outcome = self.monitor.run_once(denied, "worker", now=106)
        self.assertEqual((outcome.state, outcome.error_code), (
            "failed", "cms.lifecycle.scope_rejected",
        ))

    def test_terminal_remote_failure_remains_visible_and_blocks_health(self):
        source = support.event()
        self.monitor.register(source, dispatch_status(source), now=100)
        response = lifecycle(
            source, status="localization_failed",
            blocked_locales=[["mt-MT", "result.failed"]],
            queue_counts={
                "failed": 1, "leased": 0, "pending": 0,
                "retry_wait": 0, "succeeded": 1,
            },
        )
        outcome = self.monitor.run_once(
            ScriptedClient(response), "worker", now=100,
        )
        status = self.monitor.status("event-1", now=100)

        self.assertEqual((outcome.state, outcome.remote_status), (
            "terminal", "localization_failed",
        ))
        self.assertEqual(status.blocked_locales, (("mt-MT", "result.failed"),))
        self.assertEqual(self.monitor.health(now=100).remote_failures, 1)
        self.assertEqual(self.monitor.health(now=100).status, "blocked")
        self.assertIsNone(
            self.monitor.run_once(ScriptedClient(), "worker", now=100)
        )

    def test_wrong_generation_response_fails_closed(self):
        source = support.event()
        self.monitor.register(source, dispatch_status(source), now=100)
        altered = lifecycle(source, website_version="web-other")
        outcome = self.monitor.run_once(
            ScriptedClient(altered), "worker", now=100,
        )
        self.assertEqual((outcome.state, outcome.error_code), (
            "failed", "lifecycle_monitor.response_invalid",
        ))
        status = self.monitor.status("event-1", now=100)
        self.assertIsNone(status.lifecycle_sha256)
        self.assertEqual(status.required_locales, ())

    def test_observed_lifecycle_snapshot_is_hash_bound(self):
        source = support.event()
        self.monitor.register(source, dispatch_status(source), now=100)
        self.monitor.run_once(
            ScriptedClient(lifecycle(source)), "worker", now=100,
        )
        self.connection.execute("""
            UPDATE cms_source_lifecycle_monitor
            SET lifecycle_json = replace(
                lifecycle_json, '"status":"processing"', '"status":"ready"'
            )
            WHERE event_id = 'event-1'
        """)
        self.connection.commit()

        with self.assertRaises(MONITOR.LifecycleMonitorBlocked) as blocked:
            self.monitor.health(now=100)
        self.assertEqual(
            blocked.exception.code, "lifecycle_monitor.state_invalid",
        )

    def test_expired_lease_recovers_across_connections_and_stale_claim_loses(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "lifecycle.sqlite3"
            first_connection = sqlite3.connect(database)
            second_connection = sqlite3.connect(database)
            try:
                first = MONITOR.DurableCMSLifecycleMonitor(
                    first_connection, base_delay_seconds=5,
                )
                second = MONITOR.DurableCMSLifecycleMonitor(
                    second_connection, base_delay_seconds=5,
                )
                source = support.event()
                first.register(source, dispatch_status(source), now=100)
                old = first.claim("worker-1", now=100, lease_seconds=10)
                self.assertIsNone(second.claim("worker-2", now=109))
                self.assertIsNone(second.claim("worker-2", now=110))
                new = second.claim("worker-2", now=115, lease_seconds=10)

                with self.assertRaises(MONITOR.LifecycleMonitorBlocked) as lost:
                    first.complete(old, lifecycle(source), now=115)
                self.assertEqual(lost.exception.code, "lifecycle_monitor.claim_lost")
                completed = second.complete(new, lifecycle(source), now=115)
                self.assertEqual((completed.state, completed.poll_attempts), (
                    "watching", 2,
                ))
            finally:
                second_connection.close()
                first_connection.close()

    def test_registration_is_exact_idempotent_and_requires_dispatch_success(self):
        source = support.event()
        status = dispatch_status(source)
        first = self.monitor.register(source, status, now=100)
        replay = self.monitor.register(source, status, now=101)
        self.assertEqual(first.change_sha256, replay.change_sha256)

        changed = copy.deepcopy(source)
        changed["localization"]["source_text"] = "Different source"
        with self.assertRaises(MONITOR.LifecycleMonitorBlocked) as collision:
            self.monitor.register(changed, dispatch_status(changed), now=101)
        self.assertEqual(
            collision.exception.code, "lifecycle_monitor.idempotency_collision",
        )
        with self.assertRaises(MONITOR.LifecycleMonitorBlocked) as incomplete:
            self.monitor.register(
                support.event(event_id="event-2", source_revision="cms-2"),
                {**dispatch_status(
                    support.event(event_id="event-2", source_revision="cms-2")
                ), "status": "retry_wait"},
                now=101,
            )
        self.assertEqual(
            incomplete.exception.code, "lifecycle_monitor.dispatch_invalid",
        )

    def test_cancelled_and_superseded_dispatches_are_terminal_without_poll(self):
        for index, remote in enumerate(("cancelled", "superseded"), start=1):
            source = support.event(
                event_id=f"event-{index}", source_revision=f"cms-{index}",
            )
            registered = self.monitor.register(
                source, dispatch_status(source, remote_status=remote),
                now=100,
            )
            self.assertEqual((registered.state, registered.remote_status), (
                "terminal", remote,
            ))
        client = ScriptedClient()
        self.assertIsNone(self.monitor.run_once(client, "worker", now=100))
        self.assertEqual(client.calls, [])

    def test_tampering_and_mutated_claim_block_before_client_or_completion(self):
        source = support.event()
        self.monitor.register(source, dispatch_status(source), now=100)
        claim = self.monitor.claim("worker", now=100)
        altered = replace(claim, plan_id="plan-other")
        with self.assertRaises(MONITOR.LifecycleMonitorBlocked) as invalid:
            self.monitor.complete(altered, lifecycle(source), now=100)
        self.assertEqual(
            invalid.exception.code, "lifecycle_monitor.response_invalid",
        )

        self.connection.execute("""
            UPDATE cms_source_lifecycle_monitor
            SET website_version = 'tampered'
            WHERE event_id = 'event-1'
        """)
        self.connection.commit()
        with self.assertRaises(MONITOR.LifecycleMonitorBlocked) as health:
            self.monitor.health(now=100)
        self.assertEqual(health.exception.code, "lifecycle_monitor.state_invalid")

    def test_schema_drift_blocks_all_access(self):
        self.monitor.register(support.event(), dispatch_status(), now=100)
        self.connection.execute(
            "ALTER TABLE cms_source_lifecycle_monitor ADD COLUMN unexpected TEXT"
        )
        self.connection.commit()
        with self.assertRaises(MONITOR.LifecycleMonitorBlocked) as blocked:
            self.monitor.status("event-1", now=100)
        self.assertEqual(blocked.exception.code, "lifecycle_monitor.schema_altered")


if __name__ == "__main__":
    unittest.main()
