from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTROL = load(
    "blun_test_website_localization_benchmark_watcher_control",
    ROOT / "integrations" / "website_localization_benchmark_watcher_control.py",
)

CAMPAIGN_ID = "benchmark-campaign-" + "a" * 64
POLICY_SHA256 = "b" * 64
SUITE_SHA256 = "c" * 64


class Client:
    expected_campaign_id = CAMPAIGN_ID
    expected_policy_sha256 = POLICY_SHA256
    expected_suite_sha256 = SUITE_SHA256

    def report(self):
        raise AssertionError("operator control must not use the network client")


class BenchmarkWatcherControlTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.watcher = CONTROL._WATCHER.DurableBenchmarkReportWatcher(
            self.connection, Client(), lease_seconds=10,
            base_delay_seconds=5, max_delay_seconds=20, max_attempts=3,
        )
        self.fail_watcher()
        self.requests = []
        self.controller = CONTROL.DurableBenchmarkWatcherRearmController(
            self.watcher,
        )
        self.app = CONTROL.BenchmarkWatcherControlHTTPApplication(
            self.controller,
            lambda request: self.requests.append(request) or self.principal(),
            clock=lambda: 200,
        )

    def tearDown(self):
        self.connection.close()

    def fail_watcher(self, *, attempts=3):
        self.connection.execute("""
            UPDATE benchmark_report_watcher
            SET state = 'failed', attempts = ?, next_attempt_at = 100,
                lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL,
                last_error_code = 'benchmark_client.network', updated_at = 100
            WHERE singleton = 1
        """, (attempts,))
        self.connection.commit()

    @staticmethod
    def principal(**overrides):
        value = {
            "schema": CONTROL.PRINCIPAL_SCHEMA,
            "operator_id": "operator-1",
            "credential_id": "benchmark-control-1",
            "credential_version": "2026-09-16",
            "scope": "benchmark-watcher:rearm",
        }
        value.update(overrides)
        return value

    @staticmethod
    def request(**overrides):
        value = {
            "schema": CONTROL.REQUEST_SCHEMA,
            "request_id": "rearm-request-1",
            "expected_attempts": 3,
            "expected_failed_at": 100,
            "expected_error_code": "benchmark_client.network",
        }
        value.update(overrides)
        return value

    def call(self, payload=None, **overrides):
        payload = self.request() if payload is None else payload
        body = (
            payload if isinstance(payload, bytes)
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        captured = {}
        request_id = (
            payload.get("request_id")
            if isinstance(payload, dict) else "rearm-request-1"
        )
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": CONTROL.REARM_PATH,
            "QUERY_STRING": "",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_AUTHORIZATION": "Bearer private-control-token",
            "HTTP_IDEMPOTENCY_KEY": request_id,
        }
        environ.update(overrides)
        encoded = b"".join(self.app(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=dict(headers),
            ),
        ))
        return captured["status"], captured["headers"], json.loads(encoded), encoded

    def call_status(self, **overrides):
        captured = {}
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": CONTROL.STATUS_PATH,
            "QUERY_STRING": "",
            "wsgi.url_scheme": "https",
            "wsgi.input": io.BytesIO(b""),
            "HTTP_AUTHORIZATION": "Bearer private-status-token",
        }
        environ.update(overrides)
        encoded = b"".join(self.app(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=dict(headers),
            ),
        ))
        return captured["status"], captured["headers"], json.loads(encoded), encoded

    def test_authenticated_status_returns_only_exact_failed_generation(self):
        self.app.authenticator = lambda request: (
            self.requests.append(request)
            or self.principal(scope=CONTROL.STATUS_SCOPE)
        )

        status, headers, payload, encoded = self.call_status()

        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["schema"], CONTROL.STATUS_RESPONSE_SCHEMA)
        self.assertEqual(payload["status"], {
            "schema": CONTROL.STATUS_SCHEMA,
            "checked_at": 200.0,
            "state": "failed",
            "rearmable": True,
            "generation": {
                "attempts": 3,
                "failed_at": 100.0,
                "error_code": "benchmark_client.network",
            },
        })
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(CAMPAIGN_ID, encoded.decode("utf-8"))
        self.assertNotIn(POLICY_SHA256, encoded.decode("utf-8"))
        self.assertNotIn(SUITE_SHA256, encoded.decode("utf-8"))
        self.assertEqual(self.requests, [{
            "schema": CONTROL.AUTH_REQUEST_SCHEMA,
            "method": "GET",
            "path": CONTROL.STATUS_PATH,
            "headers": [["authorization", "Bearer private-status-token"]],
            "body_sha256": hashlib.sha256(b"").hexdigest(),
        }])

        self.connection.execute("""
            UPDATE benchmark_report_watcher
            SET state = 'pending', attempts = 0, next_attempt_at = 200,
                last_error_code = NULL, updated_at = 200
            WHERE singleton = 1
        """)
        self.connection.commit()
        payload = self.call_status()[2]["status"]
        self.assertFalse(payload["rearmable"])
        self.assertIsNone(payload["generation"])

    def test_status_scope_and_body_are_enforced_before_store_access(self):
        reads = []
        original = self.controller.status

        def observed_status(*, now):
            reads.append(now)
            return original(now=now)

        self.controller.status = observed_status
        self.app.authenticator = lambda request: self.principal()
        status, _, payload, _ = self.call_status()
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(
            payload["error_code"],
            "benchmark_watcher.control.authentication_failed",
        )
        self.assertEqual(reads, [])

        auth_calls = []
        self.app.authenticator = lambda request: (
            auth_calls.append(request)
            or self.principal(scope=CONTROL.STATUS_SCOPE)
        )
        for overrides in (
            {"CONTENT_LENGTH": "1", "wsgi.input": io.BytesIO(b"x")},
            {"CONTENT_TYPE": "application/json"},
            {"HTTP_TRANSFER_ENCODING": "chunked"},
        ):
            with self.subTest(overrides=overrides):
                status, _, payload, _ = self.call_status(**overrides)
                self.assertEqual(status, "400 Bad Request")
                self.assertEqual(
                    payload["error_code"],
                    "benchmark_watcher.control.body_invalid",
                )
        self.assertEqual(auth_calls, [])
        self.assertEqual(reads, [])

    def test_authenticated_rearm_is_atomic_content_free_and_exactly_replayed(self):
        status, headers, first, first_bytes = self.call()

        self.assertEqual(status, "200 OK")
        receipt = first["receipt"]
        self.assertEqual(receipt["schema"], CONTROL.RECEIPT_SCHEMA)
        self.assertEqual((receipt["previous_state"], receipt["state"]), (
            "failed", "pending",
        ))
        self.assertEqual(receipt["previous_attempts"], 3)
        self.assertEqual(receipt["previous_error_code"], "benchmark_client.network")
        self.assertEqual(receipt["failed_at"], 100.0)
        self.assertEqual(receipt["rearmed_at"], 200.0)
        self.assertEqual(
            receipt["request_sha256"],
            hashlib.sha256(CONTROL._canonical_json(
                CONTROL._request(self.request())
            )).hexdigest(),
        )
        current = self.watcher.status(now=200)
        self.assertEqual((current.state, current.attempts), ("pending", 0))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("campaign", json.dumps(first))
        self.assertNotIn("mt-MT", json.dumps(first))

        status, _, replay, replay_bytes = self.call()
        self.assertEqual(status, "200 OK")
        self.assertEqual(replay_bytes, first_bytes)
        self.assertEqual(replay, first)
        self.assertEqual(self.watcher.status(now=201).attempts, 0)
        stored = self.connection.execute(
            "SELECT * FROM benchmark_watcher_rearms"
        ).fetchall()
        self.assertEqual(len(stored), 1)
        self.assertNotIn("rearm-request-1", repr(tuple(stored[0])))

        auth = self.requests[0]
        self.assertEqual((auth["method"], auth["path"]), (
            "POST", CONTROL.REARM_PATH,
        ))
        self.assertEqual(
            auth["body_sha256"],
            hashlib.sha256(
                json.dumps(self.request(), separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )

    def test_same_id_with_different_payload_is_an_idempotency_conflict(self):
        self.assertEqual(self.call()[0], "200 OK")

        status, _, payload, _ = self.call(self.request(expected_attempts=2))

        self.assertEqual(status, "409 Conflict")
        self.assertEqual(
            payload["error_code"],
            "benchmark_watcher.control.idempotency_conflict",
        )
        self.assertFalse(payload["retryable"])
        self.assertEqual(self.watcher.status(now=200).state, "pending")

    def test_old_receipt_cannot_rearm_a_later_failed_generation(self):
        self.assertEqual(self.call()[0], "200 OK")
        self.connection.execute("""
            UPDATE benchmark_report_watcher
            SET state = 'failed', attempts = 3, next_attempt_at = 300,
                last_error_code = 'benchmark_client.timeout', updated_at = 300
            WHERE singleton = 1
        """)
        self.connection.commit()

        self.assertEqual(self.call()[0], "200 OK")
        self.assertEqual(self.watcher.status(now=300).state, "failed")

        stale = self.request(request_id="rearm-request-2")
        status, _, payload, _ = self.call(stale)
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(
            payload["error_code"], "benchmark_watcher.control.state_conflict",
        )
        self.assertEqual(self.watcher.status(now=300).state, "failed")

        current = self.request(
            request_id="rearm-request-2",
            expected_failed_at=300,
            expected_error_code="benchmark_client.timeout",
        )
        self.app.clock = lambda: 400
        self.assertEqual(self.call(current)[0], "200 OK")
        self.assertEqual(self.watcher.status(now=300).state, "pending")

    def test_expected_attempts_must_match_failed_generation(self):
        status, _, payload, _ = self.call(self.request(expected_attempts=2))

        self.assertEqual(status, "409 Conflict")
        self.assertEqual(
            payload["error_code"], "benchmark_watcher.control.state_conflict",
        )

        with self.assertRaises(CONTROL.BenchmarkWatcherControlFailed) as caught:
            self.controller.rearm(self.request(), now=99)
        self.assertEqual(
            str(caught.exception), "benchmark_watcher.control.clock_invalid",
        )
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.watcher.status(now=100).state, "failed")
        self.assertEqual((self.watcher.status(now=200).state,
                          self.watcher.status(now=200).attempts), ("failed", 3))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM benchmark_watcher_rearms"
            ).fetchone()[0], 0,
        )

        status, _, payload, _ = self.call(self.request(expected_failed_at=99))
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(
            payload["error_code"], "benchmark_watcher.control.state_conflict",
        )

        status, _, payload, _ = self.call(self.request(
            expected_error_code="benchmark_client.timeout",
        ))
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(
            payload["error_code"], "benchmark_watcher.control.state_conflict",
        )

    def test_authentication_and_request_validation_precede_mutation(self):
        calls = []
        app = CONTROL.BenchmarkWatcherControlHTTPApplication(
            self.controller,
            lambda request: calls.append(request) or self.principal(scope="wrong"),
            clock=lambda: 200,
        )
        original = self.app
        self.app = app
        try:
            status, _, payload, _ = self.call()
        finally:
            self.app = original

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(
            payload["error_code"],
            "benchmark_watcher.control.authentication_failed",
        )
        self.assertEqual(self.watcher.status(now=200).state, "failed")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM benchmark_watcher_rearms"
            ).fetchone()[0], 0,
        )
        self.assertEqual(len(calls), 1)

        app = CONTROL.BenchmarkWatcherControlHTTPApplication(
            self.controller,
            lambda request: (_ for _ in ()).throw(
                RuntimeError("private authenticator detail")
            ),
            clock=lambda: 200,
        )
        original = self.app
        self.app = app
        try:
            status, _, payload, _ = self.call()
        finally:
            self.app = original
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(
            payload["error_code"],
            "benchmark_watcher.control.authentication_unavailable",
        )
        self.assertTrue(payload["retryable"])
        self.assertEqual(self.watcher.status(now=200).state, "failed")

        status, _, payload, _ = self.call(b'{"schema":1,"schema":2}')
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(
            payload["error_code"], "benchmark_watcher.control.request_invalid",
        )
        self.assertEqual(self.watcher.status(now=200).state, "failed")

    def test_live_final_and_not_failed_states_never_rearm(self):
        states = (
            ("pending", "benchmark_watcher.control.rearm_not_required"),
            ("retry_wait", "benchmark_watcher.control.rearm_not_required"),
            ("leased", "benchmark_watcher.control.lease_active"),
            ("succeeded", "benchmark_watcher.control.result_final"),
        )
        for state, expected in states:
            with self.subTest(state=state):
                connection = sqlite3.connect(":memory:")
                watcher = CONTROL._WATCHER.DurableBenchmarkReportWatcher(
                    connection, Client(), lease_seconds=10,
                    base_delay_seconds=5, max_delay_seconds=20, max_attempts=3,
                )
                controller = CONTROL.DurableBenchmarkWatcherRearmController(watcher)
                if state == "retry_wait":
                    connection.execute("""
                        UPDATE benchmark_report_watcher
                        SET state = 'retry_wait', attempts = 1,
                            last_error_code = 'benchmark_client.network'
                    """)
                elif state == "leased":
                    connection.execute("""
                        UPDATE benchmark_report_watcher
                        SET state = 'leased', attempts = 1,
                            lease_owner = 'worker', lease_token = 'token',
                            lease_expires_at = 250
                    """)
                elif state == "succeeded":
                    connection.execute("""
                        UPDATE benchmark_report_watcher
                        SET state = 'succeeded', attempts = 1,
                            report_sha256 = ?, report_status = 'PASS',
                            superiority_claim_allowed = 1, locale_count = 2,
                            completed_at = 100
                    """, ("d" * 64,))
                connection.commit()
                with self.assertRaises(
                    CONTROL.BenchmarkWatcherControlFailed,
                ) as caught:
                    controller.rearm(self.request(expected_attempts=1), now=200)
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(watcher.status(now=200).state, state)
                connection.close()

    def test_transport_and_tampered_evidence_fail_closed(self):
        status, _, payload, _ = self.call(**{"wsgi.url_scheme": "http"})
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(
            payload["error_code"], "benchmark_watcher.control.https_required",
        )
        status, _, payload, _ = self.call(HTTP_IDEMPOTENCY_KEY="other")
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(
            payload["error_code"],
            "benchmark_watcher.control.idempotency_key_invalid",
        )
        self.assertEqual(self.watcher.status(now=200).state, "failed")

        self.assertEqual(self.call()[0], "200 OK")
        self.connection.execute("""
            UPDATE benchmark_watcher_rearms SET response_sha256 = ?
        """, ("e" * 64,))
        self.connection.commit()
        status, _, payload, _ = self.call()
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(
            payload["error_code"],
            "benchmark_watcher.control.evidence_invalid",
        )
        self.assertFalse(payload["retryable"])

    def test_transport_framing_bypasses_are_rejected_without_state_access(self):
        cases = (
            ({"REQUEST_METHOD": "GET"}, "404 Not Found",
             "benchmark_watcher.control.route_not_found"),
            ({"PATH_INFO": "/v1/other"}, "404 Not Found",
             "benchmark_watcher.control.route_not_found"),
            ({"QUERY_STRING": "state=failed"}, "400 Bad Request",
             "benchmark_watcher.control.query_invalid"),
            ({"CONTENT_TYPE": "text/plain"}, "415 Unsupported Media Type",
             "benchmark_watcher.control.content_type"),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, "400 Bad Request",
             "benchmark_watcher.control.transfer_encoding"),
            ({"CONTENT_LENGTH": str(CONTROL.MAX_BODY_BYTES + 1)},
             "413 Content Too Large",
             "benchmark_watcher.control.body_too_large"),
            ({"HTTP_X_BAD": "line\nbreak"}, "400 Bad Request",
             "benchmark_watcher.control.headers_invalid"),
            ({"HTTP_IDEMPOTENCY_KEY": ""}, "400 Bad Request",
             "benchmark_watcher.control.idempotency_key_invalid"),
        )
        for overrides, expected_status, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                status, _, payload, _ = self.call(**overrides)
                self.assertEqual(status, expected_status)
                self.assertEqual(payload["error_code"], expected_code)
                self.assertEqual(self.watcher.status(now=200).state, "failed")
                self.assertEqual(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM benchmark_watcher_rearms"
                    ).fetchone()[0], 0,
                )

    def test_schema_drift_blocks_without_rearming(self):
        self.connection.execute(
            "ALTER TABLE benchmark_watcher_rearms ADD COLUMN injected TEXT"
        )
        self.connection.commit()

        status, _, payload, _ = self.call()

        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(
            payload["error_code"], "benchmark_watcher.control.schema_invalid",
        )
        self.assertFalse(payload["retryable"])
        self.assertEqual(self.watcher.status(now=200).state, "failed")


if __name__ == "__main__":
    unittest.main()
