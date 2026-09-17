"""Synthetic facility fixtures; not native-language quality evidence."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import test_website_localization_subagent_backend_http as BACKEND_TEST
import test_website_localization_subagent_executor as EXEC_TEST
import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagents as BASE


FACILITY = BASE.load(
    "test_website_localization_subagent_facility_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_facility.py",
)
BACKEND = BACKEND_TEST.BACKEND
HOST = HOST_TEST.HOST
TOKEN = BACKEND_TEST.FACILITY_TOKEN
DRIVER_DEPLOYMENT_SHA256 = "8" * 64
DEPLOYMENT_MANIFEST_SHA256 = "9" * 64
ROUTE_REQUIREMENTS_SHA256 = "a" * 64
READINESS_POLICY_SHA256 = "b" * 64
FACILITY_LEDGER_INSTANCE_ID = "c" * 64


class FixtureDriver:
    driver_id = "synthetic-host-driver"
    driver_version = "fixture-1"
    supports_atomic_idempotency = True
    supports_reconcile = True
    supports_hard_deadline = True
    supports_isolated_context = True
    supports_preflight = True
    deployment_manifest_sha256 = "9" * 64

    def __init__(self):
        self.starts, self.reconciles, self.completed = [], [], {}
        self.physical_starts = 0
        self.raise_after_completion = False
        self.mutate = None
        self.forced_status = None

    def _completed(self, assignment, model_input, provider_digest, budgets):
        actual = {
            "response": EXEC_TEST.FixtureBackend._response(model_input),
            "phase": assignment["phase"],
            "reviewer_role": assignment["reviewer_role"],
            "agent_id": assignment["reviewer_agent_id"],
            "session_id": assignment["reviewer_session_id"],
            "model_id": assignment["model_id"],
            "model_version": assignment["model_version"],
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
        }
        usage = {
            "provider_request_sha256": provider_digest,
            "cost_unit": budgets["cost_unit"], "cost_units": 7,
            "input_bytes": len(FACILITY._raw(model_input)),
            "output_tokens": 1,
        }
        result = {
            "status": "completed", "provider_execution_key": "",
            "provider_request_sha256": provider_digest,
            "actual_execution": actual, "usage": usage,
        }
        return result

    def execute_idempotent(
        self, assignment, model_input, *, provider_execution_key,
        provider_request_sha256, budgets, isolation,
    ):
        self.starts.append((
            FACILITY._copy(assignment), FACILITY._copy(model_input),
            provider_execution_key, provider_request_sha256,
            FACILITY._copy(budgets), FACILITY._copy(isolation),
        ))
        if provider_execution_key not in self.completed:
            self.physical_starts += 1
            result = self._completed(
                assignment, model_input, provider_request_sha256, budgets,
            )
            result["provider_execution_key"] = provider_execution_key
            self.completed[provider_execution_key] = result
        result = FACILITY._copy(self.completed[provider_execution_key])
        if self.forced_status is not None:
            result.update(
                status=self.forced_status, actual_execution=None, usage=None,
            )
        if self.mutate:
            self.mutate(result)
        if self.raise_after_completion:
            self.raise_after_completion = False
            raise OSError("synthetic lost driver response")
        return result

    def reconcile(
        self, assignment, *, provider_execution_key, provider_request_sha256,
    ):
        self.reconciles.append((
            FACILITY._copy(assignment), provider_execution_key,
            provider_request_sha256,
        ))
        result = self.completed.get(provider_execution_key)
        if result is None:
            return {
                "status": "not_started",
                "provider_execution_key": provider_execution_key,
                "provider_request_sha256": provider_request_sha256,
                "actual_execution": None, "usage": None,
            }
        return FACILITY._copy(result)

    def preflight(self, requirements, **_kwargs):
        return {
            "schema": "translate-native.subagent-review-facility-preflight-result.v1",
            "status": "ready",
            "challenge": requirements["challenge"],
            "requirements_sha256": FACILITY._sha(requirements),
            "driver_deployment_sha256": requirements["driver_deployment_sha256"],
            "deployment_manifest_sha256": self.deployment_manifest_sha256,
            "route_requirements_sha256": requirements[
                "route_requirements_sha256"
            ],
            "capabilities": requirements["required_capabilities"],
        }


class WSGIFacilityTransport:
    def __init__(self, application):
        self.application = application
        self.calls = []
        self.lose_first_execute_reply = False

    def post(self, url, headers, body, *, timeout):
        self.calls.append((dict(headers), bytes(body), timeout))
        environ = {
            "PATH_INFO": (FACILITY.READINESS_PATH if url.endswith("/readiness")
                          else FACILITY.PATH), "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "http", "SERVER_NAME": "127.0.0.1",
            "CONTENT_TYPE": headers["Content-Type"],
            "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
        }
        for name, value in headers.items():
            if name.lower() not in {"content-type", "content-length"}:
                environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = tuple(response_headers)

        response = b"".join(self.application(environ, start_response))
        request = json.loads(body)
        if (request.get("operation") == "execute"
                and self.lose_first_execute_reply):
            self.lose_first_execute_reply = False
            raise OSError("synthetic lost facility response")
        return BACKEND.HTTPResult(
            captured["status"], captured["headers"], response,
        )


class FacilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.driver = FixtureDriver()
        self.boot_id = "a" * 64

    @staticmethod
    def routes(host_routes):
        return [FACILITY.EXECUTOR.PinnedExecutorRoute(
            route_id=route.route_id, phase=route.phase,
            reviewer_role=route.reviewer_role,
            reviewer_agent_id=route.reviewer_agent_id,
            model_id=route.model_id, model_version=route.model_version,
            host_policy_version=route.host_policy_version,
            target_locale=route.target_locale, content_type=route.content_type,
            task_policy_sha256=route.task_policy_sha256,
        ) for route in host_routes]

    def application(self, routes, *, driver=None, ledger=None, capacity=4,
                    readiness=None, accept_new_executions=True):
        ledger = ledger or FACILITY.SQLiteFacilityLedger(
            Path(self.temporary.name) / "facility.sqlite3",
            max_concurrent_executions=capacity, boot_id=self.boot_id,
        )
        return FACILITY.SubagentFacilityApplication(
            backend_id="production-host-subagents",
            backend_version="backend-1",
            facility_id="host-subagent-facility",
            facility_version="facility-1", bearer_token=TOKEN,
            policy=FACILITY.EXECUTOR.PinnedExecutorPolicy(self.routes(routes)),
            ledger=ledger, driver=driver or self.driver,
            readiness=readiness or (lambda: {
                "ready": True, "reason": "ready", "probe_generation": 1,
                "failure_generation": 0,
                "routes_checked": len(routes),
                "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                "readiness_policy_sha256": READINESS_POLICY_SHA256,
                "facility_ledger_instance_id": FACILITY_LEDGER_INSTANCE_ID,
            }),
            allow_loopback_http=True,
            accept_new_executions=accept_new_executions,
            drain_id=None if accept_new_executions else "d" * 64,
        )

    def backend(self, routes, *, driver=None, ledger=None, capacity=4):
        transport = WSGIFacilityTransport(self.application(
            routes, driver=driver, ledger=ledger, capacity=capacity,
        ))
        backend = BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            backend_id="production-host-subagents",
            backend_version="backend-1",
            facility_id="host-subagent-facility",
            facility_version="facility-1", transport=transport,
            max_output_tokens=4096, allow_loopback_http=True,
            expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
            expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
            expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
            expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
            expected_facility_ledger_instance_id=FACILITY_LEDGER_INSTANCE_ID,
            expected_routes_count=len(routes),
        )
        return backend, transport

    def chain(self, routes, *, driver=None, namespace="default"):
        directory = Path(self.temporary.name) / namespace
        directory.mkdir(exist_ok=True)
        facility_ledger = FACILITY.SQLiteFacilityLedger(
            directory / "facility.sqlite3",
            max_concurrent_executions=4, boot_id=self.boot_id,
        )
        backend, transport = self.backend(
            routes, driver=driver, ledger=facility_ledger,
        )
        fixture = EXEC_TEST.ExecutorTests()
        fixture.temporary = type("FixtureDirectory", (), {
            "name": str(directory),
        })()
        executor = fixture.executor(routes, backend=backend)
        host = fixture.review_host(routes, fixture.launcher(executor))
        return fixture.review_client(host), transport

    def test_finnish_response_crosses_actual_backend_and_facility_source_blind(self):
        task, control = HOST_TEST.response_request("fi-FI")
        client, transport = self.chain([HOST_TEST.response_route(task)])
        result = client.run_isolated(task, control=control)
        self.assertEqual(result["response"]["status"], "PASS")
        self.assertEqual(self.driver.physical_starts, 1)
        assignment, model_input, _key, _digest, _budgets, isolation = \
            self.driver.starts[0]
        self.assertEqual(assignment["phase"], "target_native")
        self.assertNotIn("source", model_input["input"])
        serialized = json.dumps(model_input, ensure_ascii=False)
        self.assertNotIn("conversation_history", serialized)
        self.assertNotIn(TOKEN, serialized)
        self.assertEqual(isolation, FACILITY.ISOLATION)
        self.assertNotIn(TOKEN, transport.calls[0][1].decode("utf-8"))

    def test_maltese_reconcile_only_blocks_execute_before_facility_reservation(self):
        routes, captures = HOST_TEST.website_routes(["mt-MT"])
        task, control = captures["mt-MT"].calls[0]
        route = routes[0]
        ledger = FACILITY.SQLiteFacilityLedger(
            Path(self.temporary.name) / "facility-drain.sqlite3",
            max_concurrent_executions=4, boot_id=self.boot_id,
        )
        application = self.application(
            routes, ledger=ledger, accept_new_executions=False,
        )
        transport = WSGIFacilityTransport(application)
        backend = BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            backend_id="production-host-subagents", backend_version="backend-1",
            facility_id="host-subagent-facility", facility_version="facility-1",
            transport=transport, max_output_tokens=4096,
            allow_loopback_http=True,
            expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
            expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
            expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
            expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
            expected_facility_ledger_instance_id=FACILITY_LEDGER_INSTANCE_ID,
            expected_routes_count=len(routes),
        )
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        readiness = backend.readiness()
        self.assertEqual(readiness["operation_mode"], "reconcile_only")
        self.assertEqual(readiness["drain_id"], "d" * 64)
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed):
            backend.execute_idempotent(
                assignment, model_input,
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
        self.assertEqual(ledger.count_active(), 0)
        self.assertEqual(self.driver.starts, [])
        class Unreadable:
            def read(self, _size):
                raise AssertionError("drain gate read the request body")
        statuses = []
        body = b"".join(application({
            "PATH_INFO": FACILITY.PATH, "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "http", "SERVER_NAME": "127.0.0.1",
            "HTTP_AUTHORIZATION": "Bearer " + TOKEN,
            "HTTP_X_SUBAGENT_FACILITY_OPERATION": "execute",
            "CONTENT_LENGTH": "1", "wsgi.input": Unreadable(),
        }, lambda status, _headers: statuses.append(status)))
        self.assertTrue(statuses[0].startswith("409"))
        self.assertIn(b"drain_active", body)

    def test_maltese_translation_uses_two_separate_facility_executions(self):
        routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        client, _transport = self.chain(routes)
        result = BASE.HostSubagentTests().execute(
            BASE.adapter(client), "mt-MT",
        )
        self.assertTrue(result["release_required"])
        self.assertEqual(
            [item[0]["phase"] for item in self.driver.starts],
            ["target_native", "source_fidelity"],
        )
        self.assertNotIn("source", self.driver.starts[0][1]["input"])
        self.assertEqual(
            self.driver.starts[1][1]["input"]["source"]["text"],
            "Build your business with BLUN.",
        )
        self.assertNotEqual(
            self.driver.starts[0][2], self.driver.starts[1][2],
        )

    def test_lost_driver_and_facility_responses_reconcile_without_duplicate_start(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        self.driver.raise_after_completion = True
        client, transport = self.chain([route])
        with self.assertRaises(HOST_TEST.HTTP.HTTPReviewHostFailed):
            client.run_isolated(task, control=control)
        recovered = client.run_isolated(task, control=control)
        self.assertEqual(recovered["response"]["status"], "PASS")
        self.assertEqual(self.driver.physical_starts, 1)
        self.assertTrue(self.driver.reconciles)

        other_driver = FixtureDriver()
        other_client, other_transport = self.chain(
            [route], driver=other_driver, namespace="lost-facility-response",
        )
        other_transport.lose_first_execute_reply = True
        with self.assertRaises(HOST_TEST.HTTP.HTTPReviewHostFailed):
            other_client.run_isolated(task, control=control)
        other_recovered = other_client.run_isolated(task, control=control)
        self.assertEqual(other_recovered["response"]["status"], "PASS")
        self.assertEqual(other_driver.physical_starts, 1)

    def test_reconcile_cannot_cross_facility_ledger_instances(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = vars(
            HOST.ReviewHostApplication._assignment(route, control)
        )
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        first = self.application([route])
        second_ledger = FACILITY.SQLiteFacilityLedger(
            Path(self.temporary.name) / "second.sqlite3",
            max_concurrent_executions=4, boot_id="b" * 64,
        )
        second = self.application(
            [route], ledger=second_ledger,
            readiness=lambda: {
                "ready": True, "reason": "ready", "probe_generation": 1,
                "failure_generation": 0, "routes_checked": 1,
                "facility_ledger_instance_id": "d" * 64,
                "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                "readiness_policy_sha256": READINESS_POLICY_SHA256,
            },
        )
        first_transport = WSGIFacilityTransport(first)
        first_transport.lose_first_execute_reply = True
        backend = BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            backend_id="production-host-subagents", backend_version="backend-1",
            facility_id="host-subagent-facility", facility_version="facility-1",
            transport=first_transport, allow_loopback_http=True,
            expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
            expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
            expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
            expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
            expected_facility_ledger_instance_id=FACILITY_LEDGER_INSTANCE_ID,
            expected_routes_count=1,
        )
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed):
            backend.execute_idempotent(
                assignment, model_input,
                execute_request_sha256="8" * 64, budgets=budgets,
            )
        self.assertEqual(self.driver.physical_starts, 1)
        backend.transport = WSGIFacilityTransport(second)
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
            backend.reconcile(
                assignment, execute_request_sha256="8" * 64,
            )
        self.assertEqual(blocked.exception.code, "backend_http.status")
        self.assertEqual(second_ledger.count_active(), 0)

        backend.transport = WSGIFacilityTransport(first)
        recovered = backend.reconcile(
            assignment, execute_request_sha256="8" * 64,
        )
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.driver.physical_starts, 1)

    def test_changed_replay_wrong_identity_and_usage_are_quarantined(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        backend, _transport = self.backend([route])
        first = backend.execute_idempotent(
            vars(assignment), model_input,
            execute_request_sha256="2" * 64, budgets=budgets,
        )
        self.assertEqual(first["status"], "completed")
        changed = FACILITY._copy(model_input)
        changed["input"]["candidate"] += " Muutos."
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as conflict:
            backend.execute_idempotent(
                vars(assignment), changed,
                execute_request_sha256="2" * 64, budgets=budgets,
            )
        self.assertEqual(conflict.exception.code,
                         "backend_http.idempotency_conflict")
        self.assertEqual(self.driver.physical_starts, 1)

        for index, mutation in enumerate((
            lambda result: result["actual_execution"].update(agent_id="writer"),
            lambda result: result["usage"].update(cost_units=10**9),
            lambda result: result.update(provider_request_sha256="0" * 64),
        )):
            with self.subTest(index=index):
                driver = FixtureDriver()
                driver.mutate = mutation
                fresh_backend, _ = self.backend(
                    [route], driver=driver,
                    ledger=FACILITY.SQLiteFacilityLedger(
                        Path(self.temporary.name) / f"bad-{index}.sqlite3",
                        max_concurrent_executions=4,
                        boot_id=f"{index + 1:064x}",
                    ),
                )
                with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
                    fresh_backend.execute_idempotent(
                        vars(assignment), model_input,
                        execute_request_sha256=f"{index + 3:064x}",
                        budgets=budgets,
                    )
                self.assertFalse(blocked.exception.retryable)

    def test_authentication_precedes_body_and_driver(self):
        task, _control = HOST_TEST.response_request("fi-FI")
        application = self.application([HOST_TEST.response_route(task)])

        class BlockingBody:
            def read(self, _size):
                raise AssertionError("body must not be read before authentication")

        statuses = []
        for path in (FACILITY.PATH, FACILITY.READINESS_PATH):
            statuses.clear()
            response = b"".join(application({
                "PATH_INFO": path, "REQUEST_METHOD": "POST",
                "wsgi.url_scheme": "http", "SERVER_NAME": "127.0.0.1",
                "CONTENT_TYPE": "application/json; charset=utf-8",
                "CONTENT_LENGTH": "2", "wsgi.input": BlockingBody(),
                "HTTP_AUTHORIZATION": (
                    "Bearer wrong-token-with-at-least-32-characters"
                ),
            }, lambda status, _headers: statuses.append(status)))
            self.assertTrue(statuses[0].startswith("401"))
            self.assertIn(b"authentication_rejected", response)
        self.assertEqual(self.driver.starts, [])
        self.assertEqual(application.ledger.count_active(), 0)

    def test_unhealthy_readiness_blocks_execute_before_ledger_but_not_reconcile(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        readiness_calls = []

        def unhealthy():
            readiness_calls.append(True)
            return {
                "ready": False, "reason": "preflight_failed",
                "probe_generation": 2, "failure_generation": 1,
                "routes_checked": 1,
                "facility_ledger_instance_id": FACILITY_LEDGER_INSTANCE_ID,
                "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                "readiness_policy_sha256": READINESS_POLICY_SHA256,
            }

        application = self.application([route], readiness=unhealthy)
        transport = WSGIFacilityTransport(application)
        backend = BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            backend_id="production-host-subagents", backend_version="backend-1",
            facility_id="host-subagent-facility", facility_version="facility-1",
            transport=transport, max_output_tokens=4096,
            allow_loopback_http=True,
            expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
            expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
            expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
            expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
            expected_facility_ledger_instance_id=FACILITY_LEDGER_INSTANCE_ID,
            expected_routes_count=1,
        )
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
            backend.execute_idempotent(
                vars(assignment), model_input,
                execute_request_sha256="f" * 64, budgets=budgets,
            )
        self.assertEqual(blocked.exception.code, "backend_http.status")
        self.assertTrue(blocked.exception.retryable)
        self.assertEqual(self.driver.starts, [])
        self.assertEqual(application.ledger.count_active(), 0)
        self.assertEqual(len(readiness_calls), 1)

        reconciled = backend.reconcile(
            vars(assignment), execute_request_sha256="f" * 64,
        )
        self.assertEqual(reconciled["status"], "not_started")
        self.assertEqual(len(readiness_calls), 2)

    def test_wrong_deployment_or_health_generation_blocks_before_ledger(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        for name, changed in (
                ("driver", {"expected_driver_deployment_sha256": "c" * 64}),
                ("policy", {"expected_readiness_policy_sha256": "d" * 64})):
            with self.subTest(name=name):
                application = self.application([route])
                options = {
                    "expected_driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                    "expected_deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                    "expected_route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                    "expected_readiness_policy_sha256": READINESS_POLICY_SHA256,
                    "expected_facility_ledger_instance_id": (
                        FACILITY_LEDGER_INSTANCE_ID
                    ),
                }
                options.update(changed)
                backend = BACKEND.HTTPSExecutionBackend(
                    "http://127.0.0.1/v1/isolated-review-executions",
                    lambda: {"Authorization": "Bearer " + TOKEN},
                    backend_id="production-host-subagents",
                    backend_version="backend-1",
                    facility_id="host-subagent-facility",
                    facility_version="facility-1",
                    transport=WSGIFacilityTransport(application),
                    expected_routes_count=1, allow_loopback_http=True,
                    **options,
                )
                with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as execute:
                    backend.execute_idempotent(
                        vars(assignment), model_input,
                        execute_request_sha256="e" * 64, budgets=budgets,
                    )
                self.assertFalse(execute.exception.retryable)
                with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as reconcile:
                    backend.reconcile(
                        vars(assignment), execute_request_sha256="e" * 64,
                    )
                self.assertFalse(reconcile.exception.retryable)
                self.assertEqual(application.ledger.count_active(), 0)
                self.assertEqual(self.driver.starts, [])

    def test_mid_execution_probe_failure_blocks_reply_and_reconciles_once(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        state = {"probe_generation": 1, "failure_generation": 0}

        def readiness():
            return {
                "ready": True, "reason": "ready",
                "probe_generation": state["probe_generation"],
                "failure_generation": state["failure_generation"],
                "routes_checked": 1,
                "facility_ledger_instance_id": FACILITY_LEDGER_INSTANCE_ID,
                "driver_deployment_sha256": DRIVER_DEPLOYMENT_SHA256,
                "deployment_manifest_sha256": DEPLOYMENT_MANIFEST_SHA256,
                "route_requirements_sha256": ROUTE_REQUIREMENTS_SHA256,
                "readiness_policy_sha256": READINESS_POLICY_SHA256,
            }

        def fail_and_recover(_result):
            state.update(probe_generation=3, failure_generation=1)
            self.driver.mutate = None

        self.driver.mutate = fail_and_recover
        application = self.application([route], readiness=readiness)
        transport = WSGIFacilityTransport(application)
        backend = BACKEND.HTTPSExecutionBackend(
            "http://127.0.0.1/v1/isolated-review-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            backend_id="production-host-subagents", backend_version="backend-1",
            facility_id="host-subagent-facility", facility_version="facility-1",
            transport=transport, allow_loopback_http=True,
            expected_driver_deployment_sha256=DRIVER_DEPLOYMENT_SHA256,
            expected_deployment_manifest_sha256=DEPLOYMENT_MANIFEST_SHA256,
            expected_route_requirements_sha256=ROUTE_REQUIREMENTS_SHA256,
            expected_readiness_policy_sha256=READINESS_POLICY_SHA256,
            expected_facility_ledger_instance_id=FACILITY_LEDGER_INSTANCE_ID,
            expected_routes_count=1,
        )
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as interrupted:
            backend.execute_idempotent(
                vars(assignment), model_input,
                execute_request_sha256="7" * 64, budgets=budgets,
            )
        self.assertTrue(interrupted.exception.retryable)
        self.assertEqual(self.driver.physical_starts, 1)
        recovered = backend.reconcile(
            vars(assignment), execute_request_sha256="7" * 64,
        )
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.driver.physical_starts, 1)

    def test_execute_not_started_and_native_source_injection_fail_closed(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        self.driver.forced_status = "not_started"
        backend, transport = self.backend([route])
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
            backend.execute_idempotent(
                vars(assignment), model_input,
                execute_request_sha256="8" * 64, budgets=budgets,
            )
        self.assertFalse(blocked.exception.retryable)

        injected = FACILITY._copy(model_input)
        injected["input"]["quality_profile"]["conversation_history"] = [
            "hidden source",
        ]
        calls = len(transport.calls)
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed):
            backend.execute_idempotent(
                vars(assignment), injected,
                execute_request_sha256="9" * 64, budgets=budgets,
            )
        self.assertEqual(len(transport.calls), calls)

    def test_facility_capacity_is_bound_independently_of_client_budget(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        budgets["max_concurrent_executions"] = 3
        backend, _transport = self.backend([route], capacity=4)
        with self.assertRaises(BACKEND.SubagentHTTPBackendFailed) as blocked:
            backend.execute_idempotent(
                vars(assignment), model_input,
                execute_request_sha256="6" * 64, budgets=budgets,
            )
        self.assertFalse(blocked.exception.retryable)
        self.assertEqual(self.driver.starts, [])

    def test_restart_fences_foreign_dispatch_before_reconcile(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        budgets = BACKEND_TEST.HTTPBackendTests.direct_request()[2]
        path = Path(self.temporary.name) / "restart.sqlite3"
        budgets["max_concurrent_executions"] = 1
        ledger = FACILITY.SQLiteFacilityLedger(
            path, max_concurrent_executions=1, boot_id="a" * 64,
        )
        backend, _ = self.backend([route], ledger=ledger)
        self.driver.forced_status = "running"
        running = backend.execute_idempotent(
            vars(assignment), model_input,
            execute_request_sha256="7" * 64, budgets=budgets,
        )
        self.assertEqual(running["status"], "running")
        with ledger._connect() as connection:
            connection.execute(
                "UPDATE subagent_facility_jobs SET status='dispatching',"
                "owner_boot_id=?", ("a" * 64,),
            )
        restarted = FACILITY.SQLiteFacilityLedger(
            path, max_concurrent_executions=1, boot_id="b" * 64,
            initialize_schema=False,
        )
        self.assertEqual(restarted.recover_foreign_dispatches(), 1)
        self.assertEqual(restarted.count_active(), 1)


if __name__ == "__main__":
    unittest.main()
