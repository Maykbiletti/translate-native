from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
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


HTTP = load(
    "blun_test_website_localization_benchmark_watcher_recovery_http",
    ROOT / "integrations"
    / "website_localization_benchmark_watcher_recovery_http.py",
)
RUNTIME = HTTP._RUNTIME
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
        digest = hashlib.sha256(RUNNER._CLIENT._canonical(request)).hexdigest()
        return Rearm(digest, {
            "schema": RUNNER._CONTROL.RECEIPT_SCHEMA,
            "request_sha256": digest,
            "previous_state": "failed",
            "previous_attempts": kwargs["expected_attempts"],
            "previous_error_code": kwargs["expected_error_code"],
            "failed_at": kwargs["expected_failed_at"],
            "state": "pending", "rearmed_at": 1500.0,
        })


class RecoveryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = Client()
        self.runtime = RUNTIME.open_durable_benchmark_watcher_recovery(
            Path(self.directory.name) / "recovery.sqlite3",
            self.client, worker_id="operator-1", clock=time.time,
            lease_seconds=0.2, base_delay_seconds=0.1,
            max_delay_seconds=0.1, maximum_wait_seconds=0.02,
            max_attempts=8,
        )
        self.auth_calls = []

        def authenticate(request):
            self.auth_calls.append(request)
            scope = {
                HTTP.START_PATH: HTTP.START_SCOPE,
                HTTP.STATUS_PATH: HTTP.STATUS_SCOPE,
                HTTP.READINESS_PATH: HTTP.READINESS_SCOPE,
                HTTP.OPENAPI_PATH: HTTP.OPENAPI_SCOPE,
            }[request["path"]]
            return {
                "schema": HTTP.PRINCIPAL_SCHEMA,
                "operator_id": "operator-1", "credential_id": "credential-1",
                "credential_version": "v1", "scope": scope,
            }

        self.app = HTTP.BenchmarkWatcherRecoveryHTTPApplication(
            self.runtime, authenticate,
        )

    def tearDown(self):
        try:
            self.runtime.close(worker_timeout_seconds=2)
        finally:
            self.directory.cleanup()

    def call(self, method, path, payload=None, *, headers=None, scheme="https",
             query="", raw=None, content_type="application/json"):
        body = raw if raw is not None else (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else b""
        )
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path,
            "QUERY_STRING": query, "wsgi.url_scheme": scheme,
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)) if body else "",
        }
        if body:
            environ["CONTENT_TYPE"] = content_type
        for name, value in (headers or {}).items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = status
            captured["headers"] = dict(response_headers)

        response = b"".join(self.app(environ, start_response))
        return int(captured["status"].split()[0]), json.loads(response), captured

    def start(self, operation="operator-remediation-1"):
        payload = {"schema": HTTP.START_REQUEST_SCHEMA, "operation_id": operation}
        return self.call(
            "POST", HTTP.START_PATH, payload,
            headers={"Idempotency-Key": operation, "Authorization": "Bearer x"},
        )

    def test_start_authenticates_exact_bytes_before_parse_and_starts_worker(self):
        with mock.patch.object(self.runtime, "start_worker") as start_worker:
            status, result, captured = self.start()
        self.assertEqual(status, 202)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"]["state"], "pending")
        self.assertNotIn("operator-remediation-1", repr(result))
        self.assertEqual(self.auth_calls[0]["body_sha256"], hashlib.sha256(
            json.dumps({
                "schema": HTTP.START_REQUEST_SCHEMA,
                "operation_id": "operator-remediation-1",
            }).encode("utf-8")
        ).hexdigest())
        start_worker.assert_called_once_with()
        self.assertEqual(captured["headers"]["Cache-Control"], "no-store")

    def test_authentication_failure_precedes_malformed_json_and_runtime(self):
        app = HTTP.BenchmarkWatcherRecoveryHTTPApplication(
            self.runtime, lambda _request: (_ for _ in ()).throw(RuntimeError()),
        )
        original, self.app = self.app, app
        try:
            with mock.patch.object(self.runtime, "start") as start:
                status, result, _ = self.call(
                    "POST", HTTP.START_PATH, raw=b"{broken",
                    headers={"Idempotency-Key": "operator-remediation-1"},
                )
        finally:
            self.app = original
        self.assertEqual(status, 503)
        self.assertEqual(
            result["error_code"],
            "benchmark_watcher.recovery_http.authentication_unavailable",
        )
        start.assert_not_called()

    def test_route_specific_scope_is_mandatory(self):
        def wrong_scope(_request):
            return {
                "schema": HTTP.PRINCIPAL_SCHEMA,
                "operator_id": "operator-1", "credential_id": "credential-1",
                "credential_version": "v1", "scope": HTTP.STATUS_SCOPE,
            }
        self.app = HTTP.BenchmarkWatcherRecoveryHTTPApplication(
            self.runtime, wrong_scope,
        )
        status, result, _ = self.start()
        self.assertEqual(status, 401)
        self.assertEqual(result["error_code"], "benchmark_watcher.recovery_http.authentication_failed")
        with self.assertRaisesRegex(
            RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked, "runner_blocked",
        ):
            self.runtime.status()

    def test_idempotency_replay_and_changed_operation_fail_closed(self):
        with mock.patch.object(self.runtime, "start_worker"):
            first = self.start()[1]
            repeated = self.start()[1]
            status, result, _ = self.start("operator-remediation-2")
        self.assertEqual(first, repeated)
        self.assertEqual(status, 409)
        self.assertEqual(result["error_code"], "benchmark_watcher.recovery_http.runtime_blocked")

    def test_missing_or_wrong_idempotency_key_cannot_start(self):
        payload = {"schema": HTTP.START_REQUEST_SCHEMA, "operation_id": "operation-1"}
        for value in (None, "operation-2"):
            headers = {} if value is None else {"Idempotency-Key": value}
            status, result, _ = self.call("POST", HTTP.START_PATH, payload, headers=headers)
            self.assertEqual(status, 400)
            self.assertEqual(result["error_code"], "benchmark_watcher.recovery_http.idempotency_key_invalid")
        with self.assertRaisesRegex(
            RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked, "runner_blocked",
        ):
            self.runtime.status()

    def test_status_and_readiness_are_content_free_and_independently_validated(self):
        status, readiness, _ = self.call("GET", HTTP.READINESS_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(readiness["readiness"]["worker_state"], "unmanaged")
        with mock.patch.object(self.runtime, "start_worker"):
            self.start("private-operation-name")
            status, result, _ = self.call("GET", HTTP.STATUS_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"]["phase"], "openapi")
        rendered = repr(result)
        for forbidden in (
            "private-operation-name", "control.example", "credential-1",
            "provider", "locale", "model",
        ):
            self.assertNotIn(forbidden, rendered)
        with mock.patch.object(self.runtime, "status", return_value={"state": "succeeded"}):
            status, result, _ = self.call("GET", HTTP.STATUS_PATH)
        self.assertEqual(status, 503)
        self.assertEqual(result["error_code"], "benchmark_watcher.recovery_http.runtime_response_invalid")

    def test_openapi_is_exact_origin_free_and_state_independent(self):
        status, result, _ = self.call("GET", HTTP.OPENAPI_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(result["contract_sha256"], HTTP._sha(HTTP._contract()))
        self.assertEqual(result["openapi_sha256"], HTTP._sha(result["openapi"]))
        self.assertEqual(set(result["openapi"]["paths"]), {
            HTTP.START_PATH, HTTP.STATUS_PATH, HTTP.READINESS_PATH, HTTP.OPENAPI_PATH,
        })
        self.assertNotIn("servers", result["openapi"])
        with self.assertRaisesRegex(
            RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked, "runner_blocked",
        ):
            self.runtime.status()

    def test_transport_and_json_boundaries_fail_closed(self):
        cases = [
            self.call("GET", HTTP.READINESS_PATH, scheme="http"),
            self.call("GET", HTTP.READINESS_PATH, query="x=1"),
            self.call("GET", "/unknown"),
            self.call("POST", HTTP.START_PATH, raw=b"{}", content_type="text/plain"),
            self.call("POST", HTTP.START_PATH, raw=b'{"schema":1,"schema":2}', headers={"Idempotency-Key": "x"}),
        ]
        self.assertEqual([case[0] for case in cases], [400, 400, 404, 415, 400])
        with self.assertRaisesRegex(
            RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked, "runner_blocked",
        ):
            self.runtime.status()

    def test_real_worker_completes_after_committed_http_start(self):
        status, result, _ = self.start("operator-remediation-complete")
        self.assertEqual(status, 202)
        self.assertEqual(result["status"]["state"], "pending")
        deadline = time.monotonic() + 3
        while self.runtime.worker_state == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        status, result, _ = self.call("GET", HTTP.STATUS_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"]["state"], "succeeded")
        self.assertEqual(self.client.calls, ["openapi", "status", "rearm"])


if __name__ == "__main__":
    unittest.main()
