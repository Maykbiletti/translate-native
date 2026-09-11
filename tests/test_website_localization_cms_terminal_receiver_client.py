from __future__ import annotations

import hashlib
import importlib.util
import io
import json
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


CLIENT = load(
    "blun_test_terminal_receiver_client",
    ROOT
    / "integrations"
    / "website_localization_cms_terminal_receiver_client.py",
)
RUNTIME = load(
    "blun_test_terminal_receiver_client_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_terminal_notification_receiver_runtime.py",
)
RECEIVER = RUNTIME._RECEIVER


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def notification():
    value = {
        "schema": RECEIVER.NOTIFICATION_SCHEMA,
        "event_id": "cms-event-184",
        "site_id": "public-site",
        "plan_id": "plan-cms-event-184",
        "website_version": "release-42",
        "source_sequence": 42,
        "job_count": 23,
        "change_sha256": "a" * 64,
        "lifecycle_binding_sha256": "b" * 64,
        "terminal_status": "published",
        "lifecycle_sha256": "c" * 64,
    }
    value["notification_id"] = "terminal-" + hashlib.sha256(
        RECEIVER._canonical(value)
    ).hexdigest()
    return value


def store(runtime, payload):
    body = RECEIVER._canonical(payload)
    return runtime.inbox.accept(
        payload, body, hashlib.sha256(body).hexdigest(), now=123.5,
    )


def principal(request):
    scopes = {
        RECEIVER.CAPABILITIES_PATH: RECEIVER.CAPABILITIES_SCOPE,
        RECEIVER.HEALTH_PATH: RECEIVER.HEALTH_SCOPE,
        RECEIVER.READINESS_PATH: RECEIVER.READINESS_SCOPE,
        RECEIVER.STATUS_PATH: RECEIVER.STATUS_SCOPE,
    }
    return {
        "schema": RECEIVER.PRINCIPAL_SCHEMA,
        "principal_id": "operator",
        "credential_id": "operator-key",
        "credential_version": "v1",
        "scope": scopes[request["path"]],
        "site_id": request.get("site_id", "public-site"),
    }


def server_authentication(request, headers):
    if headers.get("authorization") != "Bearer operator-secret":
        raise RuntimeError("private credential failure")
    return principal(request)


class WSGITransport:
    def __init__(self, application):
        self.application = application
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
            normalized = name.upper().replace("-", "_")
            if normalized == "CONTENT_TYPE":
                environ["CONTENT_TYPE"] = value
            elif normalized == "CONTENT_LENGTH":
                environ["CONTENT_LENGTH"] = value
            else:
                environ["HTTP_" + normalized] = value
        if body is not None:
            environ["CONTENT_LENGTH"] = str(len(body))
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = tuple(response_headers)

        response_body = b"".join(self.application(environ, start_response))
        return CLIENT.HTTPResult(
            captured["status"], captured["headers"], response_body,
        )


class FakeTransport:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def json_result(status, value, headers=None):
    body = canonical(value)
    return CLIENT.HTTPResult(
        status,
        tuple(headers or (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        )),
        body,
    )


class TerminalReceiverClientTests(unittest.TestCase):
    def setUp(self):
        self.runtime = RUNTIME.open_durable_terminal_notification_receiver(
            ":memory:",
            server_authentication,
            origin="https://cms.example.test",
            clock=lambda: 123.5,
        )
        self.digest = RECEIVER.capabilities_payload()["sha256"]
        self.authentication_requests = []

        def authenticate(request):
            self.authentication_requests.append(dict(request))
            request["body_sha256"] = "0" * 64
            return {"Authorization": "Bearer operator-secret"}

        self.transport = WSGITransport(self.runtime)
        self.client = CLIENT.HTTPTerminalReceiverClient(
            "https://cms.example.test",
            self.digest,
            authenticate,
            transport=self.transport,
        )

    def tearDown(self):
        self.runtime.close()

    def test_discovers_exact_pinned_contract_with_bound_authentication(self):
        response = self.client.capabilities()

        self.assertEqual(response["capabilities"]["sha256"], self.digest)
        self.assertEqual(len(self.transport.calls), 1)
        method, url, headers, body, timeout = self.transport.calls[0]
        self.assertEqual((method, url, body, timeout), (
            "GET",
            "https://cms.example.test" + RECEIVER.CAPABILITIES_PATH,
            None,
            30.0,
        ))
        self.assertNotIn("Content-Type", headers)
        self.assertEqual(self.authentication_requests, [{
            "schema": RECEIVER.AUTH_SCHEMA,
            "method": "GET",
            "origin": "https://cms.example.test",
            "path": RECEIVER.CAPABILITIES_PATH,
            "body_sha256": hashlib.sha256(b"").hexdigest(),
        }])

    def test_health_revalidates_contract_then_returns_exact_snapshot(self):
        payload = notification()
        store(self.runtime, payload)
        changes = self.runtime._connection.total_changes

        response = self.client.health()

        self.assertEqual((response["status"], response["received"]), ("ok", 1))
        self.assertEqual([call[0] for call in self.transport.calls], ["GET", "GET"])
        self.assertTrue(self.transport.calls[1][1].endswith(RECEIVER.HEALTH_PATH))
        self.assertEqual(self.runtime._connection.total_changes, changes)
        self.assertNotIn(payload["event_id"], repr(response))
        self.assertNotIn(payload["site_id"], repr(response))

    def test_readiness_preserves_valid_blocked_state(self):
        response = self.client.readiness()

        self.assertEqual(response, {
            "schema": RECEIVER.READINESS_RESPONSE_SCHEMA,
            "status": "not_ready",
            "worker_state": "unmanaged",
            "inbox_status": None,
            "error_code": "notification_receiver.worker_not_ready",
        })
        self.assertEqual(len(self.transport.calls), 2)

    def test_health_preserves_complete_storage_blocked_state(self):
        capabilities = {
            "schema": RECEIVER.CAPABILITIES_RESPONSE_SCHEMA,
            "capabilities": RECEIVER.capabilities_payload(),
        }
        blocked = {
            "schema": RECEIVER.HEALTH_RESPONSE_SCHEMA,
            "status": "blocked",
            "runtime_state": "open",
            "worker_state": "stopped",
            "inbox_status": "blocked",
            "received": None,
            "processing_counts": None,
            "processing_due": None,
            "expired_leases": None,
            "failed": None,
            "error_code": "notification_receiver.storage_blocked",
        }
        transport = FakeTransport([
            json_result(200, capabilities), json_result(503, blocked),
        ])
        client = CLIENT.HTTPTerminalReceiverClient(
            "https://cms.example.test", self.digest,
            lambda _request: {"Authorization": "x"},
            transport=transport,
        )

        self.assertEqual(client.health(), blocked)
        self.assertEqual(len(transport.calls), 2)

    def test_status_is_exactly_bound_to_event_site_body_and_contract(self):
        payload = notification()
        store(self.runtime, payload)
        changes = self.runtime._connection.total_changes

        response = self.client.status(payload["event_id"], payload["site_id"])

        self.assertEqual((response["event_id"], response["site_id"]), (
            payload["event_id"], payload["site_id"],
        ))
        method, _url, headers, body, _timeout = self.transport.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(headers["X-Localization-Terminal-Status-SHA256"], (
            hashlib.sha256(body).hexdigest()
        ))
        self.assertEqual(self.authentication_requests[1], {
            "schema": RECEIVER.AUTH_SCHEMA,
            "method": "POST",
            "origin": "https://cms.example.test",
            "path": RECEIVER.STATUS_PATH,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
        })
        self.assertEqual(self.runtime._connection.total_changes, changes)

    def test_custom_notification_path_can_be_pinned(self):
        self.runtime.close()
        custom = "/receiver/v2/terminal"
        self.runtime = RUNTIME.open_durable_terminal_notification_receiver(
            ":memory:", server_authentication,
            origin="https://cms.example.test", path=custom,
        )
        digest = RECEIVER.capabilities_payload(custom)["sha256"]
        transport = WSGITransport(self.runtime)
        client = CLIENT.HTTPTerminalReceiverClient(
            "https://cms.example.test", digest,
            lambda _request: {"Authorization": "Bearer operator-secret"},
            transport=transport,
        )

        response = client.capabilities()

        self.assertEqual(
            response["capabilities"]["operations"]["notification"]["path"],
            custom,
        )

    def test_contract_drift_blocks_before_operational_read(self):
        capabilities = RECEIVER.capabilities_payload()
        response = {
            "schema": RECEIVER.CAPABILITIES_RESPONSE_SCHEMA,
            "capabilities": capabilities,
        }
        transport = FakeTransport([json_result(200, response)])
        client = CLIENT.HTTPTerminalReceiverClient(
            "https://cms.example.test", "0" * 64,
            lambda _request: {"Authorization": "x"},
            transport=transport,
        )

        with self.assertRaises(CLIENT.TerminalReceiverClientBlocked) as caught:
            client.health()

        self.assertEqual(caught.exception.code, (
            "terminal_receiver_client.capabilities_binding"
        ))
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_semantically_altered_contract_blocks_even_with_rehashed_digest(self):
        capabilities = RECEIVER.capabilities_payload()
        capabilities.pop("sha256")
        capabilities["operations"]["health"]["scope"] = (
            "terminal-notification:write"
        )
        digest = hashlib.sha256(canonical(capabilities)).hexdigest()
        capabilities["sha256"] = digest
        transport = FakeTransport([json_result(200, {
            "schema": RECEIVER.CAPABILITIES_RESPONSE_SCHEMA,
            "capabilities": capabilities,
        })])
        client = CLIENT.HTTPTerminalReceiverClient(
            "https://cms.example.test", digest,
            lambda _request: {"Authorization": "x"},
            transport=transport,
        )

        with self.assertRaises(CLIENT.TerminalReceiverClientBlocked) as caught:
            client.capabilities()

        self.assertEqual(caught.exception.code, (
            "terminal_receiver_client.capabilities_binding"
        ))

    def test_altered_health_and_status_bindings_fail_closed(self):
        capabilities = {
            "schema": RECEIVER.CAPABILITIES_RESPONSE_SCHEMA,
            "capabilities": RECEIVER.capabilities_payload(),
        }
        bad_health = self.runtime.worker_health()
        bad_health["received"] = 1
        bad_status = {
            "schema": RECEIVER.STATUS_RESPONSE_SCHEMA,
            "notification_id": "terminal-" + "a" * 64,
            "event_id": "other-event",
            "site_id": "public-site",
            "terminal_status": "published",
            "notification_sha256": "b" * 64,
            "processing_status": "pending",
            "attempts": 0,
            "max_attempts": 5,
            "next_attempt_at": 0.0,
            "lease_expires_at": None,
            "lease_expired": False,
            "last_error_code": None,
            "processed_at": None,
        }
        for operation, result, code in (
            ("health", json_result(200, bad_health), "health_binding"),
            ("status", json_result(200, bad_status), "status_binding"),
        ):
            with self.subTest(operation=operation):
                transport = FakeTransport([
                    json_result(200, capabilities), result,
                ])
                client = CLIENT.HTTPTerminalReceiverClient(
                    "https://cms.example.test", self.digest,
                    lambda _request: {"Authorization": "x"},
                    transport=transport,
                )
                with self.assertRaises(
                    CLIENT.TerminalReceiverClientBlocked
                ) as caught:
                    if operation == "health":
                        client.health()
                    else:
                        client.status("cms-event-184", "public-site")
                self.assertEqual(
                    caught.exception.code,
                    "terminal_receiver_client." + code,
                )

    def test_redirect_transport_and_private_failures_are_content_free(self):
        secret = "private transport diagnostic"
        cases = (
            (
                CLIENT.HTTPResult(302, (("Content-Type", "text/plain"),), b""),
                "terminal_receiver_client.redirect", False,
            ),
            (
                RuntimeError(secret),
                "terminal_receiver_client.network", True,
            ),
        )
        for result, code, retryable in cases:
            with self.subTest(code=code):
                transport = FakeTransport([result])
                client = CLIENT.HTTPTerminalReceiverClient(
                    "https://cms.example.test", self.digest,
                    lambda _request: {"Authorization": "x"},
                    transport=transport,
                )
                with self.assertRaises(
                    CLIENT.TerminalReceiverClientBlocked
                ) as caught:
                    client.capabilities()
                self.assertEqual((caught.exception.code, caught.exception.retryable), (
                    code, retryable,
                ))
                self.assertNotIn(secret, str(caught.exception))

    def test_unsafe_configuration_and_authentication_headers_block(self):
        for origin in (
            "http://cms.example.test", "https://user@cms.example.test",
            "https://cms.example.test/path", "https://cms.example.test?query=1",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                CLIENT.HTTPTerminalReceiverClient(
                    origin, self.digest, lambda _request: {"Authorization": "x"},
                )
        loopback = CLIENT.HTTPTerminalReceiverClient(
            "http://127.0.0.1:8080", self.digest,
            lambda _request: {"Authorization": "x"},
            allow_loopback_http=True,
        )
        self.assertEqual(loopback.origin, "http://127.0.0.1:8080")

        client = CLIENT.HTTPTerminalReceiverClient(
            "https://cms.example.test", self.digest,
            lambda _request: {"Content-Type": "stolen"},
            transport=FakeTransport([]),
        )
        with self.assertRaises(CLIENT.TerminalReceiverClientBlocked) as caught:
            client.capabilities()
        self.assertEqual(caught.exception.code, (
            "terminal_receiver_client.authentication"
        ))


if __name__ == "__main__":
    unittest.main()
