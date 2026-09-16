from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CLIENT = load(
    "blun_test_website_localization_benchmark_watcher_control_client",
    ROOT / "integrations"
    / "website_localization_benchmark_watcher_control_client.py",
)
CONTROL = CLIENT._CONTROL


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_value(**changes):
    value = {
        "schema": CONTROL.REQUEST_SCHEMA,
        "request_id": "operator-rearm-1",
        "expected_attempts": 20,
        "expected_failed_at": 1000.0,
        "expected_error_code": "benchmark_client.network",
    }
    value.update(changes)
    return value


def receipt_value(request=None, **changes):
    request = request_value() if request is None else request
    value = {
        "schema": CONTROL.RECEIPT_SCHEMA,
        "request_sha256": hashlib.sha256(encoded(request)).hexdigest(),
        "previous_state": "failed",
        "previous_attempts": request["expected_attempts"],
        "previous_error_code": request["expected_error_code"],
        "failed_at": request["expected_failed_at"],
        "state": "pending",
        "rearmed_at": 1500.0,
    }
    value.update(changes)
    return value


def http_result(value, *, status=200, headers=()):
    body = encoded(value)
    standard = (
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    )
    return CLIENT.HTTPResult(status, standard + tuple(headers), body)


def success_response(request=None, **receipt_changes):
    return http_result({
        "schema": CONTROL.RESPONSE_SCHEMA,
        "receipt": receipt_value(request, **receipt_changes),
    })


def error_response(code, *, status, retryable):
    return http_result({
        "schema": CONTROL.ERROR_SCHEMA,
        "error_code": code,
        "retryable": retryable,
    }, status=status)


def status_response(*, state="failed", **changes):
    status = {
        "schema": CONTROL.STATUS_SCHEMA,
        "checked_at": 1500.0,
        "state": state,
        "rearmable": state == "failed",
        "generation": (
            {
                "attempts": 20,
                "failed_at": 1000.0,
                "error_code": "benchmark_client.network",
            }
            if state == "failed" else None
        ),
    }
    status.update(changes)
    return http_result({
        "schema": CONTROL.STATUS_RESPONSE_SCHEMA,
        "status": status,
    })


def openapi_response(*, document=None, contract_digest=None, document_digest=None):
    contract = CONTROL._openapi_contract()
    expected = CONTROL._OPENAPI.build_document(contract)
    document = expected if document is None else document
    return http_result({
        "schema": CONTROL.OPENAPI_RESPONSE_SCHEMA,
        "contract_sha256": (
            CONTROL._OPENAPI.document_sha256(contract)
            if contract_digest is None else contract_digest
        ),
        "openapi_sha256": (
            CONTROL._OPENAPI.document_sha256(document)
            if document_digest is None else document_digest
        ),
        "openapi": document,
    })


class Transport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class WatcherBackendClient:
    expected_campaign_id = "benchmark-campaign-" + "a" * 64
    expected_policy_sha256 = "b" * 64
    expected_suite_sha256 = "c" * 64

    def report(self):
        raise AssertionError("rearm must not call the benchmark provider")


class WSGITransport:
    def __init__(self, app, *, lose_first=False):
        self.app = app
        self.lose_first = lose_first
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        parsed = CLIENT.urllib.parse.urlsplit(url)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "wsgi.url_scheme": parsed.scheme,
            "wsgi.input": io.BytesIO(body or b""),
        }
        for name, value in headers.items():
            key = name.upper().replace("-", "_")
            if key in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                environ[key] = value
            else:
                environ["HTTP_" + key] = value
        captured = {}
        response = b"".join(self.app(
            environ,
            lambda status, items: captured.update(
                status=status, headers=tuple(items),
            ),
        ))
        if self.lose_first:
            self.lose_first = False
            raise RuntimeError("response lost after server acceptance")
        return CLIENT.HTTPResult(
            int(captured["status"].split(" ", 1)[0]),
            captured["headers"], response,
        )


class BenchmarkWatcherControlClientTests(unittest.TestCase):
    def make_client(
        self, transport, *, credentials=None, clock=lambda: 1600, **kwargs,
    ):
        return CLIENT.WebsiteLocalizationBenchmarkWatcherControlClient(
            "https://control.example",
            credentials or (
                lambda context: {"Authorization": "Bearer private-token"}
            ),
            transport=transport,
            clock=clock,
            **kwargs,
        )

    def assert_failure(self, code, call, *, retryable=None):
        with self.assertRaises(
            CLIENT.BenchmarkWatcherControlClientFailed,
        ) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if retryable is not None:
            self.assertEqual(caught.exception.retryable, retryable)

    @staticmethod
    def call(client, **changes):
        value = request_value(**changes)
        return client.rearm(
            request_id=value["request_id"],
            expected_attempts=value["expected_attempts"],
            expected_failed_at=value["expected_failed_at"],
            expected_error_code=value["expected_error_code"],
        )

    def test_rearms_exact_generation_with_body_bound_credentials(self):
        contexts = []

        def credentials(context):
            contexts.append(context)
            return {"Authorization": "Bearer private-token"}

        transport = Transport(success_response())
        snapshot = self.call(self.make_client(
            transport, credentials=credentials,
        ))
        method, url, headers, body, timeout = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://control.example" + CONTROL.REARM_PATH)
        self.assertEqual(body, encoded(request_value()))
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Idempotency-Key"], "operator-rearm-1")
        self.assertEqual(headers["Authorization"], "Bearer private-token")
        self.assertEqual(timeout, 10.0)
        self.assertEqual(contexts, [{
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "POST",
            "path": CONTROL.REARM_PATH,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "idempotency_key": "operator-rearm-1",
        }])
        self.assertEqual(snapshot.request_sha256, contexts[0]["body_sha256"])
        self.assertEqual(snapshot.receipt["previous_attempts"], 20)
        payload = snapshot.as_payload()
        payload["state"] = "changed"
        self.assertEqual(snapshot.receipt["state"], "pending")

    def test_status_reads_exact_generation_with_separate_bodyless_auth(self):
        contexts = []
        transport = Transport(status_response())
        status = self.make_client(
            transport,
            credentials=lambda context: contexts.append(context) or {
                "Authorization": "Bearer status-token",
            },
        ).status()

        method, url, headers, body, timeout = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://control.example" + CONTROL.STATUS_PATH)
        self.assertIsNone(body)
        self.assertNotIn("Content-Type", headers)
        self.assertNotIn("Content-Length", headers)
        self.assertNotIn("Idempotency-Key", headers)
        self.assertEqual(headers["Authorization"], "Bearer status-token")
        self.assertEqual(timeout, 10.0)
        self.assertEqual(contexts, [{
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "GET",
            "path": CONTROL.STATUS_PATH,
            "body_sha256": hashlib.sha256(b"").hexdigest(),
            "idempotency_key": None,
        }])
        self.assertEqual(status.state, "failed")
        self.assertTrue(status.rearmable)
        self.assertEqual(status.generation, {
            "attempts": 20,
            "failed_at": 1000.0,
            "error_code": "benchmark_client.network",
        })
        payload = status.as_payload()
        payload["generation"]["attempts"] = 1
        self.assertEqual(status.generation["attempts"], 20)

    def test_openapi_is_exact_reconstructed_and_uses_separate_bodyless_auth(self):
        contexts = []
        transport = Transport(openapi_response())
        snapshot = self.make_client(
            transport,
            credentials=lambda context: contexts.append(context) or {
                "Authorization": "Bearer openapi-token",
            },
        ).openapi()

        method, url, headers, body, timeout = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://control.example" + CONTROL.OPENAPI_PATH)
        self.assertIsNone(body)
        self.assertNotIn("Content-Type", headers)
        self.assertNotIn("Content-Length", headers)
        self.assertNotIn("Idempotency-Key", headers)
        self.assertEqual(headers["Authorization"], "Bearer openapi-token")
        self.assertEqual(timeout, 10.0)
        self.assertEqual(contexts, [{
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "GET",
            "path": CONTROL.OPENAPI_PATH,
            "body_sha256": hashlib.sha256(b"").hexdigest(),
            "idempotency_key": None,
        }])
        expected = CONTROL._OPENAPI.build_document(CONTROL._openapi_contract())
        self.assertEqual(snapshot.openapi, expected)
        self.assertEqual(set(snapshot.openapi["paths"]), {
            CONTROL.OPENAPI_PATH, CONTROL.REARM_PATH, CONTROL.STATUS_PATH,
        })
        self.assertNotIn("servers", snapshot.openapi)
        payload = snapshot.as_payload()
        payload["info"]["title"] = "changed"
        self.assertNotEqual(snapshot.openapi["info"]["title"], "changed")

    def test_openapi_rejects_self_rehashed_stale_and_digest_substitutions(self):
        altered = deepcopy(
            CONTROL._OPENAPI.build_document(CONTROL._openapi_contract())
        )
        altered["info"]["description"] = "Altered contract"
        scenarios = (
            openapi_response(document=altered),
            openapi_response(contract_digest="d" * 64),
            openapi_response(document_digest="e" * 64),
        )
        for response in scenarios:
            with self.subTest(response=response.body[:120]):
                self.assert_failure(
                    "benchmark_watcher.control_client.openapi_mismatch",
                    lambda response=response: self.make_client(
                        Transport(response)
                    ).openapi(),
                    retryable=False,
                )

    def test_rearm_failed_completes_status_to_recovery_vertical(self):
        connection = sqlite3.connect(":memory:")
        try:
            watcher = CONTROL._WATCHER.DurableBenchmarkReportWatcher(
                connection, WatcherBackendClient(), lease_seconds=10,
                base_delay_seconds=5, max_delay_seconds=20, max_attempts=20,
            )
            connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'failed', attempts = 20, next_attempt_at = 1000,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'benchmark_client.network',
                    updated_at = 1000
                WHERE singleton = 1
            """)
            connection.commit()
            auth_requests = []

            def authenticate(request):
                auth_requests.append(request)
                scope = (
                    CONTROL.STATUS_SCOPE
                    if request["path"] == CONTROL.STATUS_PATH
                    else CONTROL.REARM_SCOPE
                )
                return {
                    "schema": CONTROL.PRINCIPAL_SCHEMA,
                    "operator_id": "operator-1",
                    "credential_id": "control-credential-1",
                    "credential_version": "2026-09-16",
                    "scope": scope,
                }

            app = CONTROL.BenchmarkWatcherControlHTTPApplication(
                CONTROL.DurableBenchmarkWatcherRearmController(watcher),
                authenticate, clock=lambda: 1500,
            )
            transport = WSGITransport(app)
            snapshot = self.make_client(transport).rearm_failed(
                request_id="operator-rearm-from-status-1",
            )

            self.assertEqual(snapshot.receipt["previous_attempts"], 20)
            self.assertEqual(snapshot.receipt["failed_at"], 1000.0)
            self.assertEqual(watcher.status(now=1500).state, "pending")
            self.assertEqual(
                [call[0:2] for call in transport.calls],
                [
                    ("GET", "https://control.example" + CONTROL.STATUS_PATH),
                    ("POST", "https://control.example" + CONTROL.REARM_PATH),
                ],
            )
            self.assertEqual(
                [request["body_sha256"] for request in auth_requests],
                [
                    hashlib.sha256(b"").hexdigest(),
                    hashlib.sha256(transport.calls[1][3]).hexdigest(),
                ],
            )
        finally:
            connection.close()

    def test_rearm_failed_never_posts_when_status_is_not_failed(self):
        transport = Transport(status_response(state="pending"))
        client = self.make_client(transport)

        self.assert_failure(
            "benchmark_watcher.control_client.not_rearmable",
            lambda: client.rearm_failed(request_id="operator-rearm-1"),
            retryable=False,
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][0], "GET")

    def test_rearm_failed_cannot_reset_a_generation_changed_after_status(self):
        connection = sqlite3.connect(":memory:")
        try:
            watcher = CONTROL._WATCHER.DurableBenchmarkReportWatcher(
                connection, WatcherBackendClient(), lease_seconds=10,
                base_delay_seconds=5, max_delay_seconds=20, max_attempts=20,
            )
            connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'failed', attempts = 20, next_attempt_at = 1000,
                    last_error_code = 'benchmark_client.network',
                    updated_at = 1000
                WHERE singleton = 1
            """)
            connection.commit()

            def authenticate(request):
                return {
                    "schema": CONTROL.PRINCIPAL_SCHEMA,
                    "operator_id": "operator-1",
                    "credential_id": "control-credential-1",
                    "credential_version": "2026-09-16",
                    "scope": (
                        CONTROL.STATUS_SCOPE
                        if request["path"] == CONTROL.STATUS_PATH
                        else CONTROL.REARM_SCOPE
                    ),
                }

            backend = WSGITransport(
                CONTROL.BenchmarkWatcherControlHTTPApplication(
                    CONTROL.DurableBenchmarkWatcherRearmController(watcher),
                    authenticate, clock=lambda: 1500,
                ),
            )

            class RacingTransport:
                def __init__(self):
                    self.calls = backend.calls

                def request(self, method, url, headers, body, *, timeout):
                    result = backend.request(
                        method, url, headers, body, timeout=timeout,
                    )
                    if method == "GET":
                        connection.execute("""
                            UPDATE benchmark_report_watcher
                            SET last_error_code = 'benchmark_client.timeout',
                                updated_at = 1200
                            WHERE singleton = 1
                        """)
                        connection.commit()
                    return result

            client = self.make_client(RacingTransport())
            self.assert_failure(
                "benchmark_watcher.control.state_conflict",
                lambda: client.rearm_failed(request_id="operator-rearm-race-1"),
                retryable=False,
            )
            current = watcher.status(now=1500)
            self.assertEqual(current.state, "failed")
            self.assertEqual(current.attempts, 20)
            self.assertEqual(current.last_error_code, "benchmark_client.timeout")
            self.assertEqual(len(backend.calls), 2)
        finally:
            connection.close()

    def test_status_semantic_mismatches_fail_closed(self):
        scenarios = (
            status_response(rearmable=False),
            status_response(generation=None),
            status_response(generation={
                "attempts": True,
                "failed_at": 1000.0,
                "error_code": "benchmark_client.network",
            }),
            status_response(generation={
                "attempts": 20,
                "failed_at": 1501.0,
                "error_code": "benchmark_client.network",
            }),
            status_response(checked_at=2001.0),
            status_response(state="pending", generation={
                "attempts": 20,
                "failed_at": 1000.0,
                "error_code": "benchmark_client.network",
            }),
        )
        for response in scenarios:
            with self.subTest(response=response.body):
                self.assert_failure(
                    "benchmark_watcher.control_client.status_mismatch",
                    lambda response=response: self.make_client(
                        Transport(response), max_future_skew=0,
                    ).status(),
                    retryable=False,
                )

    def test_uncertain_retry_reuses_exact_identity_and_bytes(self):
        response = success_response()
        transport = Transport(response, response)
        client = self.make_client(transport)

        first = self.call(client)
        second = self.call(client)

        self.assertEqual(first, second)
        self.assertEqual(transport.calls[0][3], transport.calls[1][3])
        self.assertEqual(
            transport.calls[0][2]["Idempotency-Key"],
            transport.calls[1][2]["Idempotency-Key"],
        )

    def test_real_endpoint_replays_after_response_loss_without_provider_call(self):
        connection = sqlite3.connect(":memory:")
        try:
            watcher = CONTROL._WATCHER.DurableBenchmarkReportWatcher(
                connection, WatcherBackendClient(), lease_seconds=10,
                base_delay_seconds=5, max_delay_seconds=20, max_attempts=20,
            )
            connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'failed', attempts = 20, next_attempt_at = 1000,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'benchmark_client.network',
                    updated_at = 1000
                WHERE singleton = 1
            """)
            connection.commit()
            auth_requests = []
            controller = CONTROL.DurableBenchmarkWatcherRearmController(watcher)
            app = CONTROL.BenchmarkWatcherControlHTTPApplication(
                controller,
                lambda request: auth_requests.append(request) or {
                    "schema": CONTROL.PRINCIPAL_SCHEMA,
                    "operator_id": "operator-1",
                    "credential_id": "control-credential-1",
                    "credential_version": "2026-09-16",
                    "scope": "benchmark-watcher:rearm",
                },
                clock=lambda: 1500,
            )
            transport = WSGITransport(app, lose_first=True)
            client = self.make_client(transport)

            self.assert_failure(
                "benchmark_watcher.control_client.network",
                lambda: self.call(client), retryable=True,
            )
            self.assertEqual(watcher.status(now=1500).state, "pending")

            replay = self.call(client)
            self.assertEqual(replay.receipt["rearmed_at"], 1500.0)
            self.assertEqual(transport.calls[0][3], transport.calls[1][3])
            self.assertEqual(len(auth_requests), 2)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM benchmark_watcher_rearms"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_request_validation_happens_before_credentials_or_network(self):
        events = []
        transport = Transport()
        client = self.make_client(
            transport,
            credentials=lambda context: events.append(context) or {},
        )
        scenarios = (
            {"request_id": "bad key"},
            {"expected_attempts": True},
            {"expected_attempts": 0},
            {"expected_attempts": CONTROL._WATCHER.MAX_ATTEMPTS + 1},
            {"expected_failed_at": float("nan")},
            {"expected_error_code": "Bad Error"},
        )
        for changes in scenarios:
            with self.subTest(changes=changes):
                self.assert_failure(
                    "benchmark_watcher.control_client.request_invalid",
                    lambda changes=changes: self.call(client, **changes),
                    retryable=False,
                )
        self.assertEqual(events, [])
        self.assertEqual(transport.calls, [])

    def test_credentials_cannot_override_protocol_headers(self):
        for header in (
            "Accept", "Connection", "Content-Length", "Content-Type", "Host",
            "Idempotency-Key", "Transfer-Encoding",
        ):
            with self.subTest(header=header):
                client = self.make_client(
                    Transport(),
                    credentials=lambda context, header=header: {header: "forged"},
                )
                self.assert_failure(
                    "benchmark_watcher.control_client.authentication_invalid",
                    lambda: self.call(client), retryable=False,
                )

        self.assert_failure(
            "benchmark_watcher.control_client.authentication_unavailable",
            lambda: self.call(self.make_client(
                Transport(), credentials=lambda context: (_ for _ in ()).throw(
                    RuntimeError("secret manager unavailable")
                ),
            )),
            retryable=True,
        )

    def test_origin_transport_and_clock_fail_closed(self):
        invalid_origins = (
            "http://control.example", "https://user@control.example",
            "https://control.example/path", "https://control.example?q=1",
        )
        for origin in invalid_origins:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    CLIENT.WebsiteLocalizationBenchmarkWatcherControlClient(
                        origin, lambda context: {}, transport=Transport(),
                        clock=lambda: 1,
                    )
        loopback = CLIENT.WebsiteLocalizationBenchmarkWatcherControlClient(
            "http://127.0.0.1:8080", lambda context: {},
            transport=Transport(success_response()), clock=lambda: 1600,
            allow_loopback_http=True,
        )
        self.call(loopback)

        self.assert_failure(
            "benchmark_watcher.control_client.network",
            lambda: self.call(self.make_client(Transport(RuntimeError("private")))),
            retryable=True,
        )
        self.assert_failure(
            "benchmark_watcher.control_client.clock_invalid",
            lambda: self.call(self.make_client(
                Transport(success_response()), clock=lambda: float("nan"),
            )),
            retryable=False,
        )

    def test_remote_errors_preserve_only_valid_retry_semantics(self):
        scenarios = (
            (
                error_response(
                    "benchmark_watcher.control.state_conflict",
                    status=409, retryable=False,
                ),
                "benchmark_watcher.control.state_conflict", False,
            ),
            (
                error_response(
                    "benchmark_watcher.control.authentication_unavailable",
                    status=503, retryable=True,
                ),
                "benchmark_watcher.control.authentication_unavailable", True,
            ),
        )
        for response, code, retryable in scenarios:
            with self.subTest(code=code):
                self.assert_failure(
                    code,
                    lambda response=response: self.call(
                        self.make_client(Transport(response))
                    ),
                    retryable=retryable,
                )

        malformed = error_response(
            "benchmark_watcher.control.state_conflict",
            status=409, retryable=True,
        )
        unknown = error_response(
            "benchmark_watcher.control.new_unpinned_error",
            status=503, retryable=True,
        )
        for response in (malformed, unknown):
            with self.subTest(response=response.body):
                self.assert_failure(
                    "benchmark_watcher.control_client.contract_mismatch",
                    lambda response=response: self.call(
                        self.make_client(Transport(response))
                    ),
                    retryable=False,
                )

    def test_response_framing_and_json_ambiguity_fail_closed(self):
        valid = success_response()
        bodies = (
            CLIENT.HTTPResult(200, valid.headers + (("Content-Length", "1"),), valid.body),
            CLIENT.HTTPResult(200, tuple(
                pair for pair in valid.headers if pair[0] != "Referrer-Policy"
            ), valid.body),
            CLIENT.HTTPResult(200, valid.headers + (("Transfer-Encoding", "chunked"),), valid.body),
            CLIENT.HTTPResult(200, valid.headers, valid.body + b"x"),
            CLIENT.HTTPResult(200, valid.headers, b"{" + b" " * MAX_PADDING),
        )
        for result in bodies:
            with self.subTest(headers=result.headers, size=len(result.body)):
                self.assert_failure(
                    "benchmark_watcher.control_client.response_invalid",
                    lambda result=result: self.call(
                        self.make_client(Transport(result))
                    ),
                    retryable=True,
                )

        duplicate = (
            b'{"schema":"' + CONTROL.RESPONSE_SCHEMA.encode("ascii")
            + b'","schema":"duplicate","receipt":{}}'
        )
        headers = tuple(
            (name, str(len(duplicate)) if name == "Content-Length" else value)
            for name, value in valid.headers
        )
        self.assert_failure(
            "benchmark_watcher.control_client.response_invalid",
            lambda: self.call(self.make_client(Transport(
                CLIENT.HTTPResult(200, headers, duplicate),
            ))),
            retryable=True,
        )

    def test_every_receipt_binding_is_authoritative(self):
        scenarios = (
            {"request_sha256": "f" * 64},
            {"previous_state": "pending"},
            {"previous_attempts": 19},
            {"previous_attempts": True},
            {"previous_error_code": "benchmark_client.parser"},
            {"failed_at": 999.0},
            {"state": "failed"},
            {"rearmed_at": 999.0},
            {"rearmed_at": 2001.0},
            {"schema": "wrong.receipt.v1"},
        )
        for changes in scenarios:
            with self.subTest(changes=changes):
                self.assert_failure(
                    "benchmark_watcher.control_client.receipt_mismatch",
                    lambda changes=changes: self.call(self.make_client(
                        Transport(success_response(**changes)),
                        max_future_skew=0,
                    )),
                    retryable=False,
                )

    def test_status_schema_and_dependency_shape_are_strict(self):
        valid = success_response()
        scenarios = (
            CLIENT.HTTPResult(True, valid.headers, valid.body),
            CLIENT.HTTPResult(201, valid.headers, valid.body),
            object(),
            http_result({
                "schema": "wrong.response.v1",
                "receipt": receipt_value(),
            }),
            http_result({
                "schema": CONTROL.RESPONSE_SCHEMA,
                "receipt": receipt_value(),
                "extra": True,
            }),
        )
        for result in scenarios:
            with self.subTest(result=repr(result)):
                self.assert_failure(
                    "benchmark_watcher.control_client.response_invalid",
                    lambda result=result: self.call(
                        self.make_client(Transport(result))
                    ),
                    retryable=True,
                )


MAX_PADDING = CONTROL.MAX_RESPONSE_BYTES + 1


if __name__ == "__main__":
    unittest.main()
