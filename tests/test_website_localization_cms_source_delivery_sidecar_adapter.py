from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from tests import test_website_localization_cms_client as cms_support
from tests import (
    test_website_localization_cms_source_delivery_client as sidecar_support,
)


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = load(
    "blun_test_website_localization_cms_source_delivery_sidecar_adapter",
    ROOT / "integrations" /
    "website_localization_cms_source_delivery_sidecar_adapter.py",
)
DELIVERY = load(
    "blun_test_website_localization_cms_source_delivery_for_sidecar_adapter",
    ROOT / "integrations" / "website_localization_cms_source_delivery.py",
)


class SidecarFailure(RuntimeError):
    cms_source_delivery_client_failure = True

    def __init__(self, code="source_delivery_client.network", *, retryable=True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ScriptedSidecarClient:
    def __init__(self, sidecar="a" * 64, remote="b" * 64):
        self.expected_capabilities_sha256 = sidecar
        self.expected_remote_capabilities_sha256 = remote
        self.timeout = 30
        self.calls = []
        self.failures = []
        self.mutate = None

    @staticmethod
    def _canonical(value):
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _submit(self, value, source_max_attempts, delivery_max_attempts):
        copied = copy.deepcopy(value)
        self.calls.append((copied, source_max_attempts, delivery_max_attempts))
        if self.failures:
            raise self.failures.pop(0)
        schema = copied["schema"]
        if schema == ADAPTER.CHANGE_SCHEMA:
            operation = "change"
            request_id = copied["event_id"]
        elif schema == ADAPTER.CANCELLATION_SCHEMA:
            operation = "cancellation"
            request_id = copied["cancellation_id"]
        else:
            operation = "tombstone"
            request_id = copied["tombstone_id"]
        result = {
            "schema": ADAPTER.SIDECAR_QUEUE_RESPONSE_SCHEMA,
            "queue": {
                "operation": operation,
                "request_id": request_id,
                "event_id": copied["event_id"],
                "site_id": copied["site_id"],
                "payload_sha256": hashlib.sha256(self._canonical(copied)).hexdigest(),
                "remote_capabilities_sha256": self.expected_remote_capabilities_sha256,
                "status": "pending",
                "attempts": 0,
                "delivery_max_attempts": delivery_max_attempts,
                "source_max_attempts": source_max_attempts,
                "next_attempt_at": 100.0,
                "lease_expires_at": None,
                "lease_expired": False,
                "last_error_code": None,
                "response_sha256": None,
            },
            "capabilities_sha256": self.expected_capabilities_sha256,
        }
        if self.mutate is not None:
            self.mutate(result)
        return result

    def submit_change(self, value, *, source_max_attempts, delivery_max_attempts):
        return self._submit(value, source_max_attempts, delivery_max_attempts)

    def submit_removal(self, value, *, source_max_attempts, delivery_max_attempts):
        return self._submit(value, source_max_attempts, delivery_max_attempts)


class SidecarOutboxAdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.connection = sqlite3.connect(":memory:")
        self.client = ScriptedSidecarClient()
        self.adapter = ADAPTER.CMSSourceDeliverySidecarOutboxAdapter(
            self.client, sidecar_delivery_max_attempts=4,
        )
        self.outbox = DELIVERY.DurableCMSSourceDeliveryOutbox(
            self.connection, self.adapter, clock=lambda: self.now,
            base_delay_seconds=5, max_delay_seconds=20,
        )

    def tearDown(self):
        self.connection.close()

    def test_three_retry_budgets_remain_separate_through_durable_acceptance(self):
        change = cms_support.event()
        queued = self.outbox.enqueue_change(
            change, source_max_attempts=3, delivery_max_attempts=2,
        )
        outcome = self.outbox.run_once("website-worker", lease_seconds=60)

        self.assertEqual(queued.source_max_attempts, 3)
        self.assertEqual(queued.delivery_max_attempts, 2)
        self.assertEqual((outcome.status, outcome.attempt), ("succeeded", 1))
        self.assertEqual(self.client.calls, [(change, 3, 4)])
        self.assertEqual(self.outbox.health().status, "ok")

    def test_adapter_binding_changes_with_each_contract_and_inner_policy(self):
        digests = {
            ADAPTER.CMSSourceDeliverySidecarOutboxAdapter(
                ScriptedSidecarClient(sidecar="c" * 64),
                sidecar_delivery_max_attempts=4,
            ).expected_capabilities_sha256,
            ADAPTER.CMSSourceDeliverySidecarOutboxAdapter(
                ScriptedSidecarClient(remote="d" * 64),
                sidecar_delivery_max_attempts=4,
            ).expected_capabilities_sha256,
            ADAPTER.CMSSourceDeliverySidecarOutboxAdapter(
                ScriptedSidecarClient(), sidecar_delivery_max_attempts=5,
            ).expected_capabilities_sha256,
            self.adapter.expected_capabilities_sha256,
        }
        self.assertEqual(len(digests), 4)
        self.assertNotIn("a" * 64, repr(self.adapter))

    def test_retryable_sidecar_failure_uses_only_outer_acceptance_budget(self):
        change = cms_support.event()
        self.client.failures.append(SidecarFailure())
        self.outbox.enqueue_change(
            change, source_max_attempts=3, delivery_max_attempts=2,
        )
        first = self.outbox.run_once("website-worker", lease_seconds=60)
        self.now = 105
        second = self.outbox.run_once("website-worker", lease_seconds=60)

        self.assertEqual(first.status, "retry_wait")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(
            [(source, delivery) for _value, source, delivery in self.client.calls],
            [(3, 4), (3, 4)],
        )

    def test_changed_adapter_policy_blocks_existing_active_record(self):
        change = cms_support.event()
        self.outbox.enqueue_change(change)
        changed = ADAPTER.CMSSourceDeliverySidecarOutboxAdapter(
            self.client, sidecar_delivery_max_attempts=6,
        )
        replacement = DELIVERY.DurableCMSSourceDeliveryOutbox(
            self.connection, changed, clock=lambda: self.now,
        )
        with self.assertRaises(DELIVERY.CMSSourceDeliveryBlocked) as caught:
            replacement.run_once("website-worker", lease_seconds=60)

        self.assertEqual(caught.exception.code, "source_delivery.contract_changed")
        self.assertEqual(self.client.calls, [])

    def test_malformed_sidecar_acceptance_fails_closed_without_retry(self):
        change = cms_support.event()
        self.client.mutate = lambda response: response["queue"].update(
            remote_capabilities_sha256="f" * 64,
        )
        self.outbox.enqueue_change(change, delivery_max_attempts=3)
        outcome = self.outbox.run_once("website-worker", lease_seconds=60)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            outcome.error_code, "source_delivery_sidecar_adapter.response_invalid",
        )
        self.assertEqual(len(self.client.calls), 1)

    def test_incomplete_sidecar_queue_fails_closed_without_retry(self):
        change = cms_support.event()
        self.client.mutate = lambda response: response["queue"].pop(
            "next_attempt_at",
        )
        self.outbox.enqueue_change(change, delivery_max_attempts=3)

        outcome = self.outbox.run_once("website-worker", lease_seconds=60)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            outcome.error_code, "source_delivery_sidecar_adapter.response_invalid",
        )
        self.assertEqual(len(self.client.calls), 1)

    def test_change_and_removal_routes_reject_crossed_payloads(self):
        change = cms_support.event()
        cancellation = cms_support.cancellation(change)
        with self.assertRaises(
            ADAPTER.CMSSourceDeliverySidecarAdapterBlocked,
        ) as caught:
            self.adapter.submit_change(cancellation, max_attempts=3)
        self.assertEqual(
            caught.exception.code, "source_delivery_sidecar_adapter.request_invalid",
        )
        removal = self.adapter.submit_removal(cancellation, max_attempts=3)
        self.assertEqual(removal["operation"], "cancellation")

    def test_real_contract_pinned_sidecar_accepts_one_durable_outer_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sidecar.sqlite3"
            remote = sidecar_support.delivery_support.ScriptedClient()
            authenticator = sidecar_support.http_support.Authenticator()
            runtime = sidecar_support.RUNTIME.open_hosted_cms_source_delivery(
                database,
                remote,
                worker_id="sidecar-worker",
                clock=lambda: self.now,
                lease_seconds=60,
                active_delay_seconds=10,
                idle_delay_seconds=10,
                blocked_delay_seconds=10,
                http_authenticator=authenticator,
            )
            try:
                transport = sidecar_support.WSGITransport(runtime.http)
                client = sidecar_support.CLIENT.CMSSourceDeliverySidecarHTTPClient(
                    "https://delivery.example",
                    sidecar_support.HTTP._capabilities_payload()["sha256"],
                    remote.expected_capabilities_sha256,
                    lambda _context: {"Authorization": "Bearer test"},
                    transport=transport,
                )
                adapter = ADAPTER.CMSSourceDeliverySidecarOutboxAdapter(
                    client, sidecar_delivery_max_attempts=4,
                )
                outer_connection = sqlite3.connect(":memory:")
                try:
                    outer = DELIVERY.DurableCMSSourceDeliveryOutbox(
                        outer_connection, adapter, clock=lambda: self.now,
                    )
                    change = cms_support.event()
                    outer.enqueue_change(
                        change, source_max_attempts=3,
                        delivery_max_attempts=2,
                    )

                    outcome = outer.run_once(
                        "website-worker", lease_seconds=60,
                    )
                    accepted = runtime.status("change", change["event_id"])

                    self.assertEqual(outcome.status, "succeeded")
                    self.assertIn(accepted.status, {"pending", "succeeded"})
                    self.assertEqual(accepted.source_max_attempts, 3)
                    self.assertEqual(accepted.delivery_max_attempts, 4)
                finally:
                    outer_connection.close()
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
