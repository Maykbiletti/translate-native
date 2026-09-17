"""Synthetic command-driver fixtures; not native-quality or DeepL evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_website_localization_subagent_backend_http as BACKEND_TEST
import test_website_localization_subagent_executor as EXEC_TEST
import test_website_localization_subagent_facility as FACILITY_TEST
import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagents as BASE


RUNTIME = BASE.load(
    "test_command_driver_facility_runtime",
    BASE.ROOT / "integrations" / "website_localization_subagent_facility_runtime.py",
)
COMMAND = BASE.load(
    "test_website_localization_subagent_driver_command_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_driver_command.py",
)


FIXTURE = r'''#!/usr/bin/python3
import hashlib
import json
import os
import sys
import time
from pathlib import Path

STATE = Path(sys.argv[1])
CAPTURE = Path(sys.argv[2])
MODE = Path(sys.argv[3])

def raw(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
unsigned = {key: value for key, value in request.items() if key != 'request_sha256'}
if hashlib.sha256(raw(unsigned)).hexdigest() != request['request_sha256']:
    raise SystemExit(21)
os.fstat(request['lifetime_fd'])
captures = json.loads(CAPTURE.read_text()) if CAPTURE.exists() else []
captures.append({
    'request': request,
    'environment': dict(os.environ),
    'cwd': os.getcwd(),
    'fds': sorted(int(name) for name in os.listdir('/proc/self/fd') if name.isdigit()),
})
CAPTURE.write_text(json.dumps(captures, ensure_ascii=False))
state = json.loads(STATE.read_text()) if STATE.exists() else {'starts': 0, 'results': {}}
operation = request['operation']
key = request.get('provider_execution_key')
digest = request.get('provider_request_sha256')
mode = MODE.read_text().strip() if MODE.exists() else 'normal'
behavior = (mode.removeprefix('preflight_')
            if operation == 'preflight' and mode.startswith('preflight_')
            else ('normal' if operation == 'preflight' else mode))
if operation == 'preflight':
    requirements = request['requirements']
    result = {
        'schema': 'translate-native.subagent-review-facility-preflight-result.v1',
        'status': 'ready',
        'challenge': requirements['challenge'],
        'requirements_sha256': hashlib.sha256(raw(requirements)).hexdigest(),
        'driver_deployment_sha256': requirements['driver_deployment_sha256'],
        'deployment_manifest_sha256': request['deployment_manifest_sha256'],
        'route_requirements_sha256': requirements['route_requirements_sha256'],
        'capabilities': requirements['required_capabilities'],
    }
    if behavior == 'wrong_preflight_challenge':
        result['challenge'] = '0' * 64
    elif behavior == 'not_ready':
        result['status'] = 'blocked'
    elif behavior == 'wrong_route_digest':
        result['route_requirements_sha256'] = '0' * 64
    elif behavior == 'wrong_capabilities':
        result['capabilities']['no_model_start'] = False
elif operation == 'execute':
    if key not in state['results']:
        state['starts'] += 1
        assignment = request['assignment']
        model_input = request['model_input']
        locale = model_input['input']['target']['locale']
        if model_input['schema'] == 'translate-native.response-subagent-review.v1':
            review = {'schema': 'translate-native.response-native-review.v1', 'phase': 'target_native', 'locale': locale, 'status': 'PASS', 'confidence': 'high', 'findings': [], 'uncertainties': []}
        else:
            review = {'schema': 'blun.website-localization-review.v2', 'phase': model_input['phase'], 'locale': locale, 'status': 'PASS', 'confidence': 'high', 'blocking_defects': [], 'major_defects': []}
        actual = {
            'response': review,
            'phase': assignment['phase'],
            'reviewer_role': assignment['reviewer_role'],
            'agent_id': assignment['reviewer_agent_id'],
            'session_id': assignment['reviewer_session_id'],
            'model_id': assignment['model_id'],
            'model_version': assignment['model_version'],
            'inherit_context': False,
            'tools': [],
            'max_delegation_depth': 0,
        }
        usage = {
            'provider_request_sha256': digest,
            'cost_unit': request['budgets']['cost_unit'],
            'cost_units': 7,
            'input_bytes': len(raw(model_input)),
            'output_tokens': 1,
        }
        state['results'][key] = {
            'status': 'completed',
            'provider_execution_key': key,
            'provider_request_sha256': digest,
            'actual_execution': actual,
            'usage': usage,
        }
        STATE.write_text(json.dumps(state, ensure_ascii=False))
    result = state['results'][key]
else:
    result = state['results'].get(key, {
        'status': 'not_started',
        'provider_execution_key': key,
        'provider_request_sha256': digest,
        'actual_execution': None,
        'usage': None,
    })
response = {
        'schema': ('translate-native.subagent-review-command-preflight-response.v1'
                   if operation == 'preflight' else
                   'translate-native.subagent-review-command-response.v2'),
        'operation': operation,
        'protocol_version': '2',
        'driver_id': 'standard-command-driver',
        'driver_version': 'command-v2',
        'command_id': 'synthetic-host-command',
        'command_version': 'fixture-1',
        'deployment_manifest_sha256': request['deployment_manifest_sha256'],
        'request_sha256': request['request_sha256'],
}
if operation != 'preflight':
    response.update({
        'provider_execution_key': key,
        'provider_request_sha256': digest,
    })
if behavior in {'terminal', 'terminal_wrong_binding'}:
    response.update({
        'ok': False,
        'retryable': False,
        'result': None,
    })
    if behavior == 'terminal_wrong_binding':
        response['request_sha256'] = '0' * 64
elif behavior == 'nonzero':
    raise SystemExit(23)
elif behavior == 'oversize':
    sys.stdout.write('x' * 100000)
    raise SystemExit(0)
elif behavior == 'hang':
    time.sleep(5)
    raise SystemExit(0)
elif behavior == 'malformed':
    sys.stdout.write('{"schema":1,"schema":2}')
    raise SystemExit(0)
else:
    if behavior == 'wrong_identity' and operation == 'execute':
        result['actual_execution']['agent_id'] = 'forged-reviewer'
    if behavior == 'wrong_usage' and operation == 'execute':
        result['usage']['cost_units'] = request.get('budgets', {'max_cost_units': 1})['max_cost_units'] + 1
    response.update({
        'ok': True,
        'retryable': False,
        'result': result,
    })
    if behavior == 'wrong_binding':
        response['request_sha256'] = '0' * 64
    if behavior == 'wrong_manifest':
        response['deployment_manifest_sha256'] = '0' * 64
sys.stdout.buffer.write(raw(response))
'''


class CommandDriverTests(unittest.TestCase):
    def setUp(self):
        if os.name != "posix" or not Path("/proc/self/fd").exists():
            self.skipTest("command-driver fixture requires Linux descriptor execution")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.factory = self.root / "command_driver.py"
        self.factory.write_bytes(
            (BASE.ROOT / "integrations" / "website_localization_subagent_driver_command.py").read_bytes()
        )
        self.factory.chmod(0o600)
        self.executable = self.root / "synthetic_host_command"
        self.executable.write_text(FIXTURE, encoding="utf-8")
        self.executable.chmod(0o700)
        self.state = self.root / "provider-state.json"
        self.capture = self.root / "command-captures.json"
        self.mode = self.root / "mode"
        self.mode.write_text("normal", encoding="ascii")
        self.mode.chmod(0o600)
        self.token_file = self.write(
            "facility.token", BACKEND_TEST.FACILITY_TOKEN,
        )
        self.driver_file = self.write_json("driver.json", {
            "schema": RUNTIME.DRIVER_SCHEMA,
            "driver_id": "standard-command-driver",
            "driver_version": "command-v2",
            "settings": {
                "schema": COMMAND.SETTINGS_SCHEMA,
                "driver_id": "standard-command-driver",
                "driver_version": "command-v2",
                "command_id": "synthetic-host-command",
                "command_version": "fixture-1",
                "executable": str(self.executable),
                "executable_sha256": hashlib.sha256(
                    self.executable.read_bytes()
                ).hexdigest(),
                "arguments": [str(self.state), str(self.capture), str(self.mode)],
                "working_directory": str(self.root),
                "max_response_bytes": 8192,
                "deployment_manifest_sha256": "8" * 64,
            },
        })
        self.ledger = self.root / "facility.sqlite3"

    def write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="ascii")
        path.chmod(0o600)
        return path

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
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

    def configuration(self, routes):
        return self.write_json("facility.json", {
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
                "factory_file": str(self.factory),
                "factory_callable": "build_driver",
                "factory_sha256": hashlib.sha256(self.factory.read_bytes()).hexdigest(),
                "driver_id": "standard-command-driver",
                "driver_version": "command-v2",
                "config_file": str(self.driver_file),
            },
            "routes": [self.route_dict(route) for route in routes],
        })

    @staticmethod
    def backend(runtime):
        return BACKEND_TEST.BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + BACKEND_TEST.FACILITY_TOKEN},
            backend_id="production-host-subagents", backend_version="backend-1",
            facility_id="host-subagent-facility", facility_version="facility-1",
            transport=FACILITY_TEST.WSGIFacilityTransport(runtime.application),
            allow_loopback_http=True,
        )

    def direct(self, runtime, task, control, digest="1" * 64):
        route = HOST_TEST.response_route(task)
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = {
            "deadline_seconds": assignment["deadline_seconds"],
            "max_output_tokens": assignment["max_output_tokens"],
            "max_input_bytes": assignment["max_input_bytes"],
            "cost_unit": assignment["cost_unit"],
            "max_cost_units": assignment["max_cost_units"],
            "max_concurrent_executions": 4,
        }
        return self.backend(runtime).execute_idempotent(
            assignment, model_input, execute_request_sha256=digest,
            budgets=budgets,
        )

    def test_finnish_full_path_is_source_blind_and_environment_clean(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        os.environ["SYNTHETIC_AMBIENT_SECRET"] = "must-not-cross"
        self.addCleanup(os.environ.pop, "SYNTHETIC_AMBIENT_SECRET", None)
        runtime = RUNTIME.open_subagent_facility_runtime(
            self.configuration([route]), initialize_ledger=True,
        )
        try:
            result = self.direct(runtime, task, control)
        finally:
            runtime.close()
        self.assertEqual(result["execution"]["response"]["locale"], "fi-FI")
        captures = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(len(captures), 2)
        preflight, execution = captures
        self.assertEqual(preflight["request"]["operation"], "preflight")
        serialized_preflight = json.dumps(
            preflight["request"], ensure_ascii=False,
        )
        for forbidden in (
                "model_input", "budgets", "candidate", "source_text",
                "conversation_history", "provider_execution_key"):
            self.assertNotIn(forbidden, serialized_preflight)
        request = execution["request"]
        serialized = json.dumps(request, ensure_ascii=False)
        self.assertNotIn("source", request["model_input"]["input"])
        self.assertNotIn("conversation_history", serialized)
        self.assertNotIn("must-not-cross", serialized)
        self.assertEqual(execution["environment"], {
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
            "BLUN_SUBAGENT_COMMAND_PROTOCOL": "2",
        })
        self.assertEqual(preflight["environment"], execution["environment"])
        self.assertEqual(execution["cwd"], str(self.root))
        self.assertNotIn("SYNTHETIC_AMBIENT_SECRET", execution["environment"])
        self.assertGreaterEqual(len(execution["fds"]), 5)
        self.assertLessEqual(len(execution["fds"]), 8)

    def test_maltese_native_then_fidelity_have_separate_reduced_inputs(self):
        routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        runtime = RUNTIME.open_subagent_facility_runtime(
            self.configuration(routes), initialize_ledger=True,
        )
        fixture = EXEC_TEST.ExecutorTests()
        fixture.temporary = self.temporary
        try:
            executor = fixture.executor(routes, backend=self.backend(runtime))
            host = fixture.review_host(routes, fixture.launcher(executor))
            result = BASE.HostSubagentTests().execute(
                BASE.adapter(fixture.review_client(host)), "mt-MT",
            )
        finally:
            runtime.close()
        self.assertTrue(result["release_required"])
        captures = json.loads(self.capture.read_text(encoding="utf-8"))
        preflight = [
            item for item in captures
            if item["request"]["operation"] == "preflight"
        ]
        executions = [
            item for item in captures
            if item["request"]["operation"] == "execute"
        ]
        self.assertEqual(len(preflight), 1)
        self.assertEqual(
            [item["request"]["assignment"]["phase"] for item in executions],
            ["target_native", "source_fidelity"],
        )
        self.assertNotIn(
            "source", executions[0]["request"]["model_input"]["input"],
        )
        self.assertIn(
            "source", executions[1]["request"]["model_input"]["input"],
        )

    def test_restart_reconciles_completed_command_without_second_start(self):
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
        upstream = "c" * 64
        request, _body, _headers = self.backend(runtime)._request(
            "execute", assignment, execute_request_sha256=upstream,
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
            result = self.backend(restarted).reconcile(
                assignment, execute_request_sha256=upstream,
            )
        finally:
            restarted.close()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(json.loads(self.state.read_text())["starts"], 1)
        captures = json.loads(self.capture.read_text())
        self.assertNotIn("model_input", captures[-1]["request"])
        self.assertNotIn("budgets", captures[-1]["request"])

    def test_missing_reconcile_is_read_only_and_never_starts(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        runtime = RUNTIME.open_subagent_facility_runtime(
            self.configuration([route]), initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        try:
            result = self.backend(runtime).reconcile(
                assignment, execute_request_sha256="d" * 64,
            )
        finally:
            runtime.close()
        self.assertEqual(result["status"], "not_started")
        self.assertFalse(self.state.exists())
        captures = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["request"]["operation"] for item in captures],
            ["preflight"],
        )

    def test_reserved_unknown_reconcile_reaches_command_without_starting(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        runtime = RUNTIME.open_subagent_facility_runtime(
            self.configuration([route]), initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = {
            "deadline_seconds": assignment["deadline_seconds"],
            "max_output_tokens": assignment["max_output_tokens"],
            "max_input_bytes": assignment["max_input_bytes"],
            "cost_unit": assignment["cost_unit"],
            "max_cost_units": assignment["max_cost_units"],
            "max_concurrent_executions": 4,
        }
        upstream = "f" * 64
        request, _body, _headers = self.backend(runtime)._request(
            "execute", assignment, execute_request_sha256=upstream,
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
        runtime.facility.ledger.quarantine(reservation)
        try:
            result = self.backend(runtime).reconcile(
                assignment, execute_request_sha256=upstream,
            )
        finally:
            runtime.close()
        self.assertEqual(result["status"], "not_started")
        self.assertFalse(self.state.exists())
        captures = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(len(captures), 2)
        self.assertEqual(captures[-1]["request"]["operation"], "reconcile")
        self.assertNotIn("model_input", captures[-1]["request"])
        self.assertNotIn("budgets", captures[-1]["request"])

    def test_executable_permissions_digest_and_post_preflight_drift_block(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        self.executable.chmod(0o755)
        with self.assertRaisesRegex(
                RUNTIME.SubagentFacilityRuntimeError, "driver factory failed"):
            RUNTIME.open_subagent_facility_runtime(
                self.configuration([route]), initialize_ledger=True,
            )
        self.executable.chmod(0o700)
        self.ledger.unlink(missing_ok=True)
        config = self.configuration([route])
        runtime = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        self.executable.write_text(FIXTURE + "\n# changed\n", encoding="utf-8")
        self.executable.chmod(0o700)
        try:
            with self.assertRaises(BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed):
                self.direct(runtime, task, control)
            self.assertEqual(runtime.facility.ledger.count_active(), 1)
        finally:
            runtime.close()

    def test_linked_executable_and_working_directory_are_rejected(self):
        settings = json.loads(self.driver_file.read_text())["settings"]
        placeholder = dict(settings)
        placeholder["deployment_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(
                COMMAND.CommandSubagentDriverFailed,
                "deployment_manifest_invalid"):
            COMMAND.build_driver(placeholder)

        executable_link = self.root / "linked-command"
        executable_link.symlink_to(self.executable)
        settings["executable"] = str(executable_link)
        with self.assertRaises(COMMAND.CommandSubagentDriverFailed):
            COMMAND.build_driver(settings)

        executable_link.unlink()
        executable_link.hardlink_to(self.executable)
        settings["executable"] = str(self.executable)
        with self.assertRaises(COMMAND.CommandSubagentDriverFailed):
            COMMAND.build_driver(settings)
        executable_link.unlink()

        directory_link = self.root.parent / (self.root.name + "-linked")
        directory_link.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(directory_link.unlink, missing_ok=True)
        settings["working_directory"] = str(directory_link)
        with self.assertRaises(COMMAND.CommandSubagentDriverFailed):
            COMMAND.build_driver(settings)

    def test_sealed_snapshot_executes_hashed_bytes_after_in_place_rewrite(self):
        settings = json.loads(self.driver_file.read_text())["settings"]
        original = self.executable.read_bytes()
        driver = COMMAND.build_driver(settings)
        marker = self.root / "mutated-command-ran"
        prefix = (
            "#!/bin/sh\n: > " + str(marker) + "\nexit 99\n#"
        ).encode("utf-8")
        self.assertLess(len(prefix), len(original))
        replacement = prefix + b"x" * (len(original) - len(prefix))
        self.executable.write_bytes(replacement)
        self.executable.chmod(0o700)
        try:
            with mock.patch.object(
                    COMMAND, "_inside_isolated_worker", return_value=True):
                result = driver.reconcile(
                    {"deadline_seconds": 2},
                    provider_execution_key="1" * 64,
                    provider_request_sha256="2" * 64,
                )
            self.assertEqual(result["status"], "not_started")
            self.assertFalse(marker.exists())
        finally:
            driver.close()

    def test_driver_configuration_digest_changes_provider_namespace(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        config = self.configuration([route])
        first = RUNTIME.open_subagent_facility_runtime(
            config, initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = {
            "deadline_seconds": assignment["deadline_seconds"],
            "max_output_tokens": assignment["max_output_tokens"],
            "max_input_bytes": assignment["max_input_bytes"],
            "cost_unit": assignment["cost_unit"],
            "max_cost_units": assignment["max_cost_units"],
            "max_concurrent_executions": 4,
        }
        request, _body, _headers = self.backend(first)._request(
            "execute", assignment, execute_request_sha256="e" * 64,
            model_input=model_input, budgets=budgets,
        )
        first_identity = first.facility._provider_identity(request)
        first.close()

        document = json.loads(self.driver_file.read_text())
        document["settings"]["max_response_bytes"] += 1
        self.driver_file = self.write_json("driver-generation-2.json", document)
        self.ledger = self.root / "facility-generation-2.sqlite3"
        second = RUNTIME.open_subagent_facility_runtime(
            self.configuration([route]), initialize_ledger=True,
        )
        try:
            second_identity = second.facility._provider_identity(request)
        finally:
            second.close()
        self.assertNotEqual(first_identity, second_identity)

    def test_terminal_rejection_and_wrong_error_binding_fail_closed(self):
        for mode in ("terminal", "terminal_wrong_binding"):
            with self.subTest(mode=mode):
                case = self.root / mode
                case.mkdir(mode=0o700)
                self.state = case / "state.json"
                self.capture = case / "capture.json"
                self.mode = case / "mode"
                self.mode.write_text(mode, encoding="ascii")
                self.mode.chmod(0o600)
                document = json.loads(self.driver_file.read_text())
                document["settings"]["arguments"] = [
                    str(self.state), str(self.capture), str(self.mode),
                ]
                self.driver_file = self.write_json(
                    f"driver-{mode}.json", document,
                )
                self.ledger = case / "facility.sqlite3"
                task, control = HOST_TEST.response_request("fi-FI")
                route = HOST_TEST.response_route(task)
                runtime = RUNTIME.open_subagent_facility_runtime(
                    self.configuration([route]), initialize_ledger=True,
                )
                try:
                    with self.assertRaises(
                            BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed) as blocked:
                        self.direct(runtime, task, control)
                    self.assertEqual(
                        blocked.exception.retryable,
                        mode == "terminal_wrong_binding",
                    )
                    self.assertEqual(runtime.facility.ledger.count_active(), 1)
                finally:
                    runtime.close()

    def test_wrong_binding_identity_and_usage_all_fail_closed(self):
        for index, mode in enumerate(("wrong_binding", "wrong_identity", "wrong_usage")):
            with self.subTest(mode=mode):
                case = self.root / mode
                case.mkdir(mode=0o700)
                self.state = case / "state.json"
                self.capture = case / "capture.json"
                self.mode = case / "mode"
                self.mode.write_text(mode, encoding="ascii")
                self.mode.chmod(0o600)
                document = json.loads(self.driver_file.read_text())
                document["settings"]["arguments"] = [
                    str(self.state), str(self.capture), str(self.mode),
                ]
                self.driver_file = self.write_json(f"driver-{mode}.json", document)
                self.ledger = case / "facility.sqlite3"
                task, control = HOST_TEST.response_request("fi-FI")
                route = HOST_TEST.response_route(task)
                runtime = RUNTIME.open_subagent_facility_runtime(
                    self.configuration([route]), initialize_ledger=True,
                )
                try:
                    with self.assertRaises(
                            BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed):
                        self.direct(runtime, task, control, digest=f"{index + 4}" * 64)
                    self.assertEqual(runtime.facility.ledger.count_active(), 1)
                finally:
                    runtime.close()

    def test_process_and_protocol_failures_are_ambiguous_not_approved(self):
        for mode in ("nonzero", "oversize", "malformed", "hang"):
            with self.subTest(mode=mode):
                case = self.root / mode
                case.mkdir(mode=0o700)
                self.state = case / "state.json"
                self.capture = case / "capture.json"
                self.mode = case / "mode"
                self.mode.write_text(mode, encoding="ascii")
                self.mode.chmod(0o600)
                document = json.loads(self.driver_file.read_text())
                document["settings"]["arguments"] = [
                    str(self.state), str(self.capture), str(self.mode),
                ]
                document["settings"]["max_response_bytes"] = 4096
                self.driver_file = self.write_json(f"driver-{mode}.json", document)
                self.ledger = case / "facility.sqlite3"
                task, control = HOST_TEST.response_request("fi-FI")
                if mode == "hang":
                    control["timeout_seconds"] = 1
                route = HOST_TEST.response_route(task)
                runtime = RUNTIME.open_subagent_facility_runtime(
                    self.configuration([route]), initialize_ledger=True,
                )
                try:
                    with self.assertRaises(
                            BACKEND_TEST.BACKEND.SubagentHTTPBackendFailed):
                        self.direct(runtime, task, control)
                    self.assertEqual(runtime.facility.ledger.count_active(), 1)
                finally:
                    runtime.close()

    def test_active_preflight_failures_block_before_ledger_or_model_start(self):
        cases = (
            "preflight_terminal", "preflight_terminal_wrong_binding",
            "preflight_nonzero", "preflight_oversize", "preflight_malformed",
            "preflight_hang", "preflight_wrong_preflight_challenge",
            "preflight_not_ready", "preflight_wrong_route_digest",
            "preflight_wrong_capabilities", "preflight_wrong_manifest",
        )
        for mode in cases:
            with self.subTest(mode=mode):
                case = self.root / mode
                case.mkdir(mode=0o700)
                self.state = case / "state.json"
                self.capture = case / "capture.json"
                self.mode = case / "mode"
                self.mode.write_text(mode, encoding="ascii")
                self.mode.chmod(0o600)
                document = json.loads(self.driver_file.read_text())
                document["settings"]["arguments"] = [
                    str(self.state), str(self.capture), str(self.mode),
                ]
                document["settings"]["max_response_bytes"] = 4096
                self.driver_file = self.write_json(
                    f"driver-{mode}.json", document,
                )
                self.ledger = case / "facility.sqlite3"
                task, _control = HOST_TEST.response_request("fi-FI")
                route = HOST_TEST.response_route(task)
                with mock.patch.object(
                        RUNTIME, "PREFLIGHT_DEADLINE_SECONDS", 1):
                    with self.assertRaisesRegex(
                            RUNTIME.SubagentFacilityRuntimeError,
                            "host-subagent driver preflight"):
                        RUNTIME.open_subagent_facility_runtime(
                            self.configuration([route]),
                            initialize_ledger=True,
                        )
                self.assertFalse(self.ledger.exists())
                self.assertFalse(self.state.exists())

    def test_preflight_binds_manifest_and_changes_provider_namespace(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        first = RUNTIME.open_subagent_facility_runtime(
            self.configuration([route]), initialize_ledger=True,
        )
        assignment = vars(
            HOST_TEST.HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST_TEST.HOST.ReviewHostApplication._model_task(task)
        budgets = {
            "deadline_seconds": assignment["deadline_seconds"],
            "max_output_tokens": assignment["max_output_tokens"],
            "max_input_bytes": assignment["max_input_bytes"],
            "cost_unit": assignment["cost_unit"],
            "max_cost_units": assignment["max_cost_units"],
            "max_concurrent_executions": 4,
        }
        request, _body, _headers = self.backend(first)._request(
            "execute", assignment, execute_request_sha256="7" * 64,
            model_input=model_input, budgets=budgets,
        )
        first_identity = first.facility._provider_identity(request)
        first.close()

        document = json.loads(self.driver_file.read_text())
        document["settings"]["deployment_manifest_sha256"] = "a" * 64
        self.driver_file = self.write_json("driver-manifest-2.json", document)
        self.ledger = self.root / "facility-manifest-2.sqlite3"
        second = RUNTIME.open_subagent_facility_runtime(
            self.configuration([route]), initialize_ledger=True,
        )
        try:
            second_identity = second.facility._provider_identity(request)
            captures = json.loads(self.capture.read_text(encoding="utf-8"))
            latest = captures[-1]["request"]
            self.assertEqual(latest["operation"], "preflight")
            self.assertEqual(
                latest["deployment_manifest_sha256"], "a" * 64,
            )
        finally:
            second.close()
        self.assertNotEqual(first_identity, second_identity)

    def test_direct_command_driver_use_requires_isolated_runtime_worker(self):
        settings = json.loads(self.driver_file.read_text())["settings"]
        driver = COMMAND.build_driver(settings)
        try:
            with mock.patch.dict(
                    os.environ, {"BLUN_SUBAGENT_DRIVER_WORKER": "1"}):
                with mock.patch.object(
                        COMMAND.os, "getpgrp", return_value=os.getpid() + 1):
                    with self.assertRaisesRegex(
                            COMMAND.CommandSubagentDriverFailed,
                            "isolated_worker_required"):
                        driver.reconcile(
                            {"deadline_seconds": 2},
                            provider_execution_key="1" * 64,
                            provider_request_sha256="2" * 64,
                        )
        finally:
            driver.close()


if __name__ == "__main__":
    unittest.main()
