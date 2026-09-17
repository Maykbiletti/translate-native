"""Synthetic HTTPS-launcher fixtures; not native-language quality evidence."""

from __future__ import annotations

import hashlib
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import test_website_localization_subagent_host as HOST_TEST
import test_website_localization_subagent_host_runtime as RUNTIME_TEST
import test_website_localization_subagents as BASE


LAUNCHER = BASE.load(
    "test_website_localization_subagent_launcher_http_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_launcher_http.py",
)
HOST = HOST_TEST.HOST
HTTP = HOST_TEST.HTTP
TOKEN = "executor-bearer-token-with-at-least-32-characters"


class ExecutorTransport:
    """Host-owned execution-sidecar double with durable idempotency."""

    def __init__(self):
        self.calls = []
        self.executions = {}
        self.usage = {}
        self.execute_requests = {}
        self.lose_first_execute_reply = False
        self.return_running_once = False
        self.stay_running = False
        self.active = set()
        self.mutate_reply = None

    @staticmethod
    def _execution(assignment, task):
        locale = task["input"]["target"]["locale"]
        if task["schema"] == HOST.RESPONSE_REVIEW_SCHEMA:
            response = {
                "schema": HOST.RESPONSE_NATIVE_SCHEMA,
                "phase": task["phase"], "locale": locale,
                "status": "PASS", "confidence": "high",
                "findings": [], "uncertainties": [],
            }
        else:
            response = BASE.review(locale, task["phase"], confidence="high")
        return {
            "response": response,
            "execution_key": assignment["execution_key"],
            "phase": assignment["phase"],
            "reviewer_role": assignment["reviewer_role"],
            "agent_id": assignment["reviewer_agent_id"],
            "session_id": assignment["reviewer_session_id"],
            "model_id": assignment["model_id"],
            "model_version": assignment["model_version"],
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
        }

    @staticmethod
    def _result(status, body):
        raw = json.dumps(
            body, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return LAUNCHER.HTTPResult(
            status,
            (("Content-Type", "application/json; charset=utf-8"),
             ("Content-Length", str(len(raw)))),
            raw,
        )

    def post(self, _url, headers, body, *, timeout):
        request = json.loads(body)
        self.calls.append((request, dict(headers), timeout))
        key = request["assignment"]["execution_key"]
        operation = request["operation"]
        if operation == "execute":
            prior = self.execute_requests.get(key)
            digest = hashlib.sha256(body).hexdigest()
            if prior is not None and prior != digest:
                return self._result(409, {})
            if (prior is None and key not in self.active
                    and len(self.active) >= request["budgets"][
                        "max_concurrent_executions"
                    ]):
                return self._result(429, {})
            self.execute_requests[key] = digest
            if key not in self.executions:
                self.executions[key] = self._execution(
                    request["assignment"], request["model_input"],
                )
                encoded_input = json.dumps(
                    request["model_input"], ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
                self.usage[key] = {
                    "execute_request_sha256": request["request_sha256"],
                    "cost_unit": request["budgets"]["cost_unit"],
                    "cost_units": 7, "input_bytes": len(encoded_input),
                    "output_tokens": 1,
                }
            if self.lose_first_execute_reply:
                self.lose_first_execute_reply = False
                raise LAUNCHER.SubagentLauncherFailed(
                    "launcher.network", retryable=True,
                )
            status = ("running" if self.stay_running or self.return_running_once
                      else "completed")
            self.return_running_once = False
        else:
            status = ("running" if self.stay_running and key in self.executions
                      else "completed" if key in self.executions else "not_started")
        if status == "running":
            self.active.add(key)
        elif status in {"completed", "not_started"}:
            self.active.discard(key)
        reply = {
            "schema": LAUNCHER.RESPONSE_SCHEMA,
            "operation": operation,
            "launcher_id": request["launcher_id"],
            "launcher_version": request["launcher_version"],
            "executor_id": request["executor_id"],
            "execution_key": key,
            "request_sha256": request["request_sha256"],
            "status": status,
            "execution": self.executions.get(key) if status == "completed" else None,
            "usage": self.usage.get(key) if status == "completed" else None,
        }
        if self.mutate_reply:
            self.mutate_reply(reply)
        return self._result(202 if status == "running" else 200, reply)


class HTTPLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.transport = ExecutorTransport()
        self.launcher = LAUNCHER.HTTPSSubagentLauncher(
            "http://127.0.0.1/v1/subagent-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            launcher_id="deployment-review-launcher",
            launcher_version="launcher-1", executor_id="executor-1",
            transport=self.transport, request_timeout_seconds=60,
            poll_interval_milliseconds=10, max_status_polls=3,
            allow_loopback_http=True, sleeper=lambda _seconds: None,
        )

    def application(self, routes):
        signer = HOST.HMACAttestationSigner(HOST_TEST.SECRET, HOST_TEST.KEY_ID)
        ledger = HOST.SQLiteReviewLedger(
            Path(self.temporary.name) / "reviews.sqlite3", lease_seconds=65,
        )
        return HOST.ReviewHostApplication(
            host_id=HOST_TEST.HOST_ID, bearer_token=HOST_TEST.TOKEN,
            signer=signer, policy=HOST.PinnedReviewPolicy(routes),
            ledger=ledger, launcher=self.launcher, allow_loopback_http=True,
        )

    @staticmethod
    def client(application):
        return HTTP.HTTPSReviewHost(
            "http://127.0.0.1/v1/subagent-reviews",
            lambda: {"Authorization": "Bearer " + HOST_TEST.TOKEN},
            HOST_TEST.Verifier(), host_id=HOST_TEST.HOST_ID,
            transport=HOST_TEST.WSGITransport(application),
            allow_loopback_http=True,
        )

    def test_finnish_response_is_source_blind_through_real_launcher(self):
        task, control = HOST_TEST.response_request("fi-FI")
        result = self.client(self.application([
            HOST_TEST.response_route(task),
        ])).run_isolated(task, control=control)
        self.assertEqual(result["response"]["status"], "PASS")
        request, headers, _timeout = self.transport.calls[0]
        serialized = json.dumps(request, ensure_ascii=False)
        self.assertNotIn("source", request["model_input"]["input"])
        self.assertNotIn("writer-session", serialized)
        self.assertNotIn('"creator_id"', serialized)
        self.assertNotIn(TOKEN, serialized)
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["Idempotency-Key"], control["execution_key"])
        self.assertEqual(request["assignment"]["reviewer_role"],
                         "target-native-reviewer")

    def test_maltese_translation_uses_ordered_isolated_phases(self):
        routes, _captures = HOST_TEST.website_routes(["mt-MT"])
        provider = BASE.adapter(self.client(self.application(routes)))
        result = BASE.HostSubagentTests().execute(provider, "mt-MT")
        self.assertTrue(result["release_required"])
        execute = [call[0] for call in self.transport.calls
                   if call[0]["operation"] == "execute"]
        self.assertEqual([item["assignment"]["phase"] for item in execute],
                         ["target_native", "source_fidelity"])
        native, fidelity = execute
        self.assertNotIn("source", native["model_input"]["input"])
        self.assertEqual(fidelity["model_input"]["input"]["source"]["text"],
                         "Build your business with BLUN.")
        self.assertNotEqual(native["assignment"]["reviewer_agent_id"],
                            fidelity["assignment"]["reviewer_agent_id"])
        self.assertNotEqual(native["assignment"]["reviewer_session_id"],
                            fidelity["assignment"]["reviewer_session_id"])

    def test_lost_start_reply_reconciles_without_second_model_start(self):
        task, control = HOST_TEST.response_request("fi-FI")
        self.transport.lose_first_execute_reply = True
        client = self.client(self.application([HOST_TEST.response_route(task)]))
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as lost:
            client.run_isolated(task, control=control)
        self.assertTrue(lost.exception.retryable)
        mapped = HOST._launcher_blocked(
            LAUNCHER.SubagentLauncherFailed("launcher.network", retryable=True),
            "launcher_unknown",
        )
        self.assertEqual(mapped.code, "review_host.launcher_network")
        recovered = client.run_isolated(task, control=control)
        self.assertEqual(recovered["response"]["status"], "PASS")
        operations = [call[0]["operation"] for call in self.transport.calls]
        self.assertEqual(operations, ["execute", "reconcile"])
        self.assertEqual(len(self.transport.execute_requests), 1)
        self.assertEqual(
            recovered["receipt"]["usage"]["execute_request_sha256"],
            next(iter(self.transport.usage.values()))["execute_request_sha256"],
        )

    def test_recovery_rejects_usage_not_bound_to_original_execute(self):
        task, control = HOST_TEST.response_request("fi-FI")
        self.transport.lose_first_execute_reply = True
        client = self.client(self.application([HOST_TEST.response_route(task)]))
        with self.assertRaises(HTTP.HTTPReviewHostFailed):
            client.run_isolated(task, control=control)
        self.transport.usage[control["execution_key"]][
            "execute_request_sha256"
        ] = "0" * 64
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as blocked:
            client.run_isolated(task, control=control)
        self.assertFalse(blocked.exception.retryable)

    def test_running_execution_is_polled_with_a_fixed_bound(self):
        task, control = HOST_TEST.response_request("fi-FI")
        self.transport.return_running_once = True
        result = self.client(self.application([
            HOST_TEST.response_route(task),
        ])).run_isolated(task, control=control)
        self.assertEqual(result["response"]["status"], "PASS")
        self.assertEqual([call[0]["operation"] for call in self.transport.calls],
                         ["execute", "reconcile"])

    def test_wrong_executor_identity_blocks_before_host_attestation(self):
        task, control = HOST_TEST.response_request("fi-FI")
        self.transport.mutate_reply = lambda reply: reply.update(
            executor_id="wrong-executor",
        )
        client = self.client(self.application([HOST_TEST.response_route(task)]))
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as blocked:
            client.run_isolated(task, control=control)
        self.assertFalse(blocked.exception.retryable)
        mapped = HOST._launcher_blocked(
            LAUNCHER.SubagentLauncherFailed(
                "launcher.response_invalid", retryable=False,
            ),
            "launcher_unknown",
        )
        self.assertEqual(mapped.code, "review_host.launcher_response_invalid")

    def _assert_execution_mutation_blocks(self, mutation):
        task, control = HOST_TEST.response_request("fi-FI")

        def mutate(reply):
            mutation(reply["execution"])

        self.transport.mutate_reply = mutate
        client = self.client(self.application([HOST_TEST.response_route(task)]))
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as blocked:
            client.run_isolated(task, control=control)
        self.assertFalse(blocked.exception.retryable)

    def test_wrong_reviewer_identity_blocks_before_host_attestation(self):
        self._assert_execution_mutation_blocks(
            lambda execution: execution.update(agent_id="writer"),
        )

    def test_wrong_phase_blocks_before_host_attestation(self):
        self._assert_execution_mutation_blocks(
            lambda execution: execution.update(phase="source_fidelity"),
        )

    def test_wrong_locale_blocks_before_host_attestation(self):
        self._assert_execution_mutation_blocks(
            lambda execution: execution["response"].update(locale="mt-MT"),
        )

    def test_usage_must_be_complete_typed_and_within_cost_budget(self):
        task, control = HOST_TEST.response_request("fi-FI")
        assignment = HOST.ReviewHostApplication._assignment(
            HOST_TEST.response_route(task), control,
        )
        mutations = (
            lambda reply: reply.pop("usage"),
            lambda reply: reply["usage"].update(cost_units=True),
            lambda reply: reply["usage"].update(cost_units=100001),
            lambda reply: reply["usage"].update(input_bytes=0),
            lambda reply: reply["usage"].update(output_tokens=True),
            lambda reply: reply["usage"].update(output_tokens=4097),
            lambda reply: reply["usage"].update(
                execute_request_sha256="0" * 64,
            ),
            lambda reply: reply["usage"].update(cost_unit="foreign-unit"),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                transport = ExecutorTransport()
                transport.mutate_reply = mutation
                launcher = LAUNCHER.HTTPSSubagentLauncher(
                    "http://127.0.0.1/v1/subagent-executions",
                    lambda: {"Authorization": "Bearer " + TOKEN},
                    launcher_id="deployment-review-launcher",
                    launcher_version="launcher-1", executor_id="executor-1",
                    transport=transport, allow_loopback_http=True,
                    max_input_bytes=2000000,
                    cost_unit="deployment-cost-unit", max_cost_units=100000,
                )
                with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as blocked:
                    launcher.execute_idempotent(
                        assignment, task, deadline_seconds=60,
                        max_output_tokens=4096,
                    )
                self.assertFalse(blocked.exception.retryable)

    def test_input_budget_blocks_before_authentication_or_transport(self):
        task, control = HOST_TEST.response_request("fi-FI")
        assignment = HOST.ReviewHostApplication._assignment(
            HOST_TEST.response_route(task), control,
        )
        assignment = replace(assignment, max_input_bytes=1024)
        authentication_calls = []
        launcher = LAUNCHER.HTTPSSubagentLauncher(
            "http://127.0.0.1/v1/subagent-executions",
            lambda: authentication_calls.append(True) or {
                "Authorization": "Bearer " + TOKEN,
            },
            launcher_id="deployment-review-launcher",
            launcher_version="launcher-1", executor_id="executor-1",
            transport=self.transport, allow_loopback_http=True,
            max_input_bytes=1024,
        )
        oversized = HOST._copy(task)
        oversized["input"]["candidate"] = "x" * 2000
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as blocked:
            launcher.execute_idempotent(
                assignment, oversized, deadline_seconds=60,
                max_output_tokens=4096,
            )
        self.assertEqual(blocked.exception.code, "launcher.input_budget")
        self.assertEqual(authentication_calls, [])
        self.assertEqual(self.transport.calls, [])

    def test_concurrency_capacity_blocks_second_physical_start(self):
        class BlockingTransport(ExecutorTransport):
            def __init__(self):
                super().__init__()
                self.entered, self.release = threading.Event(), threading.Event()

            def post(self, url, headers, body, *, timeout):
                request = json.loads(body)
                if request["operation"] == "execute" and not self.entered.is_set():
                    self.entered.set()
                    self.release.wait(2)
                return super().post(url, headers, body, timeout=timeout)

        task, control = HOST_TEST.response_request("fi-FI")
        assignment = HOST.ReviewHostApplication._assignment(
            HOST_TEST.response_route(task), control,
        )
        transport = BlockingTransport()
        launcher = LAUNCHER.HTTPSSubagentLauncher(
            "http://127.0.0.1/v1/subagent-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            launcher_id="deployment-review-launcher",
            launcher_version="launcher-1", executor_id="executor-1",
            transport=transport, allow_loopback_http=True,
            max_concurrent_executions=1,
        )
        outcomes = []

        def first():
            try:
                outcomes.append(launcher.execute_idempotent(
                    assignment, task, deadline_seconds=60,
                    max_output_tokens=4096,
                ))
            except Exception as error:  # pragma: no cover - assertion below
                outcomes.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(transport.entered.wait(1))
        second = replace(
            assignment, execution_key="f" * 64,
            reviewer_session_id="review:" + "e" * 64,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as blocked:
            launcher.execute_idempotent(
                second, task, deadline_seconds=60, max_output_tokens=4096,
            )
        self.assertEqual(blocked.exception.code, "launcher.capacity")
        self.assertTrue(blocked.exception.retryable)
        self.assertEqual(len(transport.calls), 0)
        transport.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(len(transport.calls), 1)

    def test_running_or_ambiguous_remote_job_retains_capacity(self):
        task, control = HOST_TEST.response_request("fi-FI")
        assignment = HOST.ReviewHostApplication._assignment(
            HOST_TEST.response_route(task), control,
        )
        transport = ExecutorTransport()
        transport.stay_running = True
        launcher = LAUNCHER.HTTPSSubagentLauncher(
            "http://127.0.0.1/v1/subagent-executions",
            lambda: {"Authorization": "Bearer " + TOKEN},
            launcher_id="deployment-review-launcher",
            launcher_version="launcher-1", executor_id="executor-1",
            transport=transport, allow_loopback_http=True,
            poll_interval_milliseconds=10, max_status_polls=1,
            max_concurrent_executions=1, sleeper=lambda _seconds: None,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as running:
            launcher.execute_idempotent(
                assignment, task, deadline_seconds=60,
                max_output_tokens=4096,
            )
        self.assertEqual(running.exception.code, "launcher.running")
        second = replace(
            assignment, execution_key="f" * 64,
            reviewer_session_id="review:" + "e" * 64,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as full:
            launcher.execute_idempotent(
                second, task, deadline_seconds=60, max_output_tokens=4096,
            )
        self.assertEqual(full.exception.code, "launcher.capacity")
        self.assertEqual(
            [call[0]["operation"] for call in transport.calls],
            ["execute", "reconcile"],
        )
        transport.stay_running = False
        recovered = launcher.reconcile(
            assignment, task, deadline_seconds=60, max_output_tokens=4096,
        )
        self.assertEqual(recovered["status"], "completed")
        completed = launcher.execute_idempotent(
            second, task, deadline_seconds=60, max_output_tokens=4096,
        )
        self.assertEqual(completed["execution_key"], second.execution_key)

    def test_remote_executor_cap_survives_launcher_restart(self):
        task, control = HOST_TEST.response_request("fi-FI")
        assignment = HOST.ReviewHostApplication._assignment(
            HOST_TEST.response_route(task), control,
        )
        transport = ExecutorTransport()
        transport.stay_running = True

        def fresh_launcher():
            return LAUNCHER.HTTPSSubagentLauncher(
                "http://127.0.0.1/v1/subagent-executions",
                lambda: {"Authorization": "Bearer " + TOKEN},
                launcher_id="deployment-review-launcher",
                launcher_version="launcher-1", executor_id="executor-1",
                transport=transport, allow_loopback_http=True,
                poll_interval_milliseconds=10, max_status_polls=1,
                max_concurrent_executions=1, sleeper=lambda _seconds: None,
            )

        with self.assertRaises(LAUNCHER.SubagentLauncherFailed):
            fresh_launcher().execute_idempotent(
                assignment, task, deadline_seconds=60, max_output_tokens=4096,
            )
        second = replace(
            assignment, execution_key="f" * 64,
            reviewer_session_id="review:" + "e" * 64,
        )
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as full:
            fresh_launcher().execute_idempotent(
                second, task, deadline_seconds=60, max_output_tokens=4096,
            )
        self.assertEqual(full.exception.code, "launcher.status")
        self.assertTrue(full.exception.retryable)
        self.assertNotIn(second.execution_key, transport.execute_requests)

    def test_default_transport_enforces_wall_time_on_slow_stream(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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
        with self.assertRaises(LAUNCHER.SubagentLauncherFailed) as blocked:
            LAUNCHER.URLTransport().post(
                f"http://127.0.0.1:{server.server_address[1]}/slow",
                {"Content-Type": "application/json"}, b"{}", timeout=0.2,
            )
        elapsed = time.monotonic() - started
        self.assertTrue(blocked.exception.retryable)
        self.assertLess(elapsed, 0.8)

    def test_launcher_rejects_credentials_in_body_and_unsafe_endpoint(self):
        with self.assertRaises(ValueError):
            LAUNCHER.HTTPSSubagentLauncher(
                "https://user:secret@example.test/reviews",
                lambda: {"Authorization": "Bearer " + TOKEN},
                launcher_id="launcher", launcher_version="v1",
                executor_id="executor",
            )
        with self.assertRaises(ValueError):
            LAUNCHER.HTTPSSubagentLauncher(
                "http://example.test/reviews",
                lambda: {"Authorization": "Bearer " + TOKEN},
                launcher_id="launcher", launcher_version="v1",
                executor_id="executor", allow_loopback_http=True,
            )

    def test_factory_requires_owner_only_digest_pinned_token(self):
        token_path = Path(self.temporary.name) / "executor.token"
        token_path.write_text(TOKEN, encoding="ascii")
        os.chmod(token_path, 0o600)
        settings = {
            "schema": LAUNCHER.SETTINGS_SCHEMA,
            "launcher_id": "deployment-review-launcher",
            "launcher_version": "launcher-1", "executor_id": "executor-1",
            "endpoint": "https://executor.example/v1/subagent-executions",
            "authentication": {
                "scheme": "bearer", "token_file": str(token_path),
                "token_sha256": hashlib.sha256(TOKEN.encode("ascii")).hexdigest(),
            },
            "request_timeout_seconds": 60,
            "poll_interval_milliseconds": 250, "max_status_polls": 120,
            "max_input_bytes": 2000000,
            "cost_unit": "deployment-cost-unit", "max_cost_units": 100000,
            "max_concurrent_executions": 4,
            "allow_loopback_http": False,
        }
        launcher = LAUNCHER.build_launcher(settings)
        self.assertEqual(launcher.launcher_id, "deployment-review-launcher")
        settings["authentication"]["token_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            LAUNCHER.build_launcher(settings)

    def test_actual_protected_runtime_loads_and_binds_standard_factory(self):
        root = Path(self.temporary.name)
        if os.name != "nt":
            root.chmod(0o700)
        factory = root / "website_localization_subagent_launcher_http.py"
        factory.write_bytes((
            BASE.ROOT / "integrations" /
            "website_localization_subagent_launcher_http.py"
        ).read_bytes())
        token = root / "executor.token"
        token.write_text(TOKEN, encoding="ascii")
        host_token = root / "host.token"
        host_token.write_text(HOST_TEST.TOKEN, encoding="ascii")
        secret = root / "host.secret"
        secret.write_text(HOST_TEST.SECRET, encoding="ascii")
        for path in (factory, token, host_token, secret):
            if os.name != "nt":
                path.chmod(0o600)
        launcher_document = {
            "schema": RUNTIME_TEST.RUNTIME.LAUNCHER_SCHEMA,
            "launcher_id": "deployment-review-launcher",
            "launcher_version": "launcher-1",
            "settings": {
                "schema": LAUNCHER.SETTINGS_SCHEMA,
                "launcher_id": "deployment-review-launcher",
                "launcher_version": "launcher-1", "executor_id": "executor-1",
                "endpoint": "https://executor.example/v1/subagent-executions",
                "authentication": {
                    "scheme": "bearer", "token_file": str(token),
                    "token_sha256": hashlib.sha256(
                        TOKEN.encode("ascii"),
                    ).hexdigest(),
                },
                "request_timeout_seconds": 60,
                "poll_interval_milliseconds": 250, "max_status_polls": 120,
                "max_input_bytes": 2000000,
                "cost_unit": "deployment-cost-unit",
                "max_cost_units": 100000,
                "max_concurrent_executions": 4,
                "allow_loopback_http": False,
            },
        }
        launcher_config = root / "launcher.json"
        launcher_config.write_text(json.dumps(launcher_document), encoding="utf-8")
        if os.name != "nt":
            launcher_config.chmod(0o600)
        task, _control = HOST_TEST.response_request("fi-FI")
        route = HOST_TEST.response_route(task)
        host_document = {
            "schema": RUNTIME_TEST.RUNTIME.CONFIG_SCHEMA,
            "host_id": HOST_TEST.HOST_ID, "allow_loopback_http": True,
            "authentication": {"scheme": "bearer", "token_file": str(host_token)},
            "attestation": {
                "algorithm": "hmac-sha256", "key_id": HOST_TEST.KEY_ID,
                "secret_file": str(secret),
            },
            "ledger": {
                "path": str(root / "runtime.sqlite3"), "lease_seconds": 65,
            },
            "launcher": {
                "factory_file": str(factory), "factory_callable": "build_launcher",
                "factory_sha256": hashlib.sha256(factory.read_bytes()).hexdigest(),
                "launcher_id": "deployment-review-launcher",
                "launcher_version": "launcher-1",
                "config_file": str(launcher_config),
            },
            "routes": [RUNTIME_TEST.RuntimeTests.route_dict(route)],
        }
        host_config = root / "host.json"
        host_config.write_text(json.dumps(host_document), encoding="utf-8")
        if os.name != "nt":
            host_config.chmod(0o600)
        runtime = RUNTIME_TEST.RUNTIME.open_review_host_runtime(
            host_config, initialize_ledger=True,
        )
        try:
            self.assertEqual(runtime.launcher.launcher_id,
                             "deployment-review-launcher")
        finally:
            runtime.close()
        launcher_document["settings"]["max_cost_units"] = 99999
        launcher_config.write_text(json.dumps(launcher_document), encoding="utf-8")
        if os.name != "nt":
            launcher_config.chmod(0o600)
        with self.assertRaisesRegex(
                RUNTIME_TEST.RUNTIME.ReviewHostRuntimeError,
                "deployment binding changed"):
            RUNTIME_TEST.RUNTIME.open_review_host_runtime(host_config)


if __name__ == "__main__":
    unittest.main()
