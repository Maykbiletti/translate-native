"""Synthetic executor fixtures; not native-language quality evidence."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagents as BASE
import test_native_rewrite_worker as REWRITE


EXECUTOR = BASE.load(
    "test_website_localization_subagent_executor_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_executor.py",
)
LAUNCHER = EXECUTOR.LAUNCHER
HOST = HOST_TEST.HOST
HTTP = HOST_TEST.HTTP
EXECUTOR_TOKEN = "executor-bearer-token-with-at-least-32-characters"


class FixtureBackend:
    backend_id = "fixture-host-subagents"
    backend_version = "fixture-1"

    def __init__(self):
        self.starts, self.reconciles, self.completed = [], [], {}
        self.readiness_calls = 0
        self.readiness_error = None
        self.readiness_result = {
            "ready": True, "fixture": True,
            "operation_mode": "execute_and_reconcile", "drain_id": None,
        }
        self.readiness_hook = None
        self.running = set()
        self.raise_after_start = False
        self.stay_running = False
        self.mutate = None

    def readiness(self):
        self.readiness_calls += 1
        if self.readiness_error is not None:
            raise self.readiness_error
        if self.readiness_hook is not None:
            self.readiness_hook()
        return EXECUTOR._copy(self.readiness_result)

    @staticmethod
    def _response(model_input):
        locale = model_input["input"]["target"]["locale"]
        if model_input["schema"] == HOST.RESPONSE_REVIEW_SCHEMA:
            return {
                "schema": HOST.RESPONSE_NATIVE_SCHEMA,
                "phase": "target_native", "locale": locale,
                "status": "PASS", "confidence": "high",
                "findings": [], "uncertainties": [],
            }
        response_schema = model_input["input"].get("response_schema", {}).get("schema")
        if response_schema == REWRITE.RW.REVIEW_SCHEMA:
            response = {
                "schema": REWRITE.RW.REVIEW_SCHEMA,
                "phase": model_input["phase"], "locale": locale,
                "status": "PASS", "confidence": "high",
                "blocking_defects": [], "major_defects": [], "uncertainties": [],
            }
            if model_input["phase"] == "target_native":
                response["holistic_assessment"] = {
                    "reads_as_native_original": True,
                    "reason": "Synthetic fixture marks the complete candidate as native.",
                    "repair_scope": "none",
                    "dimensions": {name: "PASS" for name in HOST.NATIVE_DIMENSIONS},
                }
            return response
        return BASE.review(locale, model_input["phase"], confidence="high")

    def _completed(self, assignment, model_input, request_sha256, budgets):
        execution = {
            "response": self._response(model_input),
            "execution_key": assignment["execution_key"],
            "phase": assignment["phase"],
            "reviewer_role": assignment["reviewer_role"],
            "agent_id": assignment["reviewer_agent_id"],
            "session_id": assignment["reviewer_session_id"],
            "model_id": assignment["model_id"],
            "model_version": assignment["model_version"],
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
        }
        usage = {
            "execute_request_sha256": request_sha256,
            "cost_unit": budgets["cost_unit"], "cost_units": 7,
            "input_bytes": len(EXECUTOR._raw(model_input)), "output_tokens": 1,
        }
        result = {"status": "completed", "execution": execution, "usage": usage}
        if self.mutate:
            self.mutate(result)
        return result

    def execute_idempotent(self, assignment, model_input, *,
                           execute_request_sha256, budgets):
        key = assignment["execution_key"]
        self.starts.append((EXECUTOR._copy(assignment), EXECUTOR._copy(model_input),
                            execute_request_sha256, EXECUTOR._copy(budgets)))
        if key not in self.completed:
            self.completed[key] = self._completed(
                assignment, model_input, execute_request_sha256, budgets,
            )
        if self.stay_running:
            self.running.add(key)
            return {"status": "running", "execution": None, "usage": None}
        if self.raise_after_start:
            self.raise_after_start = False
            raise RuntimeError("synthetic lost backend response")
        return EXECUTOR._copy(self.completed[key])

    def reconcile(self, assignment, *, execute_request_sha256):
        key = assignment["execution_key"]
        self.reconciles.append((EXECUTOR._copy(assignment), execute_request_sha256))
        if key in self.running:
            return {"status": "running", "execution": None, "usage": None}
        result = self.completed.get(key)
        if result is None:
            return {"status": "not_started", "execution": None, "usage": None}
        return EXECUTOR._copy(result)


class WSGIExecutorTransport:
    def __init__(self, application):
        self.application = application

    def post(self, _url, headers, body, *, timeout):
        del timeout
        environ = {
            "PATH_INFO": EXECUTOR.PATH, "REQUEST_METHOD": "POST",
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
        return LAUNCHER.HTTPResult(
            captured["status"], captured["headers"], response,
        )


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.backend = FixtureBackend()

    @staticmethod
    def executor_routes(host_routes):
        return [EXECUTOR.PinnedExecutorRoute(
            route_id=route.route_id, phase=route.phase,
            reviewer_role=route.reviewer_role,
            reviewer_agent_id=route.reviewer_agent_id,
            model_id=route.model_id, model_version=route.model_version,
            host_policy_version=route.host_policy_version,
            target_locale=route.target_locale, content_type=route.content_type,
            task_policy_sha256=route.task_policy_sha256,
        ) for route in host_routes]

    def executor(self, host_routes, *, backend=None, ledger=None, capacity=4,
                 accept_new_executions=True):
        ledger = ledger or EXECUTOR.SQLiteExecutionLedger(
            Path(self.temporary.name) / "executor.sqlite3",
            max_concurrent_executions=capacity,
        )
        return EXECUTOR.SubagentExecutorApplication(
            executor_id="executor-1", launcher_id="deployment-review-launcher",
            launcher_version="launcher-1", bearer_token=EXECUTOR_TOKEN,
            policy=EXECUTOR.PinnedExecutorPolicy(self.executor_routes(host_routes)),
            ledger=ledger, backend=backend or self.backend,
            allow_loopback_http=True,
            accept_new_executions=accept_new_executions,
        )

    def launcher(self, application, *, capacity=4):
        return LAUNCHER.HTTPSSubagentLauncher(
            "http://127.0.0.1/v1/subagent-executions",
            lambda: {"Authorization": "Bearer " + EXECUTOR_TOKEN},
            launcher_id="deployment-review-launcher", launcher_version="launcher-1",
            executor_id="executor-1", transport=WSGIExecutorTransport(application),
            request_timeout_seconds=60, poll_interval_milliseconds=10,
            max_status_polls=2, max_concurrent_executions=capacity,
            allow_loopback_http=True, sleeper=lambda _seconds: None,
        )

    def review_host(self, host_routes, launcher):
        signer = HOST.HMACAttestationSigner(HOST_TEST.SECRET, HOST_TEST.KEY_ID)
        ledger = HOST.SQLiteReviewLedger(
            Path(self.temporary.name) / "host.sqlite3", lease_seconds=65,
        )
        return HOST.ReviewHostApplication(
            host_id=HOST_TEST.HOST_ID, bearer_token=HOST_TEST.TOKEN,
            signer=signer, policy=HOST.PinnedReviewPolicy(host_routes),
            ledger=ledger, launcher=launcher, allow_loopback_http=True,
        )

    @staticmethod
    def review_client(application):
        return HTTP.HTTPSReviewHost(
            "http://127.0.0.1/v1/subagent-reviews",
            lambda: {"Authorization": "Bearer " + HOST_TEST.TOKEN},
            HOST_TEST.Verifier(), host_id=HOST_TEST.HOST_ID,
            transport=HOST_TEST.WSGITransport(application),
            allow_loopback_http=True,
        )

    def test_finnish_response_crosses_real_launcher_and_executor_source_blind(self):
        task, control = HOST_TEST.response_request("fi-FI")
        routes = [HOST_TEST.response_route(task)]
        executor = self.executor(routes)
        host = self.review_host(routes, self.launcher(executor))
        result = self.review_client(host).run_isolated(task, control=control)
        self.assertEqual(result["response"]["status"], "PASS")
        self.assertEqual(len(self.backend.starts), 1)
        assignment, model_input, _digest, budgets = self.backend.starts[0]
        serialized = json.dumps(model_input, ensure_ascii=False)
        self.assertEqual(assignment["reviewer_role"], "target-native-reviewer")
        self.assertNotIn('"source"', serialized)
        self.assertNotIn('"creator_id"', serialized)
        self.assertNotIn(EXECUTOR_TOKEN, serialized)
        self.assertFalse(self.backend.completed[control["execution_key"]][
            "execution"]["inherit_context"])
        self.assertEqual(budgets["max_concurrent_executions"], 4)

    def test_long_rewrite_crosses_real_http_host_launcher_and_executor(self):
        # Synthetic protocol fixture only: it proves the real isolated route,
        # not Finnish native quality or improvement over another system.
        source = ("Pitkä synteettinen teksti säilyttää ääkköset ja numeron 42.\n\n"
                  * 120).strip()
        echo = lambda request: request.input["owned_source"]["text"]
        capture = REWRITE.Host()
        capture_worker = REWRITE.RW.NativeRewriteWorker(
            REWRITE.Creator(echo), capture,
            ledger_path=Path(self.temporary.name) / "capture-rewrite.sqlite",
            creator_id="writer", creator_session_id="writer-session",
            model_id="fixture-model", model_version="fixture-model-1",
            host_policy_version="fixture-host-v1", profile=REWRITE.profile())
        capture_worker.run(source, "prose", "capture-long-route")
        routes = []
        for task, _control in capture.calls:
            routes.append(HOST.PinnedReviewRoute(
                route_id="long-rewrite-" + task["phase"], schema=task["schema"],
                phase=task["phase"], target_locale="fi-FI", content_type="prose",
                task_policy_sha256=HOST.task_policy_sha256(task),
                model_id="fixture-model", model_version="fixture-model-1",
                host_policy_version="fixture-host-v1",
                reviewer_agent_id="reviewer:" + task["phase"],
                reviewer_role=("target-native-reviewer"
                               if task["phase"] == "target_native"
                               else "source-fidelity-reviewer")))
        executor = self.executor(routes)
        host = self.review_host(routes, self.launcher(executor))
        client = self.review_client(host)
        worker = REWRITE.RW.NativeRewriteWorker(
            REWRITE.Creator(echo), client,
            ledger_path=Path(self.temporary.name) / "actual-rewrite.sqlite",
            creator_id="writer", creator_session_id="writer-session",
            model_id="fixture-model", model_version="fixture-model-1",
            host_policy_version="fixture-host-v1", profile=REWRITE.profile())
        result = worker.run(source, "prose", "actual-long-route")
        self.assertEqual(result["target_text"], source)
        self.assertEqual(len(self.backend.starts), 2)
        native = self.backend.starts[0][1]
        fidelity = self.backend.starts[1][1]
        self.assertEqual(native["input"]["candidate"], source)
        self.assertNotIn("source", native["input"])
        self.assertNotIn("review_scope", native["input"])
        self.assertEqual(fidelity["input"]["source"]["text"], source)

    def test_finnish_reconcile_only_blocks_execute_before_reservation(self):
        task, control = HOST_TEST.response_request("fi-FI")
        routes = [HOST_TEST.response_route(task)]
        ledger = EXECUTOR.SQLiteExecutionLedger(
            Path(self.temporary.name) / "executor-drain.sqlite3",
            max_concurrent_executions=4,
        )
        application = self.executor(
            routes, ledger=ledger, accept_new_executions=False,
        )
        assignment = HOST.ReviewHostApplication._assignment(routes[0], control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed):
            self.launcher(application).execute_idempotent(
                assignment, model_input,
                deadline_seconds=assignment.deadline_seconds,
                max_output_tokens=assignment.max_output_tokens,
            )
        self.assertEqual(ledger.count_active(), 0)
        self.assertEqual(self.backend.starts, [])
        class Unreadable:
            def read(self, _size):
                raise AssertionError("drain gate read the request body")
        statuses = []
        body = b"".join(application({
            "PATH_INFO": EXECUTOR.PATH, "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "http", "SERVER_NAME": "127.0.0.1",
            "HTTP_AUTHORIZATION": "Bearer " + EXECUTOR_TOKEN,
            "HTTP_X_SUBAGENT_OPERATION": "execute",
            "CONTENT_LENGTH": "1", "wsgi.input": Unreadable(),
        }, lambda status, _headers: statuses.append(status)))
        self.assertTrue(statuses[0].startswith("409"))
        self.assertIn(b"drain_active", body)
        self.assertEqual(
            self.launcher(application).reconcile(
                assignment, model_input,
                deadline_seconds=assignment.deadline_seconds,
                max_output_tokens=assignment.max_output_tokens,
            ),
            {"status": "not_started", "execution": None},
        )

    def test_maltese_translation_uses_two_ordered_isolated_executor_starts(self):
        routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        executor = self.executor(routes)
        provider = BASE.adapter(self.review_client(
            self.review_host(routes, self.launcher(executor)),
        ))
        result = BASE.HostSubagentTests().execute(provider, "mt-MT")
        self.assertTrue(result["release_required"])
        self.assertEqual([item[0]["phase"] for item in self.backend.starts],
                         ["target_native", "source_fidelity"])
        native, fidelity = self.backend.starts
        self.assertNotIn("source", native[1]["input"])
        self.assertEqual(fidelity[1]["input"]["source"]["text"],
                         "Build your business with BLUN.")
        self.assertNotEqual(native[0]["reviewer_agent_id"],
                            fidelity[0]["reviewer_agent_id"])

    def test_lost_backend_response_recovers_without_second_start(self):
        task, control = HOST_TEST.response_request("fi-FI")
        routes = [HOST_TEST.response_route(task)]
        self.backend.raise_after_start = True
        executor = self.executor(routes)
        client = self.review_client(self.review_host(routes, self.launcher(executor)))
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as lost:
            client.run_isolated(task, control=control)
        self.assertTrue(lost.exception.retryable)
        recovered = client.run_isolated(task, control=control)
        self.assertEqual(recovered["response"]["status"], "PASS")
        self.assertEqual(len(self.backend.starts), 1)
        self.assertEqual(len(self.backend.reconciles), 1)

    def test_restart_recovers_backend_completion_before_ledger_commit(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)

        class CrashAfterCompletionBackend(FixtureBackend):
            def execute_idempotent(inner_self, *args, **kwargs):
                super().execute_idempotent(*args, **kwargs)
                raise SystemExit("synthetic process crash after backend completion")

        backend = CrashAfterCompletionBackend()
        ledger_path = Path(self.temporary.name) / "crash-recovery.sqlite3"
        first_ledger = EXECUTOR.SQLiteExecutionLedger(
            ledger_path, max_concurrent_executions=1,
        )
        first = self.executor(
            [route], backend=backend, ledger=first_ledger, capacity=1,
        )
        with self.assertRaises(SystemExit):
            self.launcher(first, capacity=1).execute_idempotent(
                assignment, model_input,
                deadline_seconds=assignment.deadline_seconds,
                max_output_tokens=assignment.max_output_tokens,
            )
        self.assertEqual(first_ledger.count_active(), 1)

        restarted_ledger = EXECUTOR.SQLiteExecutionLedger(
            ledger_path, max_concurrent_executions=1,
            initialize_schema=False,
        )
        restarted = self.executor(
            [route], backend=backend, ledger=restarted_ledger, capacity=1,
        )
        recovered = self.launcher(restarted, capacity=1).reconcile(
            assignment, model_input,
            deadline_seconds=assignment.deadline_seconds,
            max_output_tokens=assignment.max_output_tokens,
        )
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["execution"]["response"]["status"], "PASS")
        self.assertEqual(len(backend.starts), 1)
        self.assertEqual(len(backend.reconciles), 1)
        self.assertEqual(restarted_ledger.count_active(), 0)

    def test_capacity_is_durable_across_executor_restart(self):
        first_task, _first_control = HOST_TEST.response_request("fi-FI")
        second_task, _second_control = HOST_TEST.response_request(
            "fi-FI", target="Toinen tekninen testivastaus.",
        )
        first_route = HOST_TEST.response_route(first_task)
        second_route = replace(
            HOST_TEST.response_route(second_task, agent="reviewer-native-2"),
            route_id="ordinary-fi-FI-second",
        )
        routes = [first_route, second_route]
        self.backend.stay_running = True
        ledger_path = Path(self.temporary.name) / "durable.sqlite3"
        first_ledger = EXECUTOR.SQLiteExecutionLedger(
            ledger_path, max_concurrent_executions=1,
        )
        first = self.executor(routes, ledger=first_ledger, capacity=1)
        first_launcher = self.launcher(first, capacity=1)
        first_assignment = HOST.ReviewHostApplication._assignment(
            first_route, _first_control,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed):
            first_launcher.execute_idempotent(
                first_assignment, HOST.ReviewHostApplication._model_task(first_task),
                deadline_seconds=first_assignment.deadline_seconds,
                max_output_tokens=first_assignment.max_output_tokens,
            )
        restarted = self.executor(
            routes, ledger=EXECUTOR.SQLiteExecutionLedger(
                ledger_path, max_concurrent_executions=1,
                initialize_schema=False,
            ), capacity=1,
        )
        second_assignment = HOST.ReviewHostApplication._assignment(
            second_route, _second_control,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as full:
            self.launcher(restarted, capacity=1).execute_idempotent(
                second_assignment,
                HOST.ReviewHostApplication._model_task(second_task),
                deadline_seconds=second_assignment.deadline_seconds,
                max_output_tokens=second_assignment.max_output_tokens,
            )
        self.assertEqual(full.exception.code, "launcher.status")
        self.assertEqual(len(self.backend.starts), 1)
        self.assertEqual(restarted.ledger.count_active(), 1)

    def test_reconcile_cannot_free_paused_dispatch_capacity(self):
        first_task, first_control = HOST_TEST.response_request("fi-FI")
        second_task, second_control = HOST_TEST.response_request(
            "fi-FI", target="Toinen rinnakkainen testivastaus.",
        )
        first_route = HOST_TEST.response_route(first_task)
        second_route = replace(
            HOST_TEST.response_route(second_task, agent="reviewer-native-2"),
            route_id="ordinary-fi-FI-parallel",
        )
        routes = [first_route, second_route]
        paused, resume = threading.Event(), threading.Event()

        class PausingLedger(EXECUTOR.SQLiteExecutionLedger):
            def reserve_execute(inner_self, **values):
                reservation = super().reserve_execute(**values)
                if (reservation.owner
                        and reservation.execution_key == first_control["execution_key"]):
                    paused.set()
                    self.assertTrue(resume.wait(2))
                return reservation

        ledger = PausingLedger(
            Path(self.temporary.name) / "dispatch-race.sqlite3",
            max_concurrent_executions=1,
        )
        self.backend.stay_running = True
        application = self.executor(routes, ledger=ledger, capacity=1)
        first_launcher = self.launcher(application, capacity=1)
        first_assignment = HOST.ReviewHostApplication._assignment(
            first_route, first_control,
        )
        first_input = HOST.ReviewHostApplication._model_task(first_task)
        outcome = []

        def launch_first():
            try:
                first_launcher.execute_idempotent(
                    first_assignment, first_input,
                    deadline_seconds=first_assignment.deadline_seconds,
                    max_output_tokens=first_assignment.max_output_tokens,
                )
            except Exception as error:  # expected bounded running result
                outcome.append(error)

        worker = threading.Thread(target=launch_first)
        worker.start()
        self.assertTrue(paused.wait(2))
        reconciled = self.launcher(application, capacity=1).reconcile(
            first_assignment, first_input,
            deadline_seconds=first_assignment.deadline_seconds,
            max_output_tokens=first_assignment.max_output_tokens,
        )
        self.assertEqual(reconciled, {"status": "unknown", "execution": None})
        second_assignment = HOST.ReviewHostApplication._assignment(
            second_route, second_control,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as full:
            self.launcher(application, capacity=1).execute_idempotent(
                second_assignment,
                HOST.ReviewHostApplication._model_task(second_task),
                deadline_seconds=second_assignment.deadline_seconds,
                max_output_tokens=second_assignment.max_output_tokens,
            )
        self.assertEqual(full.exception.code, "launcher.status")
        resume.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.backend.starts), 1)
        self.assertEqual(ledger.count_active(), 1)
        self.assertTrue(outcome)

    def test_reconcile_failure_preserves_paused_dispatch_barrier(self):
        first_task, first_control = HOST_TEST.response_request("fi-FI")
        second_task, second_control = HOST_TEST.response_request(
            "fi-FI", target="Kolmas rinnakkainen testivastaus.",
        )
        first_route = HOST_TEST.response_route(first_task)
        second_route = replace(
            HOST_TEST.response_route(second_task, agent="reviewer-native-2"),
            route_id="ordinary-fi-FI-after-reconcile-error",
        )
        routes = [first_route, second_route]
        paused, resume = threading.Event(), threading.Event()

        class PausingLedger(EXECUTOR.SQLiteExecutionLedger):
            def reserve_execute(inner_self, **values):
                reservation = super().reserve_execute(**values)
                if (reservation.owner
                        and reservation.execution_key == first_control["execution_key"]):
                    paused.set()
                    self.assertTrue(resume.wait(2))
                return reservation

        class FlakyReconcileBackend(FixtureBackend):
            def __init__(inner_self):
                super().__init__()
                inner_self.reconcile_failures = 1

            def reconcile(inner_self, *args, **kwargs):
                if inner_self.reconcile_failures:
                    inner_self.reconcile_failures -= 1
                    raise RuntimeError("synthetic temporary reconciliation failure")
                return super().reconcile(*args, **kwargs)

        backend = FlakyReconcileBackend()
        backend.stay_running = True
        ledger = PausingLedger(
            Path(self.temporary.name) / "dispatch-error-race.sqlite3",
            max_concurrent_executions=1,
        )
        application = self.executor(
            routes, backend=backend, ledger=ledger, capacity=1,
        )
        first_assignment = HOST.ReviewHostApplication._assignment(
            first_route, first_control,
        )
        first_input = HOST.ReviewHostApplication._model_task(first_task)
        outcome = []

        def launch_first():
            try:
                self.launcher(application, capacity=1).execute_idempotent(
                    first_assignment, first_input,
                    deadline_seconds=first_assignment.deadline_seconds,
                    max_output_tokens=first_assignment.max_output_tokens,
                )
            except Exception as error:  # expected bounded running result
                outcome.append(error)

        worker = threading.Thread(target=launch_first)
        worker.start()
        self.assertTrue(paused.wait(2))
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed):
            self.launcher(application, capacity=1).reconcile(
                first_assignment, first_input,
                deadline_seconds=first_assignment.deadline_seconds,
                max_output_tokens=first_assignment.max_output_tokens,
            )
        second_reconcile = self.launcher(application, capacity=1).reconcile(
            first_assignment, first_input,
            deadline_seconds=first_assignment.deadline_seconds,
            max_output_tokens=first_assignment.max_output_tokens,
        )
        self.assertEqual(second_reconcile["status"], "unknown")
        second_assignment = HOST.ReviewHostApplication._assignment(
            second_route, second_control,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed):
            self.launcher(application, capacity=1).execute_idempotent(
                second_assignment,
                HOST.ReviewHostApplication._model_task(second_task),
                deadline_seconds=second_assignment.deadline_seconds,
                max_output_tokens=second_assignment.max_output_tokens,
            )
        resume.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(backend.starts), 1)
        self.assertEqual(ledger.count_active(), 1)
        self.assertTrue(outcome)

    def test_authentication_precedes_body_and_backend(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        app = self.executor([route])
        statuses = []
        body = b"not-json"
        environ = {
            "PATH_INFO": EXECUTOR.PATH, "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https", "SERVER_NAME": "executor.example",
            "CONTENT_TYPE": "application/json; charset=utf-8",
            "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
            "HTTP_AUTHORIZATION": "Bearer wrong-token-with-at-least-32-characters",
        }
        response = b"".join(app(
            environ, lambda status, _headers: statuses.append(status),
        ))
        self.assertTrue(statuses[0].startswith("401"))
        self.assertEqual(json.loads(response)["error"]["code"],
                         "subagent_executor.authentication_rejected")
        self.assertEqual(self.backend.starts, [])

    def test_native_nested_source_injection_blocks_before_backend(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        model_input["input"]["quality_profile"]["source_text"] = "hidden source"
        launcher = self.launcher(self.executor([route]))
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as blocked:
            launcher.execute_idempotent(
                assignment, model_input,
                deadline_seconds=assignment.deadline_seconds,
                max_output_tokens=assignment.max_output_tokens,
            )
        self.assertEqual(blocked.exception.code, "launcher.status")
        self.assertEqual(self.backend.starts, [])

    def test_wrong_identity_phase_and_locale_block_before_backend(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        original = HOST.ReviewHostApplication._model_task(task)
        cases = (
            ("identity", replace(assignment, reviewer_agent_id="wrong-reviewer"),
             original),
            ("phase", replace(assignment, phase="source_fidelity"), original),
            ("locale", assignment, {
                **original,
                "input": {**original["input"],
                          "target": {**original["input"]["target"],
                                     "locale": "mt-MT"}},
            }),
        )
        for name, changed_assignment, model_input in cases:
            with self.subTest(name=name):
                ledger = EXECUTOR.SQLiteExecutionLedger(
                    Path(self.temporary.name) / f"route-{name}.sqlite3",
                    max_concurrent_executions=4,
                )
                with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as blocked:
                    self.launcher(self.executor([route], ledger=ledger)) \
                        .execute_idempotent(
                            changed_assignment, model_input,
                            deadline_seconds=changed_assignment.deadline_seconds,
                            max_output_tokens=changed_assignment.max_output_tokens,
                        )
                self.assertIn(blocked.exception.code, {
                    "launcher.authentication_rejected", "launcher.status",
                })
                self.assertEqual(ledger.count_active(), 0)
        self.assertEqual(self.backend.starts, [])

    def test_native_instruction_and_allowed_profile_fields_are_policy_bound(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        original = HOST.ReviewHostApplication._model_task(task)
        for name, mutation in (
            ("instruction", lambda value: value.update(
                system_instruction=value["system_instruction"]
                + " Original source document: SOURCE_SENTINEL",
            )),
            ("audience", lambda value: value["input"].update(
                audience="Original source document: SOURCE_SENTINEL",
            )),
            ("profile", lambda value: value["input"]["quality_profile"].update(
                prompt_version="injected-source-policy",
            )),
        ):
            with self.subTest(name=name):
                model_input = EXECUTOR._copy(original)
                mutation(model_input)
                ledger = EXECUTOR.SQLiteExecutionLedger(
                    Path(self.temporary.name) / f"policy-{name}.sqlite3",
                    max_concurrent_executions=4,
                )
                app = self.executor([route], ledger=ledger)
                with self.assertRaises(LAUNCHER.SubagentLauncherFailed):
                    self.launcher(app).execute_idempotent(
                        assignment, model_input,
                        deadline_seconds=assignment.deadline_seconds,
                        max_output_tokens=assignment.max_output_tokens,
                    )
                self.assertEqual(ledger.count_active(), 0)
        self.assertEqual(self.backend.starts, [])

    def test_wrong_backend_identity_and_usage_are_quarantined(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        for mutation in (
            lambda result: result["execution"].update(agent_id="writer"),
            lambda result: result["usage"].update(cost_units=10**9),
            lambda result: result["usage"].update(execute_request_sha256="0" * 64),
        ):
            with self.subTest(mutation=mutation):
                isolated = FixtureBackend()
                isolated.mutate = mutation
                ledger = EXECUTOR.SQLiteExecutionLedger(
                    Path(self.temporary.name) / (
                        "bad-" + hashlib.sha256(repr(mutation).encode()).hexdigest()
                        + ".sqlite3"
                    ), max_concurrent_executions=4,
                )
                app = self.executor([route], backend=isolated, ledger=ledger)
                with self.assertRaises(LAUNCHER.SubagentLauncherFailed):
                    self.launcher(app).execute_idempotent(
                        assignment, model_input,
                        deadline_seconds=assignment.deadline_seconds,
                        max_output_tokens=assignment.max_output_tokens,
                    )
                self.assertEqual(ledger.count_active(), 1)

    def test_changed_request_conflicts_without_second_start(self):
        task, control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        app = self.executor([route])
        launcher = self.launcher(app)
        assignment = HOST.ReviewHostApplication._assignment(route, control)
        model_input = HOST.ReviewHostApplication._model_task(task)
        first = launcher.execute_idempotent(
            assignment, model_input,
            deadline_seconds=assignment.deadline_seconds,
            max_output_tokens=assignment.max_output_tokens,
        )
        self.assertEqual(first["response"]["status"], "PASS")
        changed = EXECUTOR._copy(model_input)
        changed["input"]["candidate"] += " Muutos."
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as conflict:
            launcher.execute_idempotent(
                assignment, changed,
                deadline_seconds=assignment.deadline_seconds,
                max_output_tokens=assignment.max_output_tokens,
            )
        self.assertEqual(conflict.exception.code, "launcher.idempotency_conflict")
        self.assertEqual(len(self.backend.starts), 1)


if __name__ == "__main__":
    unittest.main()
