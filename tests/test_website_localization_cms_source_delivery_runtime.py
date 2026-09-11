from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests import test_website_localization_cms_client as cms_support
from tests import test_website_localization_cms_source_delivery as delivery_support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = load(
    "blun_test_website_localization_cms_source_delivery_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_runtime.py",
)


class BlockingClient(delivery_support.ScriptedClient):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def submit_change(self, value, *, max_attempts):
        self.entered.set()
        self.release.wait(5)
        return super().submit_change(value, max_attempts=max_attempts)


class SourceDeliveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "delivery.sqlite3"
        self.client = delivery_support.ScriptedClient()
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
        self.directory.cleanup()

    def open(self, *, client=None, hosted=False, **kwargs):
        factory = (
            RUNTIME.open_hosted_cms_source_delivery
            if hosted
            else RUNTIME.open_durable_cms_source_delivery
        )
        runtime = factory(
            self.database,
            self.client if client is None else client,
            worker_id="website-source-worker",
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

    def test_invalid_configuration_creates_no_database(self):
        cases = (
            {"worker_id": "not valid", "lease_seconds": 60},
            {"worker_id": "worker", "lease_seconds": 30},
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
                    RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
                ) as caught:
                    RUNTIME.open_durable_cms_source_delivery(
                        self.database,
                        self.client,
                        clock=lambda: self.now,
                        **options,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "source_delivery_runtime.configuration_invalid",
                )
                self.assertFalse(self.database.exists())

        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            RUNTIME.open_hosted_cms_source_delivery(
                self.database,
                self.client,
                worker_id="worker",
                clock=lambda: self.now,
                lease_seconds=60,
                idle_delay_seconds=0,
            )
        self.assertEqual(
            caught.exception.code, "source_delivery_runtime.loop_invalid",
        )
        self.assertFalse(self.database.exists())

    def test_database_is_private_and_repr_is_content_free(self):
        runtime = self.open()

        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        rendered = repr(runtime)
        self.assertNotIn(str(self.database), rendered)
        self.assertNotIn("source_text", rendered)

    def test_manual_runtime_persists_and_resumes_after_restart(self):
        change = cms_support.event()
        first = self.open()
        first.enqueue_change(change)
        first.close()

        resumed = self.open()
        outcome = resumed.run_once()

        self.assertEqual((outcome.status, outcome.attempt), ("succeeded", 1))
        self.assertEqual(self.client.calls, [("change", change, 5)])
        self.assertEqual(
            resumed.status("change", change["event_id"]).status,
            "succeeded",
        )

    def test_hosted_worker_delivers_and_reports_readiness(self):
        change = cms_support.event()
        runtime = self.open(
            hosted=True,
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
            blocked_delay_seconds=0.01,
        )
        self.assertEqual(runtime.worker_readiness()["status"], "ready")

        runtime.enqueue_change(change, source_max_attempts=4)
        self.wait_for(
            lambda: runtime.status("change", change["event_id"]).status
            == "succeeded"
        )

        self.assertEqual(self.client.calls, [("change", change, 4)])
        self.assertEqual(runtime.worker_readiness()["status"], "ready")

    def test_stopped_host_rejects_new_intake(self):
        runtime = self.open(
            hosted=True,
            idle_delay_seconds=0.01,
        )
        runtime.stop_worker()

        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            runtime.enqueue_change(cms_support.event())

        self.assertEqual(
            caught.exception.code,
            "source_delivery_runtime.worker_not_ready",
        )
        readiness = runtime.worker_readiness()
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(self.client.calls, [])

    def test_permission_change_blocks_before_network_access(self):
        runtime = self.open()
        runtime.enqueue_change(cms_support.event())
        os.chmod(self.database, 0o644)

        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            runtime.run_once()

        self.assertEqual(
            caught.exception.code, "source_delivery_runtime.database_unsafe",
        )
        self.assertEqual(self.client.calls, [])

    def test_linked_database_is_rejected_without_mutating_target(self):
        target = Path(self.directory.name) / "foreign.sqlite3"
        target.write_bytes(b"foreign-state")
        os.chmod(target, 0o600)
        self.database.symlink_to(target)

        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            self.open()

        self.assertEqual(
            caught.exception.code, "source_delivery_runtime.database_unsafe",
        )
        self.assertEqual(target.read_bytes(), b"foreign-state")

    def test_database_replacement_blocks_before_network_access(self):
        runtime = self.open()
        runtime.enqueue_change(cms_support.event())
        original = Path(self.directory.name) / "original.sqlite3"
        self.database.rename(original)
        self.database.write_bytes(b"replacement")
        os.chmod(self.database, 0o600)

        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            runtime.run_once()

        self.assertEqual(
            caught.exception.code, "source_delivery_runtime.database_unsafe",
        )
        self.assertEqual(self.client.calls, [])

    def test_runtime_is_process_bound_before_lock_or_store_access(self):
        runtime = self.open()
        runtime._owner_pid += 1

        self.assertEqual(runtime.state, "foreign-process")
        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            runtime.health()

        self.assertEqual(
            caught.exception.code, "source_delivery_runtime.foreign_process",
        )
        self.assertEqual(self.client.calls, [])

    def test_concurrent_exact_enqueues_converge(self):
        runtime = self.open()
        change = cms_support.event()
        errors = []

        def enqueue():
            try:
                runtime.enqueue_change(change)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=enqueue) for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        count = runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_worker_private_failure_is_redacted_and_stops_intake(self):
        runtime = self.open()

        def fail(*_args, **_kwargs):
            raise RuntimeError("private website content and path")

        runtime._outbox.run_once = fail
        runtime.start_worker(idle_delay_seconds=0.01)
        self.wait_for(lambda: runtime.worker_state == "failed")

        readiness = runtime.worker_readiness()
        self.assertEqual(readiness, {
            "schema": "blun.cms-source-delivery-worker-readiness.v1",
            "status": "not_ready",
            "worker_state": "failed",
            "outbox_status": None,
            "error_code": "source_delivery_runtime.worker_blocked",
        })
        self.assertNotIn("private", json.dumps(readiness))
        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            runtime.enqueue_change(cms_support.event())
        self.assertEqual(
            caught.exception.code,
            "source_delivery_runtime.worker_not_ready",
        )

    def test_shutdown_timeout_keeps_database_open_until_worker_finishes(self):
        client = BlockingClient()
        runtime = self.open(
            client=client,
            hosted=True,
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
        )
        change = cms_support.event()
        runtime.enqueue_change(change)
        self.assertTrue(client.entered.wait(1))

        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            runtime.close(worker_timeout_seconds=0.01)

        self.assertEqual(
            caught.exception.code,
            "source_delivery_runtime.worker_stop_timeout",
        )
        self.assertFalse(runtime._closed)
        client.release.set()
        runtime.stop_worker(timeout_seconds=1)
        runtime.close()
        self.assertEqual(runtime.state, "closed")

    def test_two_runtimes_share_one_durable_claim(self):
        change = cms_support.event()
        first = self.open()
        second = self.open()
        first.enqueue_change(change)

        delivered = first.run_once()
        idle = second.run_once()

        self.assertEqual(delivered.status, "succeeded")
        self.assertIsNone(idle)
        self.assertEqual(len(self.client.calls), 1)

    def test_active_contract_change_is_visible_and_fail_closed(self):
        change = cms_support.event()
        first = self.open()
        first.enqueue_change(change)
        replacement = delivery_support.ScriptedClient("b" * 64)
        changed = self.open(client=replacement)

        readiness = changed.worker_readiness()
        self.assertEqual(readiness["worker_state"], "unmanaged")
        health = changed.health()
        self.assertEqual((health.status, health.contract_mismatches), (
            "blocked", 1,
        ))
        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            changed.run_once()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_runtime.outbox_blocked",
        )
        self.assertEqual(replacement.calls, [])


if __name__ == "__main__":
    unittest.main()
