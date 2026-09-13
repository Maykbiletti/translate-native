from __future__ import annotations

import copy
import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests import test_website_localization_cms_client as cms_support


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = load(
    "blun_test_website_localization_cms_source_runtime",
    ROOT / "integrations" / "website_localization_cms_source_runtime.py",
)
SERVICE = RUNTIME._SERVICE


class ScriptedClient:
    timeout = 30.0

    def __init__(self):
        self.calls = []
        self.events = {}
        self.lifecycle_status = "processing"

    def submit_change(self, change):
        self.calls.append(("change", copy.deepcopy(change)))
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
        terminal = self.lifecycle_status == "published"
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.LIFECYCLE_RESPONSE_SCHEMA,
            "request_id": "lifecycle-" + event_id,
            "event_id": event_id,
            "site_id": site_id,
            "plan_id": "plan-" + event_id,
            "website_version": change["website_version"],
            "source_sequence": change["source_sequence"],
            "status": self.lifecycle_status,
            "required_locales": required,
            "approved_locales": required if terminal else [],
            "blocked_locales": [],
            "queue_counts": {
                "failed": 0,
                "leased": 0,
                "pending": 0 if terminal else len(required),
                "retry_wait": 0,
                "succeeded": len(required) if terminal else 0,
            },
            "delivery": None,
            "tombstone": None,
        }


class DurableCMSSourceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.client = ScriptedClient()

    @staticmethod
    def paths(directory: str) -> tuple[Path, Path, Path]:
        root = Path(directory)
        return (
            root / "changes.sqlite3",
            root / "removals.sqlite3",
            root / "lifecycle.sqlite3",
        )

    def open(self, paths, **overrides):
        values = {
            "change_database": paths[0],
            "removal_database": paths[1],
            "lifecycle_database": paths[2],
            "client": self.client,
            "change_worker_id": "source-change-worker",
            "removal_worker_id": "source-removal-worker",
            "lifecycle_worker_id": "source-lifecycle-worker",
            "clock": lambda: self.now,
            "change_lease_seconds": 60,
            "removal_lease_seconds": 60,
            "lifecycle_lease_seconds": 60,
            "lifecycle_poll_interval_seconds": 30,
        }
        values.update(overrides)
        return RUNTIME.open_durable_cms_source(**values)

    def pinned_client(self, **overrides):
        queue_connection = sqlite3.connect(":memory:")
        release_connection = sqlite3.connect(":memory:")
        cms_connection = sqlite3.connect(":memory:")
        self.addCleanup(queue_connection.close)
        self.addCleanup(release_connection.close)
        self.addCleanup(cms_connection.close)
        queue = cms_support.CMS._QUEUE.LocalizationQueue(queue_connection)
        release = cms_support.CMS._RELEASE.LocalizationReleaseStore(
            release_connection, queue,
        )
        bridge = cms_support.CMS.WebsiteLocalizationCMSBridge(
            cms_connection, queue, release,
        )
        authority = cms_support.Authority()
        api = cms_support.API.WebsiteLocalizationAPI(
            bridge,
            authority,
            clock=lambda: self.now,
            approval_authority=authority,
            publication_authority=authority,
        )
        transport = cms_support.WSGITransport(api)
        current = bridge.localization_capabilities()
        options = {
            "capabilities_sha256": current["sha256"],
            "commercial_rendering_registry_sha256": (
                current["commercial_rendering_registry"]["sha256"]
            ),
        }
        options.update(overrides)
        client = cms_support.CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test",
            lambda: {"Authorization": "Bearer host-token"},
            authority,
            transport=transport,
            clock=lambda: self.now,
            **options,
        )
        return client, transport, current

    def test_capability_preflight_verifies_both_pins_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            client, transport, current = self.pinned_client()

            runtime = self.open(
                paths, client=client, capability_preflight=True,
            )
            try:
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(runtime.capability_binding(), {
                    "schema": "blun.cms-source-capability-binding.v2",
                    "status": "verified",
                    "capabilities_sha256": current["sha256"],
                    "commercial_rendering_registry_sha256": current[
                        "commercial_rendering_registry"
                    ]["sha256"],
                    "database_roles": ["changes", "removals", "lifecycle"],
                })
                self.assertTrue(all(path.exists() for path in paths))
            finally:
                runtime.close()

    def test_capability_preflight_failure_creates_no_database(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as missing:
                self.open(paths, capability_preflight=True)
            self.assertEqual(
                missing.exception.code,
                "source_runtime.capability_pins_required",
            )
            self.assertFalse(any(path.exists() for path in paths))
            self.assertEqual(self.client.calls, [])

        class BrokenPins(ScriptedClient):
            @property
            def capabilities_sha256(self):
                raise RuntimeError("private secret-manager detail")

            commercial_rendering_registry_sha256 = "0" * 64

            def capabilities(self):
                raise AssertionError("capability transport must not run")

        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as unreadable:
                self.open(
                    paths,
                    client=BrokenPins(),
                    capability_preflight=True,
                )
            self.assertEqual(
                unreadable.exception.code,
                "source_runtime.capability_preflight_failed",
            )
            self.assertNotIn("secret", str(unreadable.exception))
            self.assertFalse(any(path.exists() for path in paths))

        for option in (
            {"capabilities_sha256": "0" * 64},
            {"commercial_rendering_registry_sha256": "0" * 64},
        ):
            with self.subTest(wrong_pin=tuple(option)):
                with tempfile.TemporaryDirectory() as directory:
                    paths = self.paths(directory)
                    client, transport, _ = self.pinned_client(**option)
                    with self.assertRaises(
                        RUNTIME.DurableCMSSourceRuntimeBlocked,
                    ) as mismatch:
                        self.open(
                            paths, client=client, capability_preflight=True,
                        )
                    self.assertEqual(
                        mismatch.exception.code,
                        "source_runtime.capability_pin_mismatch",
                    )
                    self.assertEqual(len(transport.calls), 1)
                    self.assertFalse(any(path.exists() for path in paths))

        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as invalid:
                self.open(paths, capability_preflight="yes")
            self.assertEqual(
                invalid.exception.code,
                "source_runtime.capability_preflight_invalid",
            )
            self.assertFalse(any(path.exists() for path in paths))

    def test_authenticated_runtime_requires_preflight_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)

            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as caught:
                self.open(paths, http_authenticator=lambda _request: {})

            self.assertEqual(
                caught.exception.code,
                "source_runtime.capability_preflight_required",
            )
            self.assertFalse(any(path.exists() for path in paths))
            self.assertEqual(self.client.calls, [])

    def test_verified_client_binding_change_blocks_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            client, transport, _ = self.pinned_client()
            runtime = self.open(
                paths, client=client, capability_preflight=True,
            )
            client._capabilities_sha256 = "0" * 64
            try:
                with self.assertRaises(
                    RUNTIME.DurableCMSSourceRuntimeBlocked,
                ) as changed:
                    runtime.enqueue_change(cms_support.event())
                self.assertEqual(
                    changed.exception.code,
                    "source_runtime.capability_binding_changed",
                )
                self.assertEqual(len(transport.calls), 1)
                count = runtime._connections[0].execute(
                    "SELECT COUNT(*) FROM cms_source_change_outbox"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                runtime.close()

    def test_pinned_restart_preserves_exact_database_roles_and_pending_work(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            first_client, _, current = self.pinned_client()
            first = self.open(
                paths, client=first_client, capability_preflight=True,
            )
            change = cms_support.event()
            first.enqueue_change(change)
            first.close()

            # Simulate a crash after two database bindings were committed.
            partial = sqlite3.connect(paths[2])
            try:
                partial.execute(
                    "DROP TABLE cms_source_runtime_capability_binding"
                )
                partial.commit()
            finally:
                partial.close()

            second_client, transport, _ = self.pinned_client()
            second = self.open(
                paths, client=second_client, capability_preflight=True,
            )
            try:
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(
                    second.status(change["event_id"], change["site_id"])
                    .dispatch_status,
                    "pending",
                )
                for role, connection in zip(
                    RUNTIME.CAPABILITY_DATABASE_ROLES,
                    second._connections,
                ):
                    row = connection.execute(
                        "SELECT database_role, capabilities_sha256, "
                        "commercial_rendering_registry_sha256 "
                        "FROM cms_source_runtime_capability_binding"
                    ).fetchone()
                    self.assertEqual(tuple(row), (
                        role,
                        current["sha256"],
                        current["commercial_rendering_registry"]["sha256"],
                    ))
            finally:
                second.close()

    def test_pinned_restart_rejects_other_binding_and_swapped_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            client, _, current = self.pinned_client()
            runtime = self.open(
                paths, client=client, capability_preflight=True,
            )
            runtime.close()

            changed_binding = (
                "0" * 64,
                current["commercial_rendering_registry"]["sha256"],
            )
            for role, path in zip(RUNTIME.CAPABILITY_DATABASE_ROLES, paths):
                connection = sqlite3.connect(path)
                try:
                    row = RUNTIME._capability_binding_row(
                        role, changed_binding,
                    )
                    connection.execute(
                        "UPDATE cms_source_runtime_capability_binding SET "
                        "capabilities_sha256 = ?, binding_sha256 = ?",
                        (row[3], row[5]),
                    )
                    connection.commit()
                finally:
                    connection.close()

            current_client, transport, _ = self.pinned_client()
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as mismatch:
                self.open(
                    paths,
                    client=current_client,
                    capability_preflight=True,
                )
            self.assertEqual(
                mismatch.exception.code,
                "source_runtime.capability_database_mismatch",
            )
            self.assertEqual(len(transport.calls), 1)

        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            client, _, _ = self.pinned_client()
            runtime = self.open(
                paths, client=client, capability_preflight=True,
            )
            runtime.close()
            before = []
            for path in paths[:2]:
                connection = sqlite3.connect(path)
                try:
                    before.append(tuple(
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table' ORDER BY name"
                        )
                    ))
                finally:
                    connection.close()

            swapped_client, transport, _ = self.pinned_client()
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as swapped:
                self.open(
                    (paths[1], paths[0], paths[2]),
                    client=swapped_client,
                    capability_preflight=True,
                )
            self.assertEqual(
                swapped.exception.code,
                "source_runtime.capability_database_mismatch",
            )
            self.assertEqual(len(transport.calls), 1)
            after = []
            for path in paths[:2]:
                connection = sqlite3.connect(path)
                try:
                    after.append(tuple(
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table' ORDER BY name"
                        )
                    ))
                finally:
                    connection.close()
            self.assertEqual(after, before)

    def test_pinned_startup_adopts_only_empty_unbound_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            legacy = self.open(paths)
            legacy.close()

            client, _, _ = self.pinned_client()
            adopted = self.open(
                paths, client=client, capability_preflight=True,
            )
            try:
                self.assertEqual(
                    adopted.capability_binding()["status"], "verified",
                )
            finally:
                adopted.close()

        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            change = cms_support.event()
            legacy = self.open(paths)
            legacy.enqueue_change(change)
            legacy.close()

            client, transport, _ = self.pinned_client()
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as unbound:
                self.open(
                    paths, client=client, capability_preflight=True,
                )
            self.assertEqual(
                unbound.exception.code,
                "source_runtime.capability_database_unbound",
            )
            self.assertEqual(len(transport.calls), 1)
            connection = sqlite3.connect(paths[0])
            try:
                row = connection.execute(
                    "SELECT status FROM cms_source_change_outbox "
                    "WHERE event_id = ?",
                    (change["event_id"],),
                ).fetchone()
                self.assertEqual(row, ("pending",))
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = ?",
                    (RUNTIME.CAPABILITY_BINDING_TABLE,),
                ).fetchone())
            finally:
                connection.close()

    def test_runtime_blocks_tampered_durable_binding_before_queue_access(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            client, transport, _ = self.pinned_client()
            runtime = self.open(
                paths, client=client, capability_preflight=True,
            )
            runtime._connections[0].execute(
                "UPDATE cms_source_runtime_capability_binding "
                "SET binding_sha256 = ?",
                ("0" * 64,),
            )
            try:
                with self.assertRaises(
                    RUNTIME.DurableCMSSourceRuntimeBlocked,
                ) as altered:
                    runtime.enqueue_change(cms_support.event())
                self.assertEqual(
                    altered.exception.code,
                    "source_runtime.capability_database_mismatch",
                )
                self.assertEqual(len(transport.calls), 1)
                count = runtime._connections[0].execute(
                    "SELECT COUNT(*) FROM cms_source_change_outbox"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                runtime.close()

    def test_runtime_persists_complete_lifecycle_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            change = cms_support.event()
            first = self.open(paths)
            first.enqueue_change(change)
            dispatched = first.run_once()
            first.close()

            second = self.open(paths)
            try:
                polled = second.run_once()
                self.assertEqual((dispatched.phase, dispatched.status), (
                    "change", "succeeded",
                ))
                self.assertEqual((polled.phase, polled.status), (
                    "lifecycle", "watching",
                ))
                self.assertEqual(
                    [call[0] for call in self.client.calls],
                    ["change", "lifecycle"],
                )
                self.assertEqual(second.health().status, "ok")
            finally:
                second.close()

    def test_runtime_persists_and_delivers_optional_terminal_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            notifications = []

            def callback(payload):
                notifications.append(copy.deepcopy(payload))
                return {
                    "schema": SERVICE._NOTIFICATION.ACK_SCHEMA,
                    "notification_id": payload["notification_id"],
                    "event_id": payload["event_id"],
                    "site_id": payload["site_id"],
                    "status": "accepted",
                    "notification_sha256": SERVICE._NOTIFICATION._hash(
                        SERVICE._NOTIFICATION._canonical(payload)
                    ),
                }

            runtime = self.open(
                self.paths(directory),
                terminal_notifier=callback,
                notification_worker_id="source-notification-worker",
                notification_lease_seconds=60,
            )
            change = cms_support.event()
            try:
                runtime.enqueue_change(change)
                runtime.run_once()
                self.client.lifecycle_status = "published"
                runtime.run_once()
                runtime.run_once()
                delivered = runtime.run_once()
                self.assertEqual((delivered.phase, delivered.status), (
                    "notification", "succeeded",
                ))
                self.assertEqual(len(notifications), 1)
                self.assertEqual(runtime.health().status, "ok")
                self.assertNotIn(
                    change["localization"]["source_text"],
                    repr(notifications),
                )
            finally:
                runtime.close()

    def test_files_are_created_owner_only_and_repr_is_content_free(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            runtime = self.open(paths)
            try:
                for path in paths:
                    state = path.stat()
                    self.assertEqual(state.st_uid, os.geteuid())
                    self.assertEqual(state.st_nlink, 1)
                    self.assertEqual(state.st_mode & 0o777, 0o600)
                rendered = repr(runtime)
                self.assertEqual(rendered, "DurableCMSSourceRuntime(state='open')")
                self.assertNotIn(directory, rendered)
                self.assertEqual(runtime.capability_binding(), {
                    "schema": "blun.cms-source-capability-binding.v2",
                    "status": "not_configured",
                    "capabilities_sha256": None,
                    "commercial_rendering_registry_sha256": None,
                    "database_roles": [],
                })
            finally:
                runtime.close()

    def test_invalid_configuration_creates_no_database(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as invalid:
                self.open(paths, change_worker_id="not a valid worker")
            self.assertEqual(
                invalid.exception.code,
                "source_runtime.configuration_invalid",
            )
            self.assertFalse(any(path.exists() for path in paths))

        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as invalid:
                self.open(paths, terminal_notifier=lambda _payload: None)
            self.assertEqual(
                invalid.exception.code,
                "source_runtime.configuration_invalid",
            )
            self.assertFalse(any(path.exists() for path in paths))

    def test_reused_path_blocks_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            reused = (paths[0], paths[0], paths[2])
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as invalid:
                self.open(reused)
            self.assertEqual(
                invalid.exception.code,
                "source_runtime.database_path_reused",
            )
            self.assertFalse(any(path.exists() for path in paths))

    def test_unsafe_filesystem_boundaries_block_before_sqlite_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir(mode=0o700)
            outside = private / "outside.sqlite3"
            outside.touch(mode=0o600)

            permissive = private / "permissive.sqlite3"
            permissive.touch(mode=0o600)
            permissive.chmod(0o640)
            linked = private / "linked.sqlite3"
            linked.symlink_to(outside)
            hard_linked = private / "hard-linked.sqlite3"
            os.link(outside, hard_linked)
            shared = root / "shared"
            shared.mkdir(mode=0o770)
            shared.chmod(0o770)
            alias = root / "alias"
            alias.symlink_to(private, target_is_directory=True)

            cases = (
                Path("relative.sqlite3"),
                permissive,
                linked,
                hard_linked,
                shared / "state.sqlite3",
                alias / "state.sqlite3",
            )
            for index, candidate in enumerate(cases):
                other_a = private / f"other-{index}-a.sqlite3"
                other_b = private / f"other-{index}-b.sqlite3"
                with self.subTest(candidate=candidate):
                    with self.assertRaises(
                        RUNTIME.DurableCMSSourceRuntimeBlocked,
                    ):
                        self.open((candidate, other_a, other_b))
                    self.assertFalse(other_a.exists())
                    self.assertFalse(other_b.exists())

    def test_permission_change_blocks_before_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            runtime = self.open(paths)
            change = cms_support.event()
            runtime.enqueue_change(change)
            paths[0].chmod(0o640)
            try:
                with self.assertRaises(
                    RUNTIME.DurableCMSSourceRuntimeBlocked,
                ) as blocked:
                    runtime.run_once()
                self.assertEqual(
                    blocked.exception.code,
                    "source_runtime.database_unsafe",
                )
                self.assertEqual(self.client.calls, [])
            finally:
                runtime.close()

    def test_path_replacement_blocks_before_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            runtime = self.open(paths)
            runtime.enqueue_change(cms_support.event())
            displaced = Path(directory) / "displaced.sqlite3"
            paths[0].rename(displaced)
            paths[0].symlink_to(paths[1])
            try:
                with self.assertRaises(
                    RUNTIME.DurableCMSSourceRuntimeBlocked,
                ) as blocked:
                    runtime.run_once()
                self.assertEqual(
                    blocked.exception.code,
                    "source_runtime.database_unsafe",
                )
                self.assertEqual(self.client.calls, [])
            finally:
                runtime.close()

    def test_private_service_exception_is_reduced_to_stable_code(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.open(self.paths(directory))
            secret = cms_support.event()["localization"]["source_text"]
            runtime._service.run_once = mock.Mock(
                side_effect=RuntimeError(secret),
            )
            try:
                with self.assertRaises(
                    RUNTIME.DurableCMSSourceRuntimeBlocked,
                ) as blocked:
                    runtime.run_once()
                self.assertEqual(
                    blocked.exception.code,
                    "source_runtime.service_blocked",
                )
                self.assertNotIn(secret, str(blocked.exception))
            finally:
                runtime.close()

    def test_runtime_is_process_bound_before_lock_and_store_access(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.open(self.paths(directory))
            owner = runtime._owner_pid
            try:
                with mock.patch.object(RUNTIME.os, "getpid", return_value=owner + 1):
                    self.assertEqual(runtime.state, "foreign-process")
                    with self.assertRaises(
                        RUNTIME.DurableCMSSourceRuntimeBlocked,
                    ) as blocked:
                        runtime.run_once()
                    self.assertEqual(
                        blocked.exception.code,
                        "source_runtime.foreign_process",
                    )
            finally:
                runtime.close()

    def test_threaded_ticks_are_serialized_without_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.open(self.paths(directory))
            change = cms_support.event()
            runtime.enqueue_change(change)
            try:
                with ThreadPoolExecutor(max_workers=12) as pool:
                    outcomes = list(pool.map(
                        lambda _index: runtime.run_once(), range(24),
                    ))
                self.assertEqual(
                    [call[0] for call in self.client.calls].count("change"),
                    1,
                )
                self.assertEqual(
                    sum(outcome.phase == "change" for outcome in outcomes),
                    1,
                )
                self.assertEqual(runtime.health().status, "ok")
            finally:
                runtime.close()

    def test_two_process_style_runtimes_share_durable_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            first = self.open(paths)
            second = self.open(
                paths,
                change_worker_id="source-change-worker-2",
                removal_worker_id="source-removal-worker-2",
                lifecycle_worker_id="source-lifecycle-worker-2",
            )
            first.enqueue_change(cms_support.event())
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(lambda runtime: runtime.run_once(), (first, second)))
                self.assertEqual(
                    [call[0] for call in self.client.calls].count("change"),
                    1,
                )
            finally:
                first.close()
                second.close()

    def test_close_is_idempotent_and_prevents_further_work(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.open(self.paths(directory))
            runtime.close()
            runtime.close()
            self.assertEqual(runtime.state, "closed")
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as blocked:
                runtime.health()
            self.assertEqual(blocked.exception.code, "source_runtime.closed")

    @staticmethod
    def wait_for(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("condition was not reached")

    def test_managed_worker_dispatches_and_stops_interruptibly(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.open(self.paths(directory))
            runtime.enqueue_change(cms_support.event())
            runtime.start_worker(
                active_delay_seconds=0.01,
                idle_delay_seconds=60,
                blocked_delay_seconds=60,
            )
            try:
                self.wait_for(lambda: bool(self.client.calls))
                readiness = runtime.worker_readiness()
                self.assertEqual(readiness, {
                    "schema": "blun.cms-source-worker-readiness.v1",
                    "status": "ready",
                    "worker_state": "running",
                    "service_status": "ok",
                    "error_code": None,
                })
                started = time.monotonic()
                runtime.stop_worker(timeout_seconds=1)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(runtime.worker_state, "stopped")
                self.assertEqual(
                    runtime.worker_readiness()["status"], "not_ready",
                )
                calls = len(self.client.calls)
                time.sleep(0.03)
                self.assertEqual(len(self.client.calls), calls)
            finally:
                runtime.close()

    def test_managed_worker_failure_is_visible_and_content_free(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.open(self.paths(directory))
            secret = cms_support.event()["localization"]["source_text"]
            runtime._service.run_once = mock.Mock(
                side_effect=RuntimeError(secret),
            )
            runtime.start_worker(
                active_delay_seconds=0.01,
                idle_delay_seconds=0.01,
                blocked_delay_seconds=0.01,
            )
            try:
                self.wait_for(lambda: runtime.worker_state == "failed")
                readiness = runtime.worker_readiness()
                self.assertEqual(readiness["status"], "not_ready")
                self.assertEqual(readiness["worker_state"], "failed")
                self.assertEqual(
                    readiness["error_code"], "source_runtime.worker_blocked",
                )
                self.assertNotIn(secret, repr(readiness))
                with self.assertRaises(
                    RUNTIME.DurableCMSSourceRuntimeBlocked,
                ) as blocked:
                    runtime.start_worker()
                self.assertEqual(
                    blocked.exception.code, "source_runtime.worker_blocked",
                )
            finally:
                runtime.close()

    def test_hosted_factory_starts_before_return_and_close_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            runtime = RUNTIME.open_hosted_cms_source(
                *paths,
                self.client,
                change_worker_id="source-change-worker",
                removal_worker_id="source-removal-worker",
                lifecycle_worker_id="source-lifecycle-worker",
                clock=lambda: self.now,
                change_lease_seconds=60,
                removal_lease_seconds=60,
                lifecycle_lease_seconds=60,
                lifecycle_poll_interval_seconds=30,
                active_delay_seconds=0.01,
                idle_delay_seconds=60,
                blocked_delay_seconds=60,
            )
            self.wait_for(lambda: runtime.worker_state == "running")
            runtime.close(worker_timeout_seconds=1)
            self.assertEqual(runtime.state, "closed")
            self.assertEqual(runtime.worker_state, "closed")

    def test_invalid_worker_configuration_does_not_launch_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.open(self.paths(directory))
            try:
                with self.assertRaises(
                    RUNTIME.DurableCMSSourceRuntimeBlocked,
                ) as blocked:
                    runtime.start_worker(idle_delay_seconds=0)
                self.assertEqual(
                    blocked.exception.code, "source_runtime.loop_invalid",
                )
                self.assertEqual(runtime.worker_state, "unmanaged")
                self.assertIsNone(runtime._worker_thread)
            finally:
                runtime.close()

    def test_worker_stop_timeout_is_bounded_during_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event()
            release = threading.Event()

            def blocked_submit(change):
                entered.set()
                release.wait(2)
                return ScriptedClient.submit_change(self.client, change)

            self.client.submit_change = blocked_submit
            runtime = self.open(self.paths(directory))
            runtime.enqueue_change(cms_support.event())
            runtime.start_worker(
                active_delay_seconds=0.01,
                idle_delay_seconds=60,
                blocked_delay_seconds=60,
            )
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            with self.assertRaises(
                RUNTIME.DurableCMSSourceRuntimeBlocked,
            ) as blocked:
                runtime.stop_worker(timeout_seconds=0.05)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(
                blocked.exception.code, "source_runtime.worker_stop_timeout",
            )
            release.set()
            runtime.stop_worker(timeout_seconds=1)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
