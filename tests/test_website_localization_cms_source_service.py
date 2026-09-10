from __future__ import annotations

import copy
import importlib.util
import sqlite3
import sys
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


SERVICE = load(
    "blun_test_website_localization_cms_source_service",
    ROOT / "integrations" / "website_localization_cms_source_service.py",
)


class ScriptedClient:
    timeout = 30.0

    def __init__(self):
        self.calls = []
        self.events = {}
        self.submit_error = None

    def submit_change(self, change):
        self.calls.append(("change", copy.deepcopy(change)))
        if self.submit_error is not None:
            raise self.submit_error
        self.events[change["event_id"]] = copy.deepcopy(change)
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.API_SCHEMA,
            "event_id": change["event_id"],
            "plan_id": "plan-" + change["event_id"],
            "job_count": len(change["localization"]["target_locales"]),
            "inserted_jobs": len(change["localization"]["target_locales"]),
            "status": "enqueued",
        }

    def cancel(self, request):
        self.calls.append(("cancellation", copy.deepcopy(request)))
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.API_SCHEMA,
            "cancellation_id": request["cancellation_id"],
            "event_id": request["event_id"],
            "status": "cancelled",
            "newly_cancelled": True,
        }

    def request_tombstone(self, request):
        self.calls.append(("tombstone", copy.deepcopy(request)))
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.API_SCHEMA,
            "tombstone_id": request["tombstone_id"],
            "event_id": request["event_id"],
            "delivery_id": "delivery-" + request["event_id"],
            "status": "pending",
            "newly_requested": True,
        }

    def lifecycle(self, event_id, site_id):
        self.calls.append(("lifecycle", event_id, site_id))
        change = self.events[event_id]
        required = sorted(change["localization"]["target_locales"])
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.LIFECYCLE_RESPONSE_SCHEMA,
            "request_id": "lifecycle-" + event_id,
            "event_id": event_id,
            "site_id": site_id,
            "plan_id": "plan-" + event_id,
            "website_version": change["website_version"],
            "source_sequence": change["source_sequence"],
            "status": "processing",
            "required_locales": required,
            "approved_locales": [],
            "blocked_locales": [],
            "queue_counts": {
                "failed": 0,
                "leased": 0,
                "pending": len(required),
                "retry_wait": 0,
                "succeeded": 0,
            },
            "delivery": None,
            "tombstone": None,
        }


class CMSLocalizationSourceServiceTests(unittest.TestCase):
    def setUp(self):
        self.connections = [sqlite3.connect(":memory:") for _ in range(3)]
        self.now = 100.0
        self.client = ScriptedClient()
        self.service = self.build()

    def tearDown(self):
        for connection in self.connections:
            connection.close()

    def build(self):
        return SERVICE.CMSLocalizationSourceService(
            *self.connections,
            self.client,
            change_worker_id="change-worker",
            removal_worker_id="removal-worker",
            lifecycle_worker_id="lifecycle-worker",
            clock=lambda: self.now,
            change_lease_seconds=60,
            removal_lease_seconds=60,
            lifecycle_lease_seconds=60,
            lifecycle_poll_interval_seconds=30,
        )

    def test_change_is_dispatched_registered_and_polled_without_host_handoff(self):
        change = support.event()
        queued = self.service.enqueue_change(change)

        dispatched = self.service.run_once()
        registered = self.service.lifecycle_monitor.status(
            change["event_id"], now=self.now,
        )
        polled = self.service.run_once()

        self.assertEqual(queued.status, "pending")
        self.assertEqual((dispatched.phase, dispatched.status), (
            "change", "succeeded",
        ))
        self.assertEqual(registered.state, "pending")
        self.assertEqual((polled.phase, polled.status), (
            "lifecycle", "watching",
        ))
        self.assertEqual(
            [call[0] for call in self.client.calls],
            ["change", "lifecycle"],
        )
        health = self.service.health()
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.pending_lifecycle_registrations, 0)
        self.assertNotIn(change["localization"]["source_text"], repr(health))

    def test_restart_recovers_post_acceptance_registration_gap_without_resend(self):
        change = support.event()
        self.service.enqueue_change(change)
        accepted = self.service.changes.run_once(
            self.client, "crashed-worker", now=self.now, lease_seconds=60,
        )
        self.assertEqual(accepted.status, "succeeded")
        gap = self.service.health()
        self.assertEqual((gap.status, gap.pending_lifecycle_registrations), (
            "degraded", 1,
        ))
        self.assertEqual(
            gap.error_code,
            "source_service.lifecycle_registration_pending",
        )

        restarted = self.build()
        recovered = restarted.run_once()

        self.assertEqual((recovered.phase, recovered.status), (
            "lifecycle_registration", "registered",
        ))
        self.assertEqual([call[0] for call in self.client.calls], ["change"])
        self.assertEqual(
            restarted.lifecycle_monitor.status(
                change["event_id"], now=self.now,
            ).change_sha256,
            accepted.event_id and restarted.changes.status(
                change["event_id"], now=self.now,
            ).payload_sha256,
        )

    def test_restart_after_registration_commit_does_not_resend_change(self):
        change = support.event()
        self.service.enqueue_change(change)
        register = self.service.lifecycle_monitor.register

        def committed_then_crashed(*args, **kwargs):
            register(*args, **kwargs)
            raise RuntimeError(change["localization"]["source_text"])

        self.service.lifecycle_monitor.register = committed_then_crashed
        interrupted = self.service.run_once()
        self.assertEqual((interrupted.phase, interrupted.status), (
            "lifecycle_registration", "blocked",
        ))
        self.service.lifecycle_monitor.register = register

        resumed = self.service.run_once()

        self.assertEqual((resumed.phase, resumed.status), (
            "lifecycle", "watching",
        ))
        self.assertEqual(
            [call[0] for call in self.client.calls],
            ["change", "lifecycle"],
        )
        self.assertNotIn(change["localization"]["source_text"], repr(interrupted))

    def test_removal_has_priority_over_new_change(self):
        change = support.event()
        self.service.enqueue_change(change)
        self.service.enqueue_removal(support.cancellation(change))

        first = self.service.run_once()
        second = self.service.run_once()

        self.assertEqual((first.phase, first.operation, first.status), (
            "removal", "cancellation", "succeeded",
        ))
        self.assertEqual((second.phase, second.status), (
            "change", "succeeded",
        ))
        self.assertEqual(
            [call[0] for call in self.client.calls],
            ["cancellation", "change"],
        )

    def test_conflicting_lifecycle_binding_blocks_before_network(self):
        change = support.event()
        self.service.enqueue_change(change)
        accepted = self.service.changes.run_once(
            self.client, "dispatch-worker", now=self.now, lease_seconds=60,
        )
        dispatch = self.service.changes.status(
            accepted.event_id, now=self.now,
        )
        self.service.lifecycle_monitor.register(
            change,
            replace(dispatch, remote_plan_id="other-plan"),
            now=self.now,
        )
        before = len(self.client.calls)

        outcome = self.service.run_once()
        health = self.service.health()

        self.assertEqual((outcome.phase, outcome.status, outcome.error_code), (
            "lifecycle_registration",
            "blocked",
            "source_service.lifecycle_binding_mismatch",
        ))
        self.assertEqual(len(self.client.calls), before)
        self.assertEqual((health.status, health.error_code), (
            "blocked", "source_service.lifecycle_binding_mismatch",
        ))

    def test_private_client_exception_is_reduced_to_stable_failure(self):
        change = support.event()
        self.service.enqueue_change(change, max_attempts=1)
        self.client.submit_error = RuntimeError(
            change["localization"]["source_text"],
        )

        outcome = self.service.run_once()

        self.assertEqual((outcome.phase, outcome.status, outcome.error_code), (
            "change", "failed", "dispatch.client_failure",
        ))
        self.assertNotIn(change["localization"]["source_text"], repr(outcome))
        self.assertEqual(self.service.health().status, "blocked")

    def test_run_forever_uses_bounded_state_specific_delays(self):
        delays = []
        checks = 0

        def stop():
            nonlocal checks
            checks += 1
            return checks > 2

        self.service.run_forever(
            stop,
            sleeper=delays.append,
            active_delay_seconds=0.1,
            idle_delay_seconds=2,
            blocked_delay_seconds=7,
        )
        self.assertEqual(delays, [2, 2])

    def test_invalid_configuration_writes_no_schema(self):
        connection = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(SERVICE.CMSSourceServiceBlocked) as reused:
                SERVICE.CMSLocalizationSourceService(
                    connection,
                    connection,
                    connection,
                    self.client,
                    change_worker_id="change-worker",
                    removal_worker_id="removal-worker",
                    lifecycle_worker_id="lifecycle-worker",
                )
            self.assertEqual(
                reused.exception.code, "source_service.connection_reused",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall(),
                [],
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
