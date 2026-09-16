from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = load(
    "blun_test_website_localization_benchmark_watcher_recovery_runtime",
    ROOT / "integrations"
    / "website_localization_benchmark_watcher_recovery_runtime.py",
)
RUNNER = RUNTIME._RUNNER


@dataclass(frozen=True)
class OpenAPI:
    contract_sha256: str = "a" * 64
    openapi_sha256: str = "b" * 64


@dataclass(frozen=True)
class Status:
    checked_at: float = 1500.0
    state: str = "failed"
    rearmable: bool = True
    generation: dict | None = None

    def __post_init__(self):
        if self.generation is None:
            object.__setattr__(self, "generation", {
                "attempts": 20,
                "failed_at": 1000.0,
                "error_code": "benchmark_client.network",
            })


@dataclass(frozen=True)
class Rearm:
    request_sha256: str
    receipt: dict

    def as_payload(self):
        return dict(self.receipt)


class Client:
    origin = "https://control.example"

    def __init__(self):
        self.calls = []

    def openapi(self):
        self.calls.append("openapi")
        return OpenAPI()

    def status(self):
        self.calls.append("status")
        return Status()

    def rearm(self, **kwargs):
        self.calls.append("rearm")
        request = RUNNER._CLIENT._request(
            kwargs["request_id"], kwargs["expected_attempts"],
            kwargs["expected_failed_at"], kwargs["expected_error_code"],
        )
        request_sha256 = hashlib.sha256(
            RUNNER._CLIENT._canonical(request)
        ).hexdigest()
        return Rearm(request_sha256, {
            "schema": RUNNER._CONTROL.RECEIPT_SCHEMA,
            "request_sha256": request_sha256,
            "previous_state": "failed",
            "previous_attempts": kwargs["expected_attempts"],
            "previous_error_code": kwargs["expected_error_code"],
            "failed_at": kwargs["expected_failed_at"],
            "state": "pending",
            "rearmed_at": 1500.0,
        })


class BlockingClient(Client):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def openapi(self):
        self.calls.append("openapi")
        self.entered.set()
        self.release.wait(5)
        return OpenAPI()


class RecoveryRuntimeTests(unittest.TestCase):
    def open(self, path, client=None, **kwargs):
        return RUNTIME.open_durable_benchmark_watcher_recovery(
            path, client or Client(), worker_id="operator-1",
            clock=lambda: 100.0, lease_seconds=1,
            base_delay_seconds=0.1, max_delay_seconds=1,
            maximum_wait_seconds=0.1, max_attempts=8, **kwargs,
        )

    def test_invalid_configuration_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.sqlite3"
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "configuration_invalid",
            ):
                self.open(path, object())
            self.assertFalse(path.exists())

            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "configuration_invalid",
            ):
                RUNTIME.open_durable_benchmark_watcher_recovery(
                    path, Client(), worker_id="bad worker",
                )
            self.assertFalse(path.exists())

    def test_private_file_and_hosted_worker_complete_exact_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.sqlite3"
            client = Client()
            runtime = RUNTIME.open_hosted_benchmark_watcher_recovery(
                path, client, operation_id="operator-remediation-1",
                worker_id="operator-1", clock=lambda: 100.0,
                lease_seconds=1, base_delay_seconds=0.1,
                max_delay_seconds=1, maximum_wait_seconds=0.1,
                max_attempts=8,
            )
            deadline = time.monotonic() + 3
            while runtime.worker_state == "running" and time.monotonic() < deadline:
                time.sleep(0.02)
            snapshot = runtime.status()
            readiness = runtime.readiness()
            self.assertEqual(snapshot.state, "succeeded")
            self.assertEqual(client.calls, ["openapi", "status", "rearm"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(readiness["recovery_state"], "succeeded")
            self.assertNotIn("operator-remediation-1", repr(readiness))
            runtime.close()

    def test_restart_preserves_generation_and_operation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.sqlite3"
            first_client = Client()
            first = self.open(path, first_client)
            first.start("operator-remediation-1")
            first.run_once()
            first.close()

            second_client = Client()
            second = self.open(path, second_client)
            repeated = second.start("operator-remediation-1")
            second.run_once()
            completed = second.run_once()
            self.assertEqual(repeated.phase, "status")
            self.assertEqual(completed.state, "succeeded")
            self.assertEqual(second_client.calls, ["status", "rearm"])
            second.close()

    def test_existing_foreign_or_differently_bound_store_is_not_mutated(self):
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory) / "foreign.sqlite3"
            connection = sqlite3.connect(foreign)
            connection.execute("CREATE TABLE foreign_state (value TEXT)")
            connection.execute("INSERT INTO foreign_state VALUES ('kept')")
            connection.commit()
            connection.close()
            foreign.chmod(0o600)
            before = foreign.read_bytes()
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "database_schema_altered",
            ):
                self.open(foreign)
            self.assertEqual(foreign.read_bytes(), before)

            bound = Path(directory) / "bound.sqlite3"
            first = self.open(bound)
            first.start("operator-remediation-1")
            first.close()
            before = bound.read_bytes()
            changed = Client()
            changed.origin = "https://other-control.example"
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "database_generation_invalid",
            ):
                self.open(bound, changed)
            self.assertEqual(bound.read_bytes(), before)

    def test_worker_requires_explicit_operator_start(self):
        runtime = self.open(":memory:")
        with self.assertRaisesRegex(
            RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
            "runner_blocked",
        ):
            runtime.start_worker()
        self.assertEqual(runtime.worker_state, "unmanaged")
        runtime.close()

    def test_permission_drift_blocks_before_durable_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.sqlite3"
            runtime = self.open(path)
            runtime.start("operator-remediation-1")
            path.chmod(0o644)
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "database_unsafe",
            ):
                runtime.status()
            path.chmod(0o600)
            runtime.close()

    def test_linked_database_is_rejected_without_mutating_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.sqlite3"
            target.write_bytes(b"protected")
            target.chmod(0o600)
            symlink = Path(directory) / "linked.sqlite3"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "database_unsafe",
            ):
                self.open(symlink)
            self.assertEqual(target.read_bytes(), b"protected")

            symlink.unlink()
            os.link(target, symlink)
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "database_unsafe",
            ):
                self.open(symlink)
            self.assertEqual(target.read_bytes(), b"protected")

    def test_inode_replacement_blocks_before_durable_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.sqlite3"
            runtime = self.open(path)
            runtime.start("operator-remediation-1")
            replacement = Path(directory) / "replacement.sqlite3"
            replacement.touch(mode=0o600)
            os.replace(replacement, path)
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "database_unsafe",
            ):
                runtime.status()
            # The open original connection can still be closed safely.
            runtime.close()

    def test_foreign_process_blocks_before_lock_or_database(self):
        runtime = self.open(":memory:")
        with mock.patch.object(RUNTIME.os, "getpid", return_value=os.getpid() + 1):
            self.assertEqual(runtime.state, "foreign-process")
            with self.assertRaisesRegex(
                RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
                "foreign_process",
            ):
                runtime.start("operator-remediation-1")
        runtime.close()

    def test_parallel_status_reads_are_serialized_and_identical(self):
        runtime = self.open(":memory:")
        runtime.start("operator-remediation-1")
        results = []
        failures = []

        def read():
            try:
                results.append(runtime.status())
            except Exception as error:  # pragma: no cover - assertion below
                failures.append(error)

        threads = [threading.Thread(target=read) for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(failures)
        self.assertEqual(len(results), 24)
        self.assertEqual({item.state for item in results}, {"pending"})
        runtime.close()

    def test_shutdown_timeout_keeps_database_open_until_request_returns(self):
        client = BlockingClient()
        runtime = self.open(":memory:", client)
        runtime.start("operator-remediation-1")
        runtime.start_worker()
        self.assertTrue(client.entered.wait(1))
        with self.assertRaisesRegex(
            RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
            "worker_stop_timeout",
        ):
            runtime.close(worker_timeout_seconds=0.1)
        self.assertEqual(runtime.state, "open")
        client.release.set()
        runtime.stop_worker(timeout_seconds=2)
        self.assertEqual(runtime.status().phase, "status")
        runtime.close()

    def test_close_is_idempotent_and_later_operations_block(self):
        runtime = self.open(":memory:")
        runtime.close()
        runtime.close()
        self.assertEqual(runtime.state, "closed")
        with self.assertRaisesRegex(
            RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked,
            "closed",
        ):
            runtime.start("operator-remediation-1")


if __name__ == "__main__":
    unittest.main()
