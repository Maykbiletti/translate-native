"""Synthetic protected-facility runtime tests; not native-quality evidence."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from wsgiref.simple_server import make_server

import test_website_localization_subagent_backend_http as BACKEND_TEST
import test_website_localization_subagent_executor as EXEC_TEST
import test_website_localization_subagent_facility as FACILITY_TEST
import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagents as BASE


RUNTIME = BASE.load(
    "test_website_localization_subagent_facility_runtime_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_facility_runtime.py",
)
BOOTSTRAP = BASE.load(
    "test_website_localization_subagent_backend_bootstrap_impl",
    BASE.ROOT / "integrations"
    / "website_localization_subagent_backend_bootstrap.py",
)


class FacilityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        if os.name != "nt":
            self.root.chmod(0o700)
        self.module_path = self.root / "synthetic_subagent_driver.py"
        self.module_path.write_text(
            "import hashlib\n"
            "import json\n"
            "from pathlib import Path\n"
            "def raw(value):\n"
            "    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')\n"
            "class PersistentFixtureDriver:\n"
            "    driver_id = 'synthetic-host-driver'\n"
            "    driver_version = 'fixture-1'\n"
            "    supports_atomic_idempotency = True\n"
            "    supports_reconcile = True\n"
            "    supports_hard_deadline = True\n"
            "    supports_isolated_context = True\n"
            "    supports_preflight = True\n"
            "    deployment_manifest_sha256 = '9' * 64\n"
            "    def __init__(self, state_file):\n"
            "        self.state_file = Path(state_file)\n"
            "    def execute_idempotent(self, assignment, model_input, **kwargs):\n"
            "        state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {'starts': 0, 'results': {}}\n"
            "        key = kwargs['provider_execution_key']\n"
            "        if key not in state['results']:\n"
            "            state['starts'] += 1\n"
            "            locale = model_input['input']['target']['locale']\n"
            "            if model_input['schema'] == 'translate-native.response-subagent-review.v1':\n"
            "                response = {'schema': 'translate-native.response-native-review.v1', 'phase': 'target_native', 'locale': locale, 'status': 'PASS', 'confidence': 'high', 'findings': [], 'uncertainties': []}\n"
            "            else:\n"
            "                response = {'schema': 'blun.website-localization-review.v2', 'phase': model_input['phase'], 'locale': locale, 'status': 'PASS', 'confidence': 'high', 'blocking_defects': [], 'major_defects': []}\n"
            "            actual = {'response': response, 'phase': assignment['phase'], 'reviewer_role': assignment['reviewer_role'], 'agent_id': assignment['reviewer_agent_id'], 'session_id': assignment['reviewer_session_id'], 'model_id': assignment['model_id'], 'model_version': assignment['model_version'], 'inherit_context': False, 'tools': [], 'max_delegation_depth': 0}\n"
            "            usage = {'provider_request_sha256': kwargs['provider_request_sha256'], 'cost_unit': kwargs['budgets']['cost_unit'], 'cost_units': 7, 'input_bytes': len(raw(model_input)), 'output_tokens': 1}\n"
            "            state['results'][key] = {'status': 'completed', 'provider_execution_key': key, 'provider_request_sha256': kwargs['provider_request_sha256'], 'actual_execution': actual, 'usage': usage}\n"
            "            self.state_file.write_text(json.dumps(state))\n"
            "        return state['results'][key]\n"
            "    def preflight(self, requirements, **kwargs):\n"
            "        return {'schema': 'translate-native.subagent-review-facility-preflight-result.v1', 'status': 'ready', 'challenge': requirements['challenge'], 'requirements_sha256': hashlib.sha256(raw(requirements)).hexdigest(), 'driver_deployment_sha256': requirements['driver_deployment_sha256'], 'deployment_manifest_sha256': self.deployment_manifest_sha256, 'route_requirements_sha256': requirements['route_requirements_sha256'], 'capabilities': requirements['required_capabilities']}\n"
            "    def reconcile(self, assignment, **kwargs):\n"
            "        state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {'starts': 0, 'results': {}}\n"
            "        result = state['results'].get(kwargs['provider_execution_key'])\n"
            "        if result is not None:\n"
            "            return result\n"
            "        return {'status': 'not_started', 'provider_execution_key': kwargs['provider_execution_key'], 'provider_request_sha256': kwargs['provider_request_sha256'], 'actual_execution': None, 'usage': None}\n"
            "def build(settings):\n"
            "    return PersistentFixtureDriver(settings['state_file'])\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.module_path.chmod(0o600)
        self.factory_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        self.token_file = self.write(
            "facility.token", BACKEND_TEST.FACILITY_TOKEN,
        )
        self.driver_file = self.write_json("driver.json", {
            "schema": RUNTIME.DRIVER_SCHEMA,
            "driver_id": "synthetic-host-driver",
            "driver_version": "fixture-1",
            "settings": {"state_file": str(self.root / "driver-state.json")},
        })
        self.ledger = self.root / "facility.sqlite3"
        self.driver_state = self.root / "driver-state.json"

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
            "backend_id": "production-host-subagents",
            "backend_version": "backend-1",
            "facility_id": "host-subagent-facility",
            "facility_version": "facility-1",
            "allow_loopback_http": True,
            "authentication": {
                "scheme": "bearer", "token_file": str(self.token_file),
            },
            "ledger": {
                "path": str(self.ledger), "max_concurrent_executions": 4,
            },
            "health": {
                "preflight_interval_seconds": 60,
                "max_staleness_seconds": 120,
            },
            "driver": {
                "factory_file": str(self.module_path),
                "factory_callable": "build",
                "factory_sha256": self.factory_sha256,
                "driver_id": "synthetic-host-driver",
                "driver_version": "fixture-1",
                "config_file": str(self.driver_file),
            },
            "routes": [self.route_dict(route) for route in routes],
        }
        value.update(changes)
        return self.write_json("facility.json", value)

    @staticmethod
    def backend(runtime):
        preflight = runtime.preflight
        return BACKEND_TEST.BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + BACKEND_TEST.FACILITY_TOKEN},
            backend_id="production-host-subagents",
            backend_version="backend-1",
            facility_id="host-subagent-facility",
            facility_version="facility-1",
            transport=FACILITY_TEST.WSGIFacilityTransport(runtime.application),
            allow_loopback_http=True,
            expected_driver_deployment_sha256=(
                preflight["driver_deployment_sha256"]
            ),
            expected_deployment_manifest_sha256=(
                preflight["deployment_manifest_sha256"]
            ),
            expected_route_requirements_sha256=(
                preflight["route_requirements_sha256"]
            ),
            expected_readiness_policy_sha256=(
                preflight["readiness_policy_sha256"]
            ),
            expected_facility_ledger_instance_id=(
                preflight["facility_ledger_instance_id"]
            ),
            expected_routes_count=preflight["routes_checked"],
        )

    def test_finnish_roundtrip_and_restart_replay(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        try:
            first = self.backend(runtime).execute_idempotent(
                vars(assignment), model_input,
                execute_request_sha256="1" * 64, budgets=budgets,
            )
        finally:
            runtime.close()
        self.assertEqual(first["execution"]["response"]["locale"], "fi-FI")
        self.assertEqual(json.loads(self.driver_state.read_text())["starts"], 1)

        runtime = RUNTIME.open_subagent_facility_runtime(config)
        try:
            replay = self.backend(runtime).execute_idempotent(
                vars(assignment), model_input,
                execute_request_sha256="1" * 64, budgets=budgets,
            )
        finally:
            runtime.close()
        self.assertEqual(replay, first)
        self.assertEqual(json.loads(self.driver_state.read_text())["starts"], 1)

    def test_reconcile_only_latch_is_persistent_and_instance_bound(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()

        runtime = RUNTIME.open_subagent_facility_runtime(
            config, begin_drain=True,
        )
        drain_id = runtime.drain["drain_id"]
        self.assertFalse(runtime.facility.accept_new_executions)
        self.assertEqual(runtime.facility.ledger.count_active(), 0)
        runtime.close()

        runtime = RUNTIME.open_subagent_facility_runtime(config)
        self.assertEqual(runtime.drain["drain_id"], drain_id)
        self.assertFalse(runtime.facility.accept_new_executions)
        runtime.close()

        marker = RUNTIME.DRAIN.marker_path(self.ledger)
        marker.unlink()
        with self.assertRaisesRegex(
                RUNTIME.SubagentFacilityRuntimeError, "drain latch is missing"):
            RUNTIME.open_subagent_facility_runtime(config)
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, begin_drain=True,
        )
        self.assertEqual(runtime.drain["drain_id"], drain_id)
        runtime.close()

        with sqlite3.connect(self.ledger) as connection:
            connection.execute("""
                INSERT INTO subagent_facility_jobs VALUES (
                    'legacy-retry',?,?,?,?,?,?,?,?,?,?,?,?,?,'not_started',1,
                    ?,0,128,'unit',1,NULL,NULL,0,0
                )
            """, ("a" * 64, "backend", "backend-1", "facility",
                  "facility-1", "b" * 64, "c" * 64, "d" * 64,
                  "e" * 64, "f" * 64, "0" * 64, "driver", "driver-1",
                  "1" * 64))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE subagent_facility_jobs SET status='dispatching' "
                    "WHERE execution_key='legacy-retry'"
                )
        with sqlite3.connect(self.ledger) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM subagent_facility_jobs "
                "WHERE execution_key='legacy-retry'"
            ).fetchone()[0], "not_started")

        document = json.loads(marker.read_text(encoding="utf-8"))
        document["facility_ledger_instance_id"] = "0" * 64
        marker.write_text(json.dumps(document), encoding="utf-8")
        if os.name != "nt":
            marker.chmod(0o600)
        with self.assertRaises(RUNTIME.SubagentFacilityRuntimeError):
            RUNTIME.open_subagent_facility_runtime(config)

    def test_reconcile_only_check_reports_content_free_drain_status(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        config = self.configuration([HOST_TEST.response_route(task)])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [
                "website_localization_subagent_facility_runtime.py",
                "--config", str(config), "--begin-drain", "--check",
        ]), contextlib.redirect_stdout(output):
            self.assertEqual(RUNTIME.main(), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["operation_mode"], "reconcile_only")
        self.assertRegex(status["drain_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(status["active_executions"], 0)
        self.assertTrue(status["drained"])

    def test_begin_drain_survives_driver_preflight_failure(self):
        task, _control = HOST_TEST.response_request("mt-MT")
        config = self.configuration([HOST_TEST.response_route(task)])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()

        with mock.patch.object(
                RUNTIME, "_preflight_routes",
                side_effect=RUNTIME.SubagentFacilityRuntimeError("offline")):
            with self.assertRaisesRegex(
                    RUNTIME.SubagentFacilityRuntimeError, "offline"):
                RUNTIME.open_subagent_facility_runtime(
                    config, begin_drain=True,
                )
        marker = RUNTIME.DRAIN.marker_path(self.ledger)
        self.assertTrue(marker.is_file())

        runtime = RUNTIME.open_subagent_facility_runtime(config)
        try:
            self.assertIsNotNone(runtime.drain)
            self.assertFalse(runtime.facility.accept_new_executions)
        finally:
            runtime.close()

    def test_restart_reconciles_provider_completion_before_facility_commit(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        upstream_digest = "c" * 64
        backend = self.backend(runtime)
        request, _body, _headers = backend._request(
            "execute", assignment, execute_request_sha256=upstream_digest,
            model_input=model_input, budgets=budgets,
        )
        provider_key, provider_digest = runtime.facility._provider_identity(request)
        reservation = runtime.facility.ledger.reserve_execute(
            request=request,
            principal_sha256=hashlib.sha256(
                ("Bearer " + BACKEND_TEST.FACILITY_TOKEN).encode("ascii")
            ).hexdigest(),
            assignment_sha256=RUNTIME.FACILITY._sha(request["assignment"]),
            model_input_sha256=RUNTIME.FACILITY._sha(request["model_input"]),
            provider_execution_key=provider_key,
            provider_request_sha256=provider_digest,
            driver_id=runtime.driver.driver_id,
            driver_version=runtime.driver.driver_version,
            input_bytes=len(RUNTIME.FACILITY._raw(request["model_input"])),
        )
        self.assertTrue(reservation.owner)
        runtime.driver.execute_idempotent(
            request["assignment"], request["model_input"],
            provider_execution_key=provider_key,
            provider_request_sha256=provider_digest,
            budgets=request["budgets"], isolation=RUNTIME.FACILITY.ISOLATION,
        )
        runtime.close()

        restarted = RUNTIME.open_subagent_facility_runtime(config)
        try:
            recovered = self.backend(restarted).reconcile(
                assignment, execute_request_sha256=upstream_digest,
            )
        finally:
            restarted.close()
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["execution"]["response"]["locale"], "fi-FI")
        self.assertEqual(json.loads(self.driver_state.read_text())["starts"], 1)

    def test_facility_ledger_instance_is_persistent_and_unique(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        first_config = self.configuration([route])
        first = RUNTIME.open_subagent_facility_runtime(
            first_config, initialize_ledger=True,
        )
        first_id = first.preflight["facility_ledger_instance_id"]
        first.close()
        reopened = RUNTIME.open_subagent_facility_runtime(first_config)
        try:
            self.assertEqual(
                reopened.preflight["facility_ledger_instance_id"], first_id,
            )
        finally:
            reopened.close()

        second_ledger = self.root / "facility-second.sqlite3"
        second_config = self.configuration(
            [route], ledger={
                "path": str(second_ledger), "max_concurrent_executions": 4,
            },
        )
        second = RUNTIME.open_subagent_facility_runtime(
            second_config, initialize_ledger=True,
        )
        try:
            self.assertNotEqual(
                second.preflight["facility_ledger_instance_id"], first_id,
            )
        finally:
            second.close()

    def test_maltese_runtime_preserves_two_isolated_phases(self):
        routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        config = self.configuration(routes)
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        fixture = EXEC_TEST.ExecutorTests()
        fixture.temporary = self.temporary
        executor = fixture.executor(routes, backend=self.backend(runtime))
        try:
            host = fixture.review_host(routes, fixture.launcher(executor))
            result = BASE.HostSubagentTests().execute(
                BASE.adapter(fixture.review_client(host)), "mt-MT",
            )
        finally:
            runtime.close()
        self.assertTrue(result["release_required"])
        self.assertEqual(json.loads(self.driver_state.read_text())["starts"], 2)

    def test_real_url_transport_reaches_facility_and_executor_runtime(self):
        fi_task, fi_control = HOST_TEST.response_request("fi-FI")
        fi_route = HOST_TEST.response_route(fi_task)
        mt_routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        routes = [fi_route, *mt_routes]
        facility_config = self.configuration(routes)
        initialized = RUNTIME.open_subagent_facility_runtime(
            facility_config, initialize_ledger=True,
        )
        initialized.close()

        backend_factory = self.root / "website_localization_subagent_backend_http.py"
        backend_factory.write_bytes((
            BASE.ROOT / "integrations"
            / "website_localization_subagent_backend_http.py"
        ).read_bytes())
        if os.name != "nt":
            backend_factory.chmod(0o600)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        facility_port = probe.getsockname()[1]
        probe.close()
        backend_config = self.root / "http-backend.json"
        backend_template = self.write_json("http-backend-template.json", {
            "schema": BACKEND_TEST.RUNTIME_TEST.RUNTIME.BACKEND_SCHEMA,
            "backend_id": "production-host-subagents",
            "backend_version": "backend-1",
            "settings": {
                "schema": BACKEND_TEST.BACKEND.SETTINGS_SCHEMA,
                "backend_id": "production-host-subagents",
                "backend_version": "backend-1",
                "facility_id": "host-subagent-facility",
                "facility_version": "facility-1",
                "endpoint": (
                    f"http://127.0.0.1:{facility_port}"
                    "/v1/isolated-review-executions"
                ),
                "authentication": {
                    "scheme": "bearer", "token_file": str(self.token_file),
                    "token_sha256": hashlib.sha256(
                        BACKEND_TEST.FACILITY_TOKEN.encode("ascii")
                    ).hexdigest(),
                },
                "readiness": None,
                "request_timeout_seconds": 10,
                "max_input_bytes": 2_000_000,
                "max_output_tokens": 4096,
                "cost_unit": "deployment-cost-unit",
                "max_cost_units": 100_000,
                "allow_loopback_http": True,
            },
        })
        executor_token = self.write(
            "executor.token", EXEC_TEST.EXECUTOR_TOKEN,
        )
        executor_ledger = self.root / "executor.sqlite3"
        executor_config = self.write_json("executor.json", {
            "schema": BACKEND_TEST.RUNTIME_TEST.RUNTIME.CONFIG_SCHEMA,
            "executor_id": "executor-1",
            "launcher_id": "deployment-review-launcher",
            "launcher_version": "launcher-1",
            "allow_loopback_http": True,
            "authentication": {
                "scheme": "bearer", "token_file": str(executor_token),
            },
            "ledger": {
                "path": str(executor_ledger), "max_concurrent_executions": 4,
            },
            "backend": {
                "factory_file": str(backend_factory),
                "factory_callable": "build_backend",
                "factory_sha256": hashlib.sha256(
                    backend_factory.read_bytes()
                ).hexdigest(),
                "backend_id": "production-host-subagents",
                "backend_version": "backend-1",
                "config_file": str(backend_config),
            },
            "routes": [self.route_dict(route) for route in routes],
        })
        receipt = BOOTSTRAP.bootstrap_backend(
            facility_config, backend_template, executor_config, backend_config,
        )
        self.assertEqual(receipt["status"], "created")
        self.assertTrue(receipt["content_free"])
        self.assertFalse(self.driver_state.exists())
        self.assertEqual(
            BOOTSTRAP.bootstrap_backend(
                facility_config, backend_template, executor_config, backend_config,
            )["status"],
            "already_materialized",
        )
        materialized = json.loads(backend_config.read_text(encoding="utf-8"))
        self.assertIsInstance(materialized["settings"]["readiness"], dict)
        self.assertNotIn("source", json.dumps(receipt))
        self.assertNotIn("target", json.dumps(receipt))
        self.assertNotIn(BACKEND_TEST.FACILITY_TOKEN, backend_config.read_text())

        facility_runtime = RUNTIME.open_subagent_facility_runtime(facility_config)
        server = make_server("127.0.0.1", facility_port, facility_runtime.application)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        executor_runtime = None
        try:
            executor_runtime = (
                BACKEND_TEST.RUNTIME_TEST.RUNTIME.open_subagent_executor_runtime(
                    executor_config, initialize_ledger=True,
                )
            )
            launcher = EXEC_TEST.LAUNCHER.HTTPSSubagentLauncher(
                "http://127.0.0.1/v1/subagent-executions",
                lambda: {
                    "Authorization": "Bearer " + EXEC_TEST.EXECUTOR_TOKEN,
                },
                launcher_id="deployment-review-launcher",
                launcher_version="launcher-1", executor_id="executor-1",
                transport=EXEC_TEST.WSGIExecutorTransport(
                    executor_runtime.application
                ),
                allow_loopback_http=True,
            )
            fi_assignment = HOST_TEST.HOST.ReviewHostApplication._assignment(
                fi_route, fi_control,
            )
            fi_result = launcher.execute_idempotent(
                fi_assignment,
                HOST_TEST.HOST.ReviewHostApplication._model_task(fi_task),
                deadline_seconds=fi_assignment.deadline_seconds,
                max_output_tokens=fi_assignment.max_output_tokens,
            )
            self.assertEqual(fi_result["response"]["locale"], "fi-FI")

            fixture = EXEC_TEST.ExecutorTests()
            fixture.temporary = self.temporary
            host = fixture.review_host(routes, launcher)
            mt_result = BASE.HostSubagentTests().execute(
                BASE.adapter(fixture.review_client(host)), "mt-MT",
            )
            self.assertTrue(mt_result["release_required"])
            self.assertEqual(json.loads(self.driver_state.read_text())["starts"], 3)
        finally:
            if executor_runtime is not None:
                executor_runtime.close()
            server.shutdown()
            server.server_close()
            server_thread.join(2)
            facility_runtime.close()

    def test_backend_bootstrap_rejects_route_drift_and_existing_ledger(self):
        fi_task, _control = HOST_TEST.response_request("fi-FI")
        fi_route = HOST_TEST.response_route(fi_task)
        facility_config = self.configuration([fi_route])
        initialized = RUNTIME.open_subagent_facility_runtime(
            facility_config, initialize_ledger=True,
        )
        initialized.close()
        backend_factory = self.root / "website_localization_subagent_backend_http.py"
        backend_factory.write_bytes((
            BASE.ROOT / "integrations"
            / "website_localization_subagent_backend_http.py"
        ).read_bytes())
        if os.name != "nt":
            backend_factory.chmod(0o600)
        output = self.root / "bootstrapped-backend.json"
        template = self.write_json("bootstrap-template.json", {
            "schema": BACKEND_TEST.RUNTIME_TEST.RUNTIME.BACKEND_SCHEMA,
            "backend_id": "production-host-subagents",
            "backend_version": "backend-1",
            "settings": {
                "schema": BACKEND_TEST.BACKEND.SETTINGS_SCHEMA,
                "backend_id": "production-host-subagents",
                "backend_version": "backend-1",
                "facility_id": "host-subagent-facility",
                "facility_version": "facility-1",
                "endpoint": "http://127.0.0.1:47643/v1/isolated-review-executions",
                "authentication": {
                    "scheme": "bearer", "token_file": str(self.token_file),
                    "token_sha256": hashlib.sha256(
                        BACKEND_TEST.FACILITY_TOKEN.encode("ascii")
                    ).hexdigest(),
                },
                "readiness": None,
                "request_timeout_seconds": 10,
                "max_input_bytes": 2_000_000,
                "max_output_tokens": 4096,
                "cost_unit": "deployment-cost-unit",
                "max_cost_units": 100_000,
                "allow_loopback_http": True,
            },
        })
        executor_token = self.write("bootstrap-executor.token", EXEC_TEST.EXECUTOR_TOKEN)
        executor_ledger = self.root / "bootstrap-executor.sqlite3"
        executor_value = {
            "schema": BACKEND_TEST.RUNTIME_TEST.RUNTIME.CONFIG_SCHEMA,
            "executor_id": "executor-1",
            "launcher_id": "deployment-review-launcher",
            "launcher_version": "launcher-1",
            "allow_loopback_http": True,
            "authentication": {"scheme": "bearer", "token_file": str(executor_token)},
            "ledger": {"path": str(executor_ledger), "max_concurrent_executions": 4},
            "backend": {
                "factory_file": str(backend_factory),
                "factory_callable": "build_backend",
                "factory_sha256": hashlib.sha256(backend_factory.read_bytes()).hexdigest(),
                "backend_id": "production-host-subagents",
                "backend_version": "backend-1",
                "config_file": str(output),
            },
            "routes": [self.route_dict(fi_route)],
        }
        executor_config = self.write_json("bootstrap-executor.json", executor_value)
        drifted = dict(executor_value)
        drifted["routes"] = [dict(executor_value["routes"][0])]
        drifted["routes"][0]["model_version"] = "wrong-model-generation"
        executor_config.write_text(json.dumps(drifted), encoding="utf-8")
        if os.name != "nt":
            executor_config.chmod(0o600)
        with self.assertRaisesRegex(
                BOOTSTRAP.SubagentBackendBootstrapError,
                "executor and facility routes differ"):
            BOOTSTRAP.bootstrap_backend(
                facility_config, template, executor_config, output,
            )
        self.assertFalse(output.exists())

        executor_config.write_text(json.dumps(executor_value), encoding="utf-8")
        if os.name != "nt":
            executor_config.chmod(0o600)
        executor_ledger.write_bytes(b"occupied")
        if os.name != "nt":
            executor_ledger.chmod(0o600)
        with self.assertRaisesRegex(
                BOOTSTRAP.SubagentBackendBootstrapError,
                "executor ledger already exists"):
            BOOTSTRAP.bootstrap_backend(
                facility_config, template, executor_config, output,
            )
        self.assertFalse(output.exists())

        executor_ledger.unlink()
        receipt = BOOTSTRAP.bootstrap_backend(
            facility_config, template, executor_config, output,
        )
        self.assertEqual(receipt["status"], "created")
        output.write_text("{}", encoding="utf-8")
        if os.name != "nt":
            output.chmod(0o600)
        with self.assertRaisesRegex(
                BOOTSTRAP.SubagentBackendBootstrapError,
                "different content"):
            BOOTSTRAP.bootstrap_backend(
                facility_config, template, executor_config, output,
            )

        output.unlink()
        active = RUNTIME.open_subagent_facility_runtime(facility_config)
        try:
            with self.assertRaisesRegex(
                    BOOTSTRAP.SubagentBackendBootstrapError,
                    "facility preflight blocked"):
                BOOTSTRAP.bootstrap_backend(
                    facility_config, template, executor_config, output,
                )
            self.assertFalse(output.exists())
        finally:
            active.close()

    def test_live_preflight_failure_blocks_new_start_then_recovers(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        try:
            with mock.patch.object(
                    RUNTIME, "_preflight_routes",
                    side_effect=RUNTIME.SubagentFacilityRuntimeError("offline")):
                self.assertFalse(runtime.supervisor.run_once())
            failed_snapshot = runtime.supervisor.snapshot()
            self.assertEqual(failed_snapshot["reason"], "preflight_failed")
            self.assertEqual(failed_snapshot["failure_generation"], 1)
            with self.assertRaises(
                    BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed) as blocked:
                self.backend(runtime).execute_idempotent(
                    assignment, model_input,
                    execute_request_sha256="6" * 64, budgets=budgets,
                )
            self.assertEqual(blocked.exception.code, "backend_http.status")
            self.assertTrue(blocked.exception.retryable)
            self.assertEqual(runtime.facility.ledger.count_active(), 0)
            self.assertFalse(self.driver_state.exists())

            with mock.patch.object(
                    RUNTIME, "_preflight_routes", return_value=runtime.preflight):
                self.assertTrue(runtime.supervisor.run_once())
            recovered_snapshot = runtime.supervisor.snapshot()
            self.assertEqual(recovered_snapshot["reason"], "ready")
            self.assertEqual(recovered_snapshot["failure_generation"], 1)
            result = self.backend(runtime).execute_idempotent(
                assignment, model_input,
                execute_request_sha256="6" * 64, budgets=budgets,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(json.loads(self.driver_state.read_text())["starts"], 1)
        finally:
            runtime.close()

    def test_readiness_lease_stales_and_preflight_is_single_flight(self):
        now = [10.0]
        initial = {
            "routes_checked": 1,
            "driver_deployment_sha256": "8" * 64,
            "deployment_manifest_sha256": "9" * 64,
            "route_requirements_sha256": "a" * 64,
            "readiness_policy_sha256": "b" * 64,
            "facility_ledger_instance_id": "c" * 64,
        }
        supervisor = RUNTIME._PreflightSupervisor(
            object(), [], interval_seconds=60, max_staleness_seconds=120,
            initial=initial, clock=lambda: now[0],
        )
        self.assertEqual(supervisor.snapshot()["reason"], "ready")
        now[0] = 131.0
        self.assertEqual(supervisor.snapshot()["reason"], "preflight_stale")

        entered, release = threading.Event(), threading.Event()

        def blocking_probe(_driver, _routes):
            entered.set()
            release.wait(1)
            return initial

        with mock.patch.object(RUNTIME, "_preflight_routes", blocking_probe):
            worker = threading.Thread(target=supervisor.run_once)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(supervisor.run_once())
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(supervisor.snapshot()["reason"], "ready")
        supervisor.close()
        self.assertEqual(supervisor.snapshot()["reason"], "monitor_stopped")

    def test_facility_authentication_is_validated_before_external_preflight(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        self.token_file.unlink()
        config = self.configuration([route])
        with mock.patch.object(RUNTIME, "_preflight_routes") as preflight:
            with self.assertRaises(RUNTIME.SubagentFacilityRuntimeError):
                RUNTIME.open_subagent_facility_runtime(
                    config, initialize_ledger=True,
                )
            preflight.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_deployment_drift_and_unsafe_files_fail_before_readiness(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        value = json.loads(config.read_text(encoding="utf-8"))
        value["routes"][0]["reviewer_agent_id"] = "different-native-reviewer"
        self.write_json("facility.json", value)
        with self.assertRaisesRegex(
                RUNTIME.SubagentFacilityRuntimeError, "deployment binding changed"):
            RUNTIME.open_subagent_facility_runtime(config)

        if os.name != "nt":
            self.driver_file.chmod(0o644)
            self.ledger.unlink()
            with self.assertRaisesRegex(
                    RUNTIME.SubagentFacilityRuntimeError, "driver configuration"):
                RUNTIME.open_subagent_facility_runtime(
                    config, initialize_ledger=True,
                )

    def test_invalid_driver_capabilities_fail_before_ledger(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        self.module_path.write_text(
            "class Driver:\n"
            "    driver_id = 'synthetic-host-driver'\n"
            "    driver_version = 'fixture-1'\n"
            "    supports_atomic_idempotency = True\n"
            "    supports_reconcile = True\n"
            "    supports_hard_deadline = False\n"
            "    supports_isolated_context = True\n"
            "    supports_preflight = True\n"
            "    deployment_manifest_sha256 = '9' * 64\n"
            "    def execute_idempotent(self, *args, **kwargs): return {}\n"
            "    def reconcile(self, *args, **kwargs): return {}\n"
            "    def preflight(self, *args, **kwargs): return {}\n"
            "def build(settings):\n"
            "    return Driver()\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.module_path.chmod(0o600)
        value = json.loads(config.read_text(encoding="utf-8"))
        value["driver"]["factory_sha256"] = hashlib.sha256(
            self.module_path.read_bytes()
        ).hexdigest()
        config = self.write_json("facility.json", value)
        with self.assertRaisesRegex(
                RUNTIME.SubagentFacilityRuntimeError, "invalid driver"):
            RUNTIME.open_subagent_facility_runtime(
                config, initialize_ledger=True,
            )
        self.assertFalse(self.ledger.exists())

    def test_closed_runtime_fails_closed(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        runtime.close()
        statuses = []
        response = b"".join(runtime.application({
            "PATH_INFO": RUNTIME.FACILITY.PATH, "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "http", "SERVER_NAME": "127.0.0.1",
            "CONTENT_LENGTH": "2", "wsgi.input": None,
        }, lambda status, _headers: statuses.append(status)))
        self.assertTrue(statuses[0].startswith("503"))
        self.assertIn(b"runtime_unavailable", response)

    def test_check_cli_reports_only_after_active_preflight(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [
                "website_localization_subagent_facility_runtime.py",
                "--config", str(config), "--initialize-ledger", "--check",
        ]), contextlib.redirect_stdout(output):
            self.assertEqual(RUNTIME.main(), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(set(status), {
            "content_free", "deployment_manifest_sha256",
            "driver_deployment_sha256", "driver_preflight", "ready",
            "readiness_policy_sha256", "route_requirements_sha256",
            "routes_checked", "facility_ledger_instance_id",
            "operation_mode", "drain_id", "active_executions", "drained",
        })
        self.assertEqual(status["content_free"], True)
        self.assertEqual(status["deployment_manifest_sha256"], "9" * 64)
        self.assertEqual(status["driver_preflight"], "passed")
        self.assertEqual(status["ready"], True)
        self.assertEqual(status["routes_checked"], 1)
        self.assertEqual(status["operation_mode"], "execute_and_reconcile")
        self.assertIsNone(status["drain_id"])
        self.assertEqual(status["active_executions"], 0)
        self.assertFalse(status["drained"])
        self.assertRegex(status["driver_deployment_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(status["route_requirements_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(status["readiness_policy_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(status["facility_ledger_instance_id"], r"^[0-9a-f]{64}$")
        self.assertTrue(self.ledger.exists())
        self.assertFalse(self.driver_state.exists())

    def test_second_runtime_cannot_fence_a_live_dispatch_owner(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        try:
            with self.assertRaisesRegex(
                    RUNTIME.SubagentFacilityRuntimeError, "already active"):
                RUNTIME.open_subagent_facility_runtime(config)
            self.assertEqual(runtime.facility.ledger.count_active(), 0)
        finally:
            runtime.close()

    def test_driver_call_is_killed_at_hard_deadline(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        self.module_path.write_text(
            "import time\n"
            "class SlowDriver:\n"
            "    driver_id = 'synthetic-host-driver'\n"
            "    driver_version = 'fixture-1'\n"
            "    supports_atomic_idempotency = True\n"
            "    supports_reconcile = True\n"
            "    supports_hard_deadline = True\n"
            "    supports_isolated_context = True\n"
            "    supports_preflight = True\n"
            "    deployment_manifest_sha256 = '9' * 64\n"
            "    def preflight(self, requirements, **kwargs):\n"
            "        import hashlib, json\n"
            "        raw = json.dumps(requirements, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')\n"
            "        return {'schema': 'translate-native.subagent-review-facility-preflight-result.v1', 'status': 'ready', 'challenge': requirements['challenge'], 'requirements_sha256': hashlib.sha256(raw).hexdigest(), 'driver_deployment_sha256': requirements['driver_deployment_sha256'], 'deployment_manifest_sha256': self.deployment_manifest_sha256, 'route_requirements_sha256': requirements['route_requirements_sha256'], 'capabilities': requirements['required_capabilities']}\n"
            "    def execute_idempotent(self, *args, **kwargs):\n"
            "        time.sleep(3)\n"
            "        return {}\n"
            "    def reconcile(self, *args, **kwargs): return {}\n"
            "def build(settings):\n"
            "    return SlowDriver()\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.module_path.chmod(0o600)
        self.factory_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        ).copy()
        assignment["deadline_seconds"] = 1
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        budgets["deadline_seconds"] = 1
        started = time.monotonic()
        try:
            with self.assertRaises(BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed):
                self.backend(runtime).execute_idempotent(
                    assignment, model_input,
                    execute_request_sha256="d" * 64, budgets=budgets,
                )
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(runtime.facility.ledger.count_active(), 1)
        finally:
            runtime.close()

    def test_driver_cleanup_remains_inside_hard_deadline(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        source = "import time\n" + self.module_path.read_text(encoding="utf-8")
        source = source.replace(
            "        self.state_file = Path(state_file)\n",
            "        self.state_file = Path(state_file)\n        self.used = False\n",
        ).replace(
            "    def execute_idempotent(self, assignment, model_input, **kwargs):\n",
            "    def execute_idempotent(self, assignment, model_input, **kwargs):\n        self.used = True\n",
        ).replace(
            "    def reconcile(self, assignment, **kwargs):\n",
            "    def close(self):\n        if self.used:\n            time.sleep(3)\n"
            "    def reconcile(self, assignment, **kwargs):\n",
        )
        self.module_path.write_text(source, encoding="utf-8")
        if os.name != "nt":
            self.module_path.chmod(0o600)
        self.factory_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        ).copy()
        assignment["deadline_seconds"] = 1
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        budgets["deadline_seconds"] = 1
        started = time.monotonic()
        try:
            with self.assertRaises(BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed):
                self.backend(runtime).execute_idempotent(
                    assignment, model_input,
                    execute_request_sha256="b" * 64, budgets=budgets,
                )
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(runtime.facility.ledger.count_active(), 1)
        finally:
            runtime.close()

    def test_driver_config_cannot_change_after_runtime_preflight(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        changed_state = self.root / "changed-driver-state.json"
        self.write_json("driver.json", {
            "schema": RUNTIME.DRIVER_SCHEMA,
            "driver_id": "synthetic-host-driver",
            "driver_version": "fixture-1",
            "settings": {"state_file": str(changed_state)},
        })
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        try:
            with self.assertRaises(BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed):
                self.backend(runtime).execute_idempotent(
                    assignment, model_input,
                    execute_request_sha256="e" * 64, budgets=budgets,
                )
            self.assertFalse(self.driver_state.exists())
            self.assertFalse(changed_state.exists())
            self.assertEqual(runtime.facility.ledger.count_active(), 1)
        finally:
            runtime.close()

    def test_terminal_driver_rejection_stays_terminal_across_worker(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        self.module_path.write_text(
            "class Rejected(RuntimeError):\n"
            "    retryable = False\n"
            "class Driver:\n"
            "    driver_id = 'synthetic-host-driver'\n"
            "    driver_version = 'fixture-1'\n"
            "    supports_atomic_idempotency = True\n"
            "    supports_reconcile = True\n"
            "    supports_hard_deadline = True\n"
            "    supports_isolated_context = True\n"
            "    supports_preflight = True\n"
            "    deployment_manifest_sha256 = '9' * 64\n"
            "    def execute_idempotent(self, *args, **kwargs): raise Rejected()\n"
            "    def reconcile(self, *args, **kwargs): raise Rejected()\n"
            "    def preflight(self, requirements, **kwargs):\n"
            "        import hashlib, json\n"
            "        raw = json.dumps(requirements, sort_keys=True, separators=(',', ':')).encode()\n"
            "        return {'schema': 'translate-native.subagent-review-facility-preflight-result.v1', 'status': 'ready', 'challenge': requirements['challenge'], 'requirements_sha256': hashlib.sha256(raw).hexdigest(), 'driver_deployment_sha256': requirements['driver_deployment_sha256'], 'deployment_manifest_sha256': self.deployment_manifest_sha256, 'route_requirements_sha256': requirements['route_requirements_sha256'], 'capabilities': requirements['required_capabilities']}\n"
            "def build(settings): return Driver()\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.module_path.chmod(0o600)
        self.factory_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        try:
            with self.assertRaises(
                    BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed) as blocked:
                self.backend(runtime).execute_idempotent(
                    assignment, model_input,
                    execute_request_sha256="f" * 64, budgets=budgets,
                )
            self.assertFalse(blocked.exception.retryable)
            self.assertEqual(runtime.facility.ledger.count_active(), 1)
        finally:
            runtime.close()

    def test_driver_worker_ignores_ambient_python_startup_paths(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        marker = self.root / "ambient-sitecustomize-ran"
        ambient = self.root / "ambient"
        ambient.mkdir()
        (ambient / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        config = self.configuration([route])
        previous = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(ambient)
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        try:
            result = self.backend(runtime).execute_idempotent(
                assignment, model_input,
                execute_request_sha256="a" * 64, budgets=budgets,
            )
            self.assertEqual(result["status"], "completed")
            self.assertFalse(marker.exists())
        finally:
            runtime.close()
            if previous is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous


if __name__ == "__main__":
    unittest.main()
