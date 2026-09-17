"""Synthetic HTTPS-backend fixtures; not native-language quality evidence."""

from __future__ import annotations

import hashlib
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import test_website_localization_subagent_executor as EXEC_TEST
import test_website_localization_subagent_executor_runtime as RUNTIME_TEST
import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagents as BASE


BACKEND = BASE.load(
    "test_website_localization_subagent_backend_http_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_backend_http.py",
)
HOST = HOST_TEST.HOST
HTTP = HOST_TEST.HTTP
FACILITY_TOKEN = "facility-bearer-token-with-at-least-32-characters"
DRIVER_DEPLOYMENT_SHA256 = "8" * 64
DEPLOYMENT_MANIFEST_SHA256 = "9" * 64
ROUTE_REQUIREMENTS_SHA256 = "a" * 64
READINESS_POLICY_SHA256 = "b" * 64
FACILITY_LEDGER_INSTANCE_ID = "c" * 64
ROUTES_COUNT = 2


class FacilityTransport:
    """Synthetic durable facility behind the actual HTTP backend adapter."""

    def __init__(self):
        self.calls = []
        self.executions, self.usage, self.execute_digests = {}, {}, {}
        self.physical_starts = 0
        self.lose_first_execute_reply = False
        self.mutate_reply = None
        self.status = "completed"
        self.readiness_status = "ready"
        self.mutate_readiness = None

    @staticmethod
    def _result(status, body):
        raw = json.dumps(
            body, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return BACKEND.HTTPResult(
            status,
            (("Content-Type", "application/json; charset=utf-8"),
             ("Content-Length", str(len(raw)))),
            raw,
        )

    @staticmethod
    def _execution(assignment, model_input):
        return {
            "response": EXEC_TEST.FixtureBackend._response(model_input),
            "execution_key": assignment["execution_key"],
            "phase": assignment["phase"],
            "reviewer_role": assignment["reviewer_role"],
            "agent_id": assignment["reviewer_agent_id"],
            "session_id": assignment["reviewer_session_id"],
            "model_id": assignment["model_id"],
            "model_version": assignment["model_version"],
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
        }

    def post(self, _url, headers, body, *, timeout):
        request = json.loads(body)
        self.calls.append((request, dict(headers), timeout, bytes(body)))
        if request.get("schema") == BACKEND.READINESS_REQUEST_SCHEMA:
            ready = self.readiness_status == "ready"
            reply = {
                "schema": BACKEND.READINESS_RESPONSE_SCHEMA,
                "backend_id": request["backend_id"],
                "backend_version": request["backend_version"],
                "facility_id": request["facility_id"],
                "facility_version": request["facility_version"],
                "challenge": request["challenge"],
                "request_sha256": request["request_sha256"],
                "ready": ready, "reason": self.readiness_status,
                "probe_generation": 1, "failure_generation": 0,
                "routes_checked": ROUTES_COUNT,
                "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                "readiness_policy_sha256": READINESS_POLICY_SHA256,
                "facility_ledger_instance_id": FACILITY_LEDGER_INSTANCE_ID,
            }
            if self.mutate_readiness:
                self.mutate_readiness(reply)
            return self._result(200 if ready else 503, reply)
        key, operation = (
            request["assignment"]["execution_key"], request["operation"],
        )
        if operation == "execute":
            digest = hashlib.sha256(body).hexdigest()
            prior = self.execute_digests.get(key)
            if prior is not None and prior != digest:
                return self._result(409, {})
            if prior is None:
                self.execute_digests[key] = digest
                self.physical_starts += 1
                assignment, model_input = request["assignment"], request["model_input"]
                self.executions[key] = self._execution(assignment, model_input)
                self.usage[key] = {
                    "execute_request_sha256": request["execute_request_sha256"],
                    "cost_unit": request["budgets"]["cost_unit"],
                    "cost_units": 7,
                    "input_bytes": len(EXEC_TEST.EXECUTOR._raw(model_input)),
                    "output_tokens": 1,
                }
            if self.lose_first_execute_reply:
                self.lose_first_execute_reply = False
                raise OSError("synthetic lost facility response")
            status = self.status
        else:
            status = "completed" if key in self.executions else "not_started"
        reply = {
            "schema": BACKEND.RESPONSE_SCHEMA,
            "operation": operation,
            "backend_id": request["backend_id"],
            "backend_version": request["backend_version"],
            "facility_id": request["facility_id"],
            "facility_version": request["facility_version"],
            "execution_key": key,
            "execute_request_sha256": request["execute_request_sha256"],
            "request_sha256": request["request_sha256"],
            "readiness_binding": request["readiness_binding"],
            "status": status,
            "execution": self.executions.get(key) if status == "completed" else None,
            "usage": self.usage.get(key) if status == "completed" else None,
        }
        if self.mutate_reply:
            self.mutate_reply(reply)
        return self._result(202 if status in BACKEND.ACTIVE else 200, reply)


class HTTPBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.facility = FacilityTransport()
        self.backend = BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + FACILITY_TOKEN},
            backend_id="production-host-subagents", backend_version="backend-1",
            facility_id="host-subagent-facility",
            facility_version="facility-1", transport=self.facility,
            max_output_tokens=4096, allow_loopback_http=True,
            expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
            expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
            expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
            expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
            expected_facility_ledger_instance_id=FACILITY_LEDGER_INSTANCE_ID,
            expected_routes_count=ROUTES_COUNT,
        )
        self.fixture = EXEC_TEST.ExecutorTests()
        self.fixture.temporary = self.temporary
        self.fixture.backend = self.backend

    def chain(self, routes):
        executor = self.fixture.executor(routes, backend=self.backend)
        host = self.fixture.review_host(
            routes, self.fixture.launcher(executor),
        )
        return self.fixture.review_client(host)

    @staticmethod
    def direct_request(locale="fi-FI"):
        task, control = HOST_TEST.response_request(locale)
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        return assignment, HOST.ReviewHostApplication._model_task(task), {
            "deadline_seconds": assignment.deadline_seconds,
            "max_output_tokens": assignment.max_output_tokens,
            "max_input_bytes": assignment.max_input_bytes,
            "cost_unit": assignment.cost_unit,
            "max_cost_units": assignment.max_cost_units,
            "max_concurrent_executions": 4,
        }

    def test_readiness_is_content_free_bound_and_fail_closed(self):
        reply = self.backend.readiness()
        self.assertTrue(reply["ready"])
        request, headers, timeout, body = self.facility.calls[-1]
        self.assertEqual(set(request), {
            "schema", "backend_id", "backend_version", "facility_id",
            "facility_version", "challenge", "request_sha256",
        })
        serialized = body.decode("utf-8")
        for forbidden in ("assignment", "model_input", "source", "candidate"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            headers["X-Subagent-Readiness-Challenge"], request["challenge"],
        )
        self.assertEqual(timeout, 60.0)
        self.assertEqual(self.facility.physical_starts, 0)

        self.facility.readiness_status = "preflight_failed"
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
            self.backend.readiness()
        self.assertEqual(blocked.exception.code,
                         "backend_http.readiness_blocked")
        self.assertTrue(blocked.exception.retryable)
        self.assertEqual(self.facility.physical_starts, 0)

        self.facility.readiness_status = "ready"
        self.facility.mutate_readiness = lambda value: value.update(
            route_requirements_sha256="0" * 64,
        )
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as forged:
            self.backend.readiness()
        self.assertEqual(forged.exception.code,
                         "backend_http.readiness_binding")
        self.assertTrue(forged.exception.retryable)

    def test_finnish_response_crosses_complete_chain_without_source_or_secret(self):
        task, control = HOST_TEST.response_request("fi-FI")
        result = self.chain([HOST_TEST.response_route(task)]).run_isolated(
            task, control=control,
        )
        self.assertEqual(result["response"]["status"], "PASS")
        self.assertEqual(self.facility.physical_starts, 1)
        request, headers, timeout, body = self.facility.calls[0]
        serialized = body.decode("utf-8")
        self.assertEqual(request["operation"], "execute")
        self.assertEqual(request["isolation"], {
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
        })
        self.assertNotIn("source", request["model_input"]["input"])
        self.assertNotIn("history", serialized)
        self.assertNotIn("creator_id", serialized)
        self.assertNotIn(FACILITY_TOKEN, serialized)
        self.assertEqual(headers["Authorization"], "Bearer " + FACILITY_TOKEN)
        self.assertEqual(headers["Idempotency-Key"], control["execution_key"])
        self.assertEqual(timeout, 60.0)

    def test_maltese_translation_uses_two_ordered_isolated_facility_jobs(self):
        routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        result = BASE.HostSubagentTests().execute(
            BASE.adapter(self.chain(routes)), "mt-MT",
        )
        self.assertTrue(result["release_required"])
        execute = [item[0] for item in self.facility.calls
                   if item[0]["operation"] == "execute"]
        self.assertEqual([item["assignment"]["phase"] for item in execute],
                         ["target_native", "source_fidelity"])
        self.assertNotIn("source", execute[0]["model_input"]["input"])
        self.assertEqual(execute[1]["model_input"]["input"]["source"]["text"],
                         "Build your business with BLUN.")
        self.assertNotEqual(execute[0]["assignment"]["reviewer_agent_id"],
                            execute[1]["assignment"]["reviewer_agent_id"])

    def test_lost_facility_reply_reconciles_without_second_physical_start(self):
        task, control = HOST_TEST.response_request("fi-FI")
        self.facility.lose_first_execute_reply = True
        client = self.chain([HOST_TEST.response_route(task)])
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as lost:
            client.run_isolated(task, control=control)
        self.assertTrue(lost.exception.retryable)
        recovered = client.run_isolated(task, control=control)
        self.assertEqual(recovered["response"]["status"], "PASS")
        self.assertEqual(self.facility.physical_starts, 1)
        self.assertEqual([item[0]["operation"] for item in self.facility.calls],
                         ["execute", "reconcile"])
        reconcile = self.facility.calls[1][0]
        self.assertNotIn("model_input", reconcile)
        self.assertNotIn("budgets", reconcile)
        self.assertEqual(
            reconcile["execute_request_sha256"],
            self.facility.calls[0][0]["execute_request_sha256"],
        )

    def test_redirect_retry_and_changed_replay_classify_fail_closed(self):
        assignment, model_input, budgets = self.direct_request()

        class StatusTransport:
            def __init__(self, status):
                self.status = status

            def post(self, _url, _headers, _body, *, timeout):
                return BACKEND.HTTPResult(self.status, (), b"")

        original = self.backend.transport
        try:
            for status, code, retryable in (
                    (302, "backend_http.redirect", False),
                    (429, "backend_http.status", True)):
                with self.subTest(status=status):
                    self.backend.transport = StatusTransport(status)
                    with self.assertRaises(
                            BACKEND.SubagentHTTPBackendFailed) as blocked:
                        self.backend.execute_idempotent(
                            vars(assignment), model_input,
                            execute_request_sha256="2" * 64,
                            budgets=budgets,
                        )
                    self.assertEqual(blocked.exception.code, code)
                    self.assertIs(blocked.exception.retryable, retryable)
        finally:
            self.backend.transport = original

        first = self.backend.execute_idempotent(
            vars(assignment), model_input,
            execute_request_sha256="2" * 64, budgets=budgets,
        )
        self.assertEqual(first["status"], "completed")
        changed = json.loads(json.dumps(model_input))
        changed["input"]["candidate"] += " Muutos."
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as conflict:
            self.backend.execute_idempotent(
                vars(assignment), changed,
                execute_request_sha256="2" * 64, budgets=budgets,
            )
        self.assertEqual(conflict.exception.code,
                         "backend_http.idempotency_conflict")
        self.assertFalse(conflict.exception.retryable)
        self.assertEqual(self.facility.physical_starts, 1)

    def test_wrong_facility_digest_identity_and_usage_fail_closed(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        mutations = (
            lambda reply: reply.update(facility_id="foreign-facility"),
            lambda reply: reply.update(execute_request_sha256="0" * 64),
            lambda reply: reply["execution"].update(agent_id="writer"),
            lambda reply: reply["usage"].update(cost_units=10**9),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                facility = FacilityTransport()
                facility.mutate_reply = mutation
                backend = BACKEND.HTTPSExecutionBackend(
                    "http://127.0.0.1/v1/isolated-review-executions",
                    lambda: {"Authorization": "Bearer " + FACILITY_TOKEN},
                    backend_id="production-host-subagents",
                    backend_version="backend-1",
                    facility_id="host-subagent-facility",
                    facility_version="facility-1", transport=facility,
                    max_output_tokens=4096, allow_loopback_http=True,
                    expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
                    expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
                    expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
                    expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
                    expected_facility_ledger_instance_id=(
                        FACILITY_LEDGER_INSTANCE_ID
                    ),
                    expected_routes_count=ROUTES_COUNT,
                )
                ledger = EXEC_TEST.EXECUTOR.SQLiteExecutionLedger(
                    Path(self.temporary.name) / f"bad-{index}.sqlite3",
                    max_concurrent_executions=4,
                )
                executor = self.fixture.executor(
                    [route], backend=backend, ledger=ledger,
                )
                host = self.fixture.review_host(
                    [route], self.fixture.launcher(executor),
                )
                with self.assertRaises(HTTP.HTTPReviewHostFailed) as blocked:
                    self.fixture.review_client(host).run_isolated(
                        task, control=control,
                    )
                self.assertFalse(blocked.exception.retryable)
                self.assertEqual(ledger.count_active(), 1)

    def test_native_source_injection_and_unsafe_endpoint_block_before_transport(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        model_input["input"]["quality_profile"]["conversation_history"] = [
            "hidden source",
        ]
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
            self.backend.execute_idempotent(
                vars(assignment), model_input,
                execute_request_sha256="1" * 64,
                budgets={
                    "deadline_seconds": assignment.deadline_seconds,
                    "max_output_tokens": assignment.max_output_tokens,
                    "max_input_bytes": assignment.max_input_bytes,
                    "cost_unit": assignment.cost_unit,
                    "max_cost_units": assignment.max_cost_units,
                    "max_concurrent_executions": 4,
                },
            )
        self.assertEqual(blocked.exception.code,
                         "backend_http.native_source_isolation")
        self.assertEqual(self.facility.calls, [])
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            BACKEND.HTTPSExecutionBackend(
                "http://facility.example/v1/isolated-review-executions",
                lambda: {"Authorization": "Bearer " + FACILITY_TOKEN},
                backend_id="production-host-subagents",
                backend_version="backend-1",
                facility_id="host-subagent-facility",
                facility_version="facility-1",
                expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
                expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
                expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
                expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
                expected_facility_ledger_instance_id=(
                    FACILITY_LEDGER_INSTANCE_ID
                ),
                expected_routes_count=ROUTES_COUNT,
            )

    def test_protected_factory_binds_token_digest_and_permissions(self):
        root = Path(self.temporary.name).resolve()
        if os.name != "nt":
            root.chmod(0o700)
        token_file = root / "facility.token"
        token_file.write_text(FACILITY_TOKEN, encoding="ascii")
        if os.name != "nt":
            token_file.chmod(0o600)
        settings = {
            "schema": BACKEND.SETTINGS_SCHEMA,
            "backend_id": "production-host-subagents",
            "backend_version": "backend-1",
            "facility_id": "host-subagent-facility",
            "facility_version": "facility-1",
            "endpoint": "http://127.0.0.1/v1/isolated-review-executions",
            "authentication": {
                "scheme": "bearer", "token_file": str(token_file),
                "token_sha256": hashlib.sha256(
                    FACILITY_TOKEN.encode("ascii"),
                ).hexdigest(),
            },
            "readiness": {
                "facility_ledger_instance_id": FACILITY_LEDGER_INSTANCE_ID,
                "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                "readiness_policy_sha256": READINESS_POLICY_SHA256,
                "routes_count": ROUTES_COUNT,
            },
            "request_timeout_seconds": 60,
            "max_input_bytes": 2_000_000,
            "max_output_tokens": 4096,
            "cost_unit": "deployment-cost-unit",
            "max_cost_units": 100_000,
            "allow_loopback_http": True,
        }
        backend = BACKEND.build_backend(settings)
        self.assertEqual(backend._auth()["Authorization"],
                         "Bearer " + FACILITY_TOKEN)
        if os.name != "nt":
            token_file.chmod(0o644)
            with self.assertRaises(BACKEND.SubagentHTTPBackendFailed):
                backend._auth()

    def test_executor_runtime_loads_digest_pinned_standard_factory(self):
        root = Path(self.temporary.name).resolve()
        if os.name != "nt":
            root.chmod(0o700)
        factory_path = root / "website_localization_subagent_backend_http.py"
        factory_path.write_bytes((
            BASE.ROOT / "integrations"
            / "website_localization_subagent_backend_http.py"
        ).read_bytes())
        token_file = root / "facility-runtime.token"
        token_file.write_text(FACILITY_TOKEN, encoding="ascii")
        if os.name != "nt":
            factory_path.chmod(0o600)
            token_file.chmod(0o600)
        settings = {
            "schema": BACKEND.SETTINGS_SCHEMA,
            "backend_id": "production-host-subagents",
            "backend_version": "backend-1",
            "facility_id": "host-subagent-facility",
            "facility_version": "facility-1",
            "endpoint": "http://127.0.0.1/v1/isolated-review-executions",
            "authentication": {
                "scheme": "bearer", "token_file": str(token_file),
                "token_sha256": hashlib.sha256(
                    FACILITY_TOKEN.encode("ascii"),
                ).hexdigest(),
            },
            "readiness": {
                "facility_ledger_instance_id": FACILITY_LEDGER_INSTANCE_ID,
                "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                "readiness_policy_sha256": READINESS_POLICY_SHA256,
                "routes_count": ROUTES_COUNT,
            },
            "request_timeout_seconds": 60,
            "max_input_bytes": 2_000_000,
            "max_output_tokens": 4096,
            "cost_unit": "deployment-cost-unit",
            "max_cost_units": 100_000,
            "allow_loopback_http": True,
        }
        config = {
            "factory_file": str(factory_path),
            "factory_callable": "build_backend",
            "factory_sha256": hashlib.sha256(factory_path.read_bytes()).hexdigest(),
            "backend_id": "production-host-subagents",
            "backend_version": "backend-1",
        }
        backend = RUNTIME_TEST.RUNTIME._build_backend(
            config, {"settings": settings},
        )
        try:
            self.assertEqual(backend.backend_id, "production-host-subagents")
            self.assertEqual(backend.backend_version, "backend-1")
            self.assertEqual(
                backend._backend._auth()["Authorization"],
                "Bearer " + FACILITY_TOKEN,
            )
        finally:
            backend.close()

    def test_runtime_preflight_rejects_bad_token_before_ledger_or_network(self):
        root = Path(self.temporary.name).resolve()
        if os.name != "nt":
            root.chmod(0o700)
        factory_path = root / "website_localization_subagent_backend_http.py"
        factory_path.write_bytes((
            BASE.ROOT / "integrations"
            / "website_localization_subagent_backend_http.py"
        ).read_bytes())
        if os.name != "nt":
            factory_path.chmod(0o600)
        executor_token = root / "executor.token"
        executor_token.write_text(EXEC_TEST.EXECUTOR_TOKEN, encoding="ascii")
        if os.name != "nt":
            executor_token.chmod(0o600)
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)

        def route_dict():
            return {
                "route_id": route.route_id, "phase": route.phase,
                "reviewer_role": route.reviewer_role,
                "reviewer_agent_id": route.reviewer_agent_id,
                "model_id": route.model_id,
                "model_version": route.model_version,
                "host_policy_version": route.host_policy_version,
                "target_locale": route.target_locale,
                "content_type": route.content_type,
                "task_policy_sha256": route.task_policy_sha256,
            }

        def write_json(path, value):
            path.write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8",
            )
            if os.name != "nt":
                path.chmod(0o600)

        cases = ("missing", "wrong-digest") + (("unsafe-mode",)
                 if os.name != "nt" else ())
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                token_file = root / f"facility-{index}.token"
                if case != "missing":
                    token_file.write_text(FACILITY_TOKEN, encoding="ascii")
                    if os.name != "nt":
                        token_file.chmod(0o644 if case == "unsafe-mode" else 0o600)
                token_sha256 = hashlib.sha256(
                    FACILITY_TOKEN.encode("ascii"),
                ).hexdigest()
                if case == "wrong-digest":
                    token_sha256 = "0" * 64
                backend_file = root / f"backend-{index}.json"
                write_json(backend_file, {
                    "schema": RUNTIME_TEST.RUNTIME.BACKEND_SCHEMA,
                    "backend_id": "production-host-subagents",
                    "backend_version": "backend-1",
                    "settings": {
                        "schema": BACKEND.SETTINGS_SCHEMA,
                        "backend_id": "production-host-subagents",
                        "backend_version": "backend-1",
                        "facility_id": "host-subagent-facility",
                        "facility_version": "facility-1",
                        "endpoint": "http://127.0.0.1/v1/isolated-review-executions",
                        "authentication": {
                            "scheme": "bearer", "token_file": str(token_file),
                            "token_sha256": token_sha256,
                        },
                        "readiness": {
                            "facility_ledger_instance_id": (
                                FACILITY_LEDGER_INSTANCE_ID
                            ),
                            "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                            "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                            "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                            "readiness_policy_sha256": READINESS_POLICY_SHA256,
                            "routes_count": ROUTES_COUNT,
                        },
                        "request_timeout_seconds": 60,
                        "max_input_bytes": 2_000_000,
                        "max_output_tokens": 4096,
                        "cost_unit": "deployment-cost-unit",
                        "max_cost_units": 100_000,
                        "allow_loopback_http": True,
                    },
                })
                ledger = root / f"invalid-{index}.sqlite3"
                config_file = root / f"executor-{index}.json"
                write_json(config_file, {
                    "schema": RUNTIME_TEST.RUNTIME.CONFIG_SCHEMA,
                    "executor_id": "executor-1",
                    "launcher_id": "deployment-review-launcher",
                    "launcher_version": "launcher-1",
                    "allow_loopback_http": True,
                    "authentication": {
                        "scheme": "bearer", "token_file": str(executor_token),
                    },
                    "ledger": {
                        "path": str(ledger), "max_concurrent_executions": 4,
                    },
                    "backend": {
                        "factory_file": str(factory_path),
                        "factory_callable": "build_backend",
                        "factory_sha256": hashlib.sha256(
                            factory_path.read_bytes(),
                        ).hexdigest(),
                        "backend_id": "production-host-subagents",
                        "backend_version": "backend-1",
                        "config_file": str(backend_file),
                    },
                    "routes": [route_dict()],
                })
                with self.assertRaises((RuntimeError, ValueError)):
                    RUNTIME_TEST.RUNTIME.open_subagent_executor_runtime(
                        config_file, initialize_ledger=True,
                    )
                self.assertFalse(ledger.exists())
                self.assertEqual(self.facility.calls, [])

    def test_default_transport_kills_slow_response_at_wall_timeout(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "2")
                self.end_headers()
                try:
                    self.wfile.write(b"{")
                    self.wfile.flush()
                    time.sleep(1.0)
                    self.wfile.write(b"}")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, _format, *_args):
                pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SlowHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        started = time.monotonic()
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
            BACKEND.URLTransport().post(
                f"http://127.0.0.1:{server.server_address[1]}/slow",
                {"Content-Type": "application/json"}, b"{}", timeout=0.2,
            )
        self.assertTrue(blocked.exception.retryable)
        self.assertLess(time.monotonic() - started, 0.8)


if __name__ == "__main__":
    unittest.main()
