from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
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


RUNTIME = load(
    "blun_test_website_localization_submission_dispatch_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_dispatch_runtime.py",
)


class BlockingClient:
    def __init__(self, client):
        self.client = client
        self.timeout = client.timeout
        self.expected_capabilities_sha256 = client.expected_capabilities_sha256
        self.entered = threading.Event()
        self.release = threading.Event()

    def submit_change(self, payload, **budgets):
        self.entered.set()
        self.release.wait(5)
        return self.client.submit_change(payload, **budgets)

    def submit_removal(self, payload, **budgets):
        return self.client.submit_removal(payload, **budgets)


class SubmissionDispatchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "public-submissions.sqlite3"
        self.support = client_support.SourceDeliverySubmissionClientTests(
            methodName="runTest"
        )
        self.support.setUp()
        self.runtimes = []

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            runtime._owner_pid = os.getpid()
            if self.database.exists() and not self.database.is_symlink():
                os.chmod(self.database, 0o600)
            try:
                runtime.close(worker_timeout_seconds=1)
            except Exception:
                pass
        self.support.tearDown()
        self.directory.cleanup()

    def open(self, *, client=None, hosted=False, **kwargs):
        factory = (
            RUNTIME.open_hosted_cms_source_delivery_submission_dispatch
            if hosted
            else RUNTIME.open_durable_cms_source_delivery_submission_dispatch
        )
        runtime = factory(
            self.database,
            self.support.client if client is None else client,
            worker_id="cms-public-submission-worker",
            clock=lambda: self.now,
            lease_seconds=60,
            **kwargs,
        )
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def wait_for(predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition was not reached")

    def test_invalid_configuration_creates_no_database_or_network_call(self):
        cases = (
            {"worker_id": "not valid", "lease_seconds": 60},
            {"worker_id": "worker", "lease_seconds": 10},
            {
                "worker_id": "worker",
                "lease_seconds": 60,
                "base_delay_seconds": 10,
                "max_delay_seconds": 5,
            },
        )
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaises(
                    RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
                ) as caught:
                    RUNTIME.open_durable_cms_source_delivery_submission_dispatch(
                        self.database,
                        self.support.client,
                        clock=lambda: self.now,
                        **options,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "source_delivery_submission_dispatch_runtime.configuration_invalid",
                )
                self.assertFalse(self.database.exists())
        self.assertEqual(self.support.transport.calls, [])

        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            RUNTIME.open_hosted_cms_source_delivery_submission_dispatch(
                self.database,
                self.support.client,
                worker_id="worker",
                clock=lambda: self.now,
                lease_seconds=60,
                idle_delay_seconds=0,
            )
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.loop_invalid",
        )
        self.assertFalse(self.database.exists())

    def test_private_database_manual_restart_and_content_free_repr(self):
        change = cms_support.event()
        first = self.open()
        first.enqueue(change, source_max_attempts=3, delivery_max_attempts=4)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        self.assertNotIn(str(self.database), repr(first))
        self.assertNotIn(change["localization"]["source_text"], repr(first))
        first.close()

        resumed = self.open()
        outcome = resumed.run_once()

        self.assertEqual((outcome.status, outcome.attempt), ("accepted", 1))
        status = resumed.status("change", change["event_id"])
        self.assertEqual(status.status, "accepted")
        self.assertEqual(status.source_max_attempts, 3)
        self.assertEqual(status.delivery_max_attempts, 4)

    def test_hosted_worker_delivers_and_reports_readiness(self):
        change = cms_support.event()
        runtime = self.open(
            hosted=True,
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
            blocked_delay_seconds=0.01,
        )
        self.assertEqual(runtime.worker_readiness()["status"], "ready")

        runtime.enqueue(change, client_max_attempts=2)
        self.wait_for(
            lambda: runtime.status("change", change["event_id"]).status
            == "accepted"
        )

        readiness = runtime.worker_readiness()
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["capabilities_sha256"], self.support.digest)

    def test_stopped_host_rejects_new_intake(self):
        runtime = self.open(hosted=True, idle_delay_seconds=0.01)
        runtime.stop_worker()

        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            runtime.enqueue(cms_support.event())

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.worker_not_ready",
        )
        readiness = runtime.worker_readiness()
        self.assertEqual(readiness["status"], "not_ready")
        self.assertNotIn("source_text", json.dumps(readiness))

    def test_permission_drift_blocks_before_network_access(self):
        runtime = self.open()
        runtime.enqueue(cms_support.event())
        calls = len(self.support.transport.calls)
        os.chmod(self.database, 0o644)

        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            runtime.run_once()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.database_unsafe",
        )
        self.assertEqual(len(self.support.transport.calls), calls)

    def test_symlink_is_rejected_without_mutating_target(self):
        target = Path(self.directory.name) / "foreign.sqlite3"
        target.write_bytes(b"foreign-state")
        os.chmod(target, 0o600)
        self.database.symlink_to(target)

        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            self.open()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.database_unsafe",
        )
        self.assertEqual(target.read_bytes(), b"foreign-state")
        self.assertEqual(self.support.transport.calls, [])

    def test_inode_replacement_blocks_before_network_access(self):
        runtime = self.open()
        runtime.enqueue(cms_support.event())
        original = Path(self.directory.name) / "original.sqlite3"
        self.database.rename(original)
        self.database.write_bytes(b"replacement")
        os.chmod(self.database, 0o600)

        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            runtime.run_once()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.database_unsafe",
        )
        self.assertEqual(self.support.transport.calls, [])

    def test_schema_tampering_blocks_restart_without_network_access(self):
        runtime = self.open()
        runtime.close()
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE injected(value TEXT)")
        connection.close()

        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            self.open()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.database_schema_altered",
        )
        self.assertEqual(self.support.transport.calls, [])

    def test_capability_drift_blocks_before_store_or_network_access(self):
        runtime = self.open()
        runtime.enqueue(cms_support.event())
        original = self.support.client.expected_capabilities_sha256
        self.support.client.expected_capabilities_sha256 = "b" * 64
        try:
            with self.assertRaises(
                RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
            ) as caught:
                runtime.run_once()
        finally:
            self.support.client.expected_capabilities_sha256 = original

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.capability_drift",
        )
        self.assertEqual(self.support.transport.calls, [])

    def test_runtime_is_process_bound_before_lock_or_store_access(self):
        runtime = self.open()
        runtime._owner_pid += 1

        self.assertEqual(runtime.state, "foreign-process")
        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            runtime.health()

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.foreign_process",
        )
        self.assertEqual(self.support.transport.calls, [])

    def test_concurrent_exact_enqueues_converge(self):
        runtime = self.open()
        change = cms_support.event()
        errors = []

        def enqueue():
            try:
                runtime.enqueue(change)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=enqueue) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        count = runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_public_submission_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_shutdown_timeout_keeps_database_open_until_worker_finishes(self):
        client = BlockingClient(self.support.client)
        runtime = self.open(
            client=client,
            hosted=True,
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
        )
        runtime.enqueue(cms_support.event())
        self.assertTrue(client.entered.wait(1))

        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            runtime.close(worker_timeout_seconds=0.01)

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.worker_stop_timeout",
        )
        self.assertFalse(runtime._closed)
        client.release.set()
        runtime.stop_worker(timeout_seconds=1)
        runtime.close()
        self.assertEqual(runtime.state, "closed")


if __name__ == "__main__":
    unittest.main()
