"""Synthetic protected-facility runtime tests; not native-quality evidence."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_website_localization_subagent_backend_http as BACKEND_TEST
import test_website_localization_subagent_executor as EXEC_TEST
import test_website_localization_subagent_facility as FACILITY_TEST
import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagents as BASE


RUNTIME = BASE.load(
    "test_website_localization_subagent_facility_runtime_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_facility_runtime.py",
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
        return BACKEND_TEST.BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + BACKEND_TEST.FACILITY_TOKEN},
            backend_id="production-host-subagents",
            backend_version="backend-1",
            facility_id="host-subagent-facility",
            facility_version="facility-1",
            transport=FACILITY_TEST.WSGIFacilityTransport(runtime.application),
            allow_loopback_http=True,
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
        self.assertEqual(status, {
            "content_free": True,
            "driver_preflight": "passed",
            "ready": True,
            "routes_checked": 1,
        })
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
