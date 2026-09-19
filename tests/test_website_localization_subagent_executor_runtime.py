"""Synthetic protected-executor runtime tests; not native-quality evidence."""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_website_localization_subagent_executor as ENDPOINT
import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagents as BASE


RUNTIME = BASE.load(
    "test_website_localization_subagent_executor_runtime_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_executor_runtime.py",
)
ACTIVE_BACKEND = None
FACTORY_SETTINGS = []


def backend_factory(settings):
    FACTORY_SETTINGS.append(settings)
    return ACTIVE_BACKEND


class ExecutorRuntimeTests(unittest.TestCase):
    def setUp(self):
        global ACTIVE_BACKEND
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        if os.name != "nt":
            self.root.chmod(0o700)
        self.module_path = self.root / "synthetic_subagent_backend.py"
        self.module_path.write_text(
            "import importlib\n"
            f"def build(settings):\n    return importlib.import_module({__name__!r}).backend_factory(settings)\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.module_path.chmod(0o600)
        self.factory_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        self.token_file = self.write("executor.token", ENDPOINT.EXECUTOR_TOKEN)
        self.backend_file = self.write_json("backend.json", {
            "schema": RUNTIME.BACKEND_SCHEMA,
            "backend_id": "fixture-host-subagents",
            "backend_version": "fixture-1",
            "settings": {"host_facility": "synthetic-test-fixture"},
        })
        self.ledger = self.root / "executor.sqlite3"
        ACTIVE_BACKEND = ENDPOINT.FixtureBackend()
        FACTORY_SETTINGS.clear()

    def write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="ascii")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    @staticmethod
    def route_dict(route):
        return {
            "route_id": route.route_id, "phase": route.phase,
            "reviewer_role": route.reviewer_role,
            "reviewer_agent_id": route.reviewer_agent_id,
            "model_id": route.model_id, "model_version": route.model_version,
            "host_policy_version": route.host_policy_version,
            "target_locale": route.target_locale,
            "content_type": route.content_type,
            "task_policy_sha256": route.task_policy_sha256,
        }

    def configuration(self, routes, **changes):
        value = {
            "schema": RUNTIME.CONFIG_SCHEMA,
            "executor_id": "executor-1",
            "launcher_id": "deployment-review-launcher",
            "launcher_version": "launcher-1",
            "allow_loopback_http": True,
            "authentication": {
                "scheme": "bearer", "token_file": str(self.token_file),
            },
            "ledger": {
                "path": str(self.ledger), "max_concurrent_executions": 4,
            },
            "backend": {
                "factory_file": str(self.module_path),
                "factory_callable": "build",
                "factory_sha256": self.factory_sha256,
                "backend_id": "fixture-host-subagents",
                "backend_version": "fixture-1",
                "config_file": str(self.backend_file),
            },
            "routes": [self.route_dict(route) for route in routes],
        }
        value.update(changes)
        return self.write_json("executor.json", value)

    @staticmethod
    def launcher(runtime):
        return ENDPOINT.LAUNCHER.HTTPSSubagentLauncher(
            "http://127.0.0.1/v1/subagent-executions",
            lambda: {"Authorization": "Bearer " + ENDPOINT.EXECUTOR_TOKEN},
            launcher_id="deployment-review-launcher", launcher_version="launcher-1",
            executor_id="executor-1",
            transport=ENDPOINT.WSGIExecutorTransport(runtime.application),
            allow_loopback_http=True,
        )

    def test_finnish_roundtrip_and_restart_replay(self):
        global ACTIVE_BACKEND
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        assignment = HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        first_backend = ACTIVE_BACKEND
        try:
            first = self.launcher(runtime).execute_idempotent(
                assignment, model_input,
                deadline_seconds=assignment.deadline_seconds,
                max_output_tokens=assignment.max_output_tokens,
            )
        finally:
            runtime.close()
        self.assertEqual(first["response"]["locale"], "fi-FI")
        self.assertEqual(len(first_backend.starts), 1)
        self.assertEqual(FACTORY_SETTINGS, [
            {"host_facility": "synthetic-test-fixture"},
        ])

        ACTIVE_BACKEND = ENDPOINT.FixtureBackend()
        runtime = RUNTIME.open_subagent_executor_runtime(config)
        try:
            replay = self.launcher(runtime).execute_idempotent(
                assignment, model_input,
                deadline_seconds=assignment.deadline_seconds,
                max_output_tokens=assignment.max_output_tokens,
            )
        finally:
            runtime.close()
        self.assertEqual(replay, first)
        self.assertEqual(ACTIVE_BACKEND.starts, [])

    def test_reconcile_only_latch_is_persistent_and_deployment_bound(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()

        ACTIVE_BACKEND.readiness_result = {
            "ready": True, "fixture": True,
            "operation_mode": "reconcile_only", "drain_id": "f" * 64,
        }
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, begin_drain=True,
        )
        drain_id = runtime.drain["drain_id"]
        self.assertFalse(runtime.executor.accept_new_executions)
        self.assertEqual(runtime.executor.ledger.count_active(), 0)
        runtime.close()

        runtime = RUNTIME.open_subagent_executor_runtime(config)
        self.assertEqual(runtime.drain["drain_id"], drain_id)
        self.assertFalse(runtime.executor.accept_new_executions)
        runtime.close()

        marker = RUNTIME.DRAIN.marker_path(self.ledger)
        marker.unlink()
        with self.assertRaisesRegex(
                RUNTIME.SubagentExecutorRuntimeError, "drain latch is missing"):
            RUNTIME.open_subagent_executor_runtime(config)
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, begin_drain=True,
        )
        self.assertEqual(runtime.drain["drain_id"], drain_id)
        runtime.close()

        with sqlite3.connect(self.ledger) as connection:
            connection.execute("""
                INSERT INTO subagent_executor_jobs VALUES (
                    'legacy-retry',?,?,?,?,?,?,'not_started',1,0,128,'unit',1,
                    NULL,NULL,0,0
                )
            """, ("a" * 64, "b" * 64, "c" * 64,
                  "launcher", "launcher-1", "executor-1"))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE subagent_executor_jobs SET status='dispatching' "
                    "WHERE execution_key='legacy-retry'"
                )
        with sqlite3.connect(self.ledger) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM subagent_executor_jobs "
                "WHERE execution_key='legacy-retry'"
            ).fetchone()[0], "not_started")

        document = json.loads(marker.read_text(encoding="utf-8"))
        document["deployment_binding_sha256"] = "0" * 64
        marker.write_text(json.dumps(document), encoding="utf-8")
        if os.name != "nt":
            marker.chmod(0o600)
        with self.assertRaises(RUNTIME.SubagentExecutorRuntimeError):
            RUNTIME.open_subagent_executor_runtime(config)

    def test_reconcile_only_check_reports_content_free_drain_status(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        config = self.configuration([HOST_TEST.response_route(task)])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        ACTIVE_BACKEND.readiness_result = {
            "ready": True, "fixture": True,
            "operation_mode": "reconcile_only", "drain_id": "f" * 64,
        }
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [
                "website_localization_subagent_executor_runtime.py",
                "--config", str(config), "--begin-drain", "--check",
        ]), contextlib.redirect_stdout(output):
            self.assertEqual(RUNTIME.main(), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["operation_mode"], "reconcile_only")
        self.assertRegex(status["drain_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(status["executor_active_executions"], 0)
        self.assertTrue(status["drained"])
        self.assertEqual(set(status), {
            "ready", "content_free", "operation_mode", "drain_id",
            "executor_active_executions", "drained",
        })

    def test_begin_drain_survives_backend_readiness_failure(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        config = self.configuration([HOST_TEST.response_route(task)])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()

        ACTIVE_BACKEND.readiness_result = {
            "ready": True, "fixture": True,
            "operation_mode": "reconcile_only", "drain_id": "f" * 64,
        }
        ACTIVE_BACKEND.readiness_error = RuntimeError("facility unavailable")
        with self.assertRaisesRegex(
                RUNTIME.SubagentExecutorRuntimeError,
                "facility readiness failed"):
            RUNTIME.open_subagent_executor_runtime(config, begin_drain=True)
        marker = RUNTIME.DRAIN.marker_path(self.ledger)
        self.assertTrue(marker.is_file())

        ACTIVE_BACKEND.readiness_error = None
        runtime = RUNTIME.open_subagent_executor_runtime(config)
        try:
            self.assertIsNotNone(runtime.drain)
            self.assertFalse(runtime.executor.accept_new_executions)
        finally:
            runtime.close()

    def test_drain_status_rejects_unknown_persisted_status(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        config = self.configuration([HOST_TEST.response_route(task)])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        ACTIVE_BACKEND.readiness_result = {
            "ready": True, "fixture": True,
            "operation_mode": "reconcile_only", "drain_id": "f" * 64,
        }
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, begin_drain=True,
        )
        try:
            with sqlite3.connect(self.ledger) as connection:
                connection.execute("""
                    INSERT INTO subagent_executor_jobs VALUES (
                        'tampered-status',?,?,?,?,?,?,'runing',1,0,128,'unit',1,
                        NULL,NULL,0,0
                    )
                """, ("a" * 64, "b" * 64, "c" * 64,
                      "launcher", "launcher-1", "executor-1"))
            with self.assertRaisesRegex(ValueError, "invalid status"):
                runtime.executor.ledger.drain_active_count()
        finally:
            runtime.close()

    def test_readiness_mode_matches_local_drain_and_fences_stale_dispatch(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        config = self.configuration([HOST_TEST.response_route(task)])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("""
                INSERT INTO subagent_executor_jobs VALUES (
                    'crashed-dispatch',?,?,?,?,?,?,'dispatching',1,0,128,
                    'unit',1,NULL,NULL,0,0
                )
            """, ("a" * 64, "b" * 64, "c" * 64,
                  "launcher", "launcher-1", "executor-1"))
        ACTIVE_BACKEND.readiness_result = {
            "ready": True, "fixture": True,
            "operation_mode": "reconcile_only", "drain_id": "f" * 64,
        }
        with self.assertRaisesRegex(
                RUNTIME.SubagentExecutorRuntimeError,
                "facility readiness failed"):
            RUNTIME.open_subagent_executor_runtime(config)
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, begin_drain=True,
        )
        try:
            with sqlite3.connect(self.ledger) as connection:
                status = connection.execute(
                    "SELECT status FROM subagent_executor_jobs "
                    "WHERE execution_key='crashed-dispatch'"
                ).fetchone()[0]
            self.assertEqual(status, "unknown")
            self.assertEqual(runtime.executor.ledger.drain_active_count(), 1)
        finally:
            runtime.close()

    def test_maltese_runtime_keeps_native_and_fidelity_separate(self):
        routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        config = self.configuration(routes)
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        fixture = ENDPOINT.ExecutorTests()
        fixture.temporary = self.temporary
        try:
            host = fixture.review_host(routes, self.launcher(runtime))
            result = BASE.HostSubagentTests().execute(
                BASE.adapter(fixture.review_client(host)), "mt-MT",
            )
        finally:
            runtime.close()
        self.assertTrue(result["release_required"])
        self.assertEqual([item[0]["phase"] for item in ACTIVE_BACKEND.starts],
                         ["target_native", "source_fidelity"])
        self.assertNotIn("source", ACTIVE_BACKEND.starts[0][1]["input"])

    def test_live_backend_readiness_precedes_ledger_and_dispatch(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        ACTIVE_BACKEND.readiness_error = RuntimeError("facility unavailable")
        with self.assertRaisesRegex(
                RUNTIME.SubagentExecutorRuntimeError,
                "facility readiness failed"):
            RUNTIME.open_subagent_executor_runtime(
                config, initialize_ledger=True,
            )
        self.assertEqual(ACTIVE_BACKEND.readiness_calls, 1)
        self.assertEqual(ACTIVE_BACKEND.starts, [])
        self.assertFalse(self.ledger.exists())

        for malformed in (None, {}, {"ready": False}):
            with self.subTest(malformed=malformed):
                ACTIVE_BACKEND.readiness_error = None
                ACTIVE_BACKEND.readiness_result = malformed
                with self.assertRaisesRegex(
                        RUNTIME.SubagentExecutorRuntimeError,
                        "facility readiness failed"):
                    RUNTIME.open_subagent_executor_runtime(
                        config, initialize_ledger=True,
                    )
                self.assertFalse(self.ledger.exists())

        ACTIVE_BACKEND.readiness_error = None
        ACTIVE_BACKEND.readiness_result = {
            "ready": True, "fixture": True,
            "operation_mode": "execute_and_reconcile", "drain_id": None,
        }
        readiness_calls = ACTIVE_BACKEND.readiness_calls
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        try:
            self.assertEqual(ACTIVE_BACKEND.readiness_calls,
                             readiness_calls + 1)
        finally:
            runtime.close()

    def test_executor_authentication_precedes_external_readiness(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        self.token_file.unlink()
        with self.assertRaises(RUNTIME.SubagentExecutorRuntimeError):
            RUNTIME.open_subagent_executor_runtime(
                config, initialize_ledger=True,
            )
        self.assertEqual(ACTIVE_BACKEND.readiness_calls, 0)
        self.assertFalse(self.ledger.exists())

    def test_deployment_binding_is_rechecked_after_remote_readiness(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()

        def replace_binding():
            with sqlite3.connect(self.ledger) as connection:
                connection.execute(
                    "UPDATE subagent_executor_deployment "
                    "SET binding_sha256=? WHERE singleton=1", ("0" * 64,),
                )

        ACTIVE_BACKEND.readiness_hook = replace_binding
        with self.assertRaisesRegex(
                RUNTIME.SubagentExecutorRuntimeError,
                "deployment binding changed"):
            RUNTIME.open_subagent_executor_runtime(config)
        self.assertEqual(ACTIVE_BACKEND.starts, [])

    def test_deployment_drift_and_unsafe_files_block_before_backend(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        value = json.loads(config.read_text(encoding="utf-8"))
        value["routes"][0]["reviewer_agent_id"] = "different-native-reviewer"
        self.write_json("executor.json", value)
        calls = ACTIVE_BACKEND.readiness_calls
        with self.assertRaisesRegex(
                RUNTIME.SubagentExecutorRuntimeError, "deployment binding changed"):
            RUNTIME.open_subagent_executor_runtime(config)
        self.assertEqual(ACTIVE_BACKEND.readiness_calls, calls)

        if os.name != "nt":
            self.backend_file.chmod(0o644)
            self.ledger.unlink()
            with self.assertRaisesRegex(
                    RUNTIME.SubagentExecutorRuntimeError, "backend configuration"):
                RUNTIME.open_subagent_executor_runtime(
                    config, initialize_ledger=True,
                )

    def test_closed_runtime_and_missing_ledger_fail_closed(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        with self.assertRaisesRegex(
                RUNTIME.SubagentExecutorRuntimeError, "ledger is missing"):
            RUNTIME.open_subagent_executor_runtime(config)
        runtime = RUNTIME.open_subagent_executor_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        statuses = []
        body = b"{}"
        response = b"".join(runtime.application({
            "PATH_INFO": ENDPOINT.EXECUTOR.PATH, "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "http", "SERVER_NAME": "127.0.0.1",
            "CONTENT_LENGTH": str(len(body)), "wsgi.input": None,
        }, lambda status, _headers: statuses.append(status)))
        self.assertTrue(statuses[0].startswith("503"))
        self.assertIn(b"runtime_unavailable", response)


if __name__ == "__main__":
    unittest.main()
