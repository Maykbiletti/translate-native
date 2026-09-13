from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


NOTIFY = load(
    "blun_test_terminal_notification_http_outbox",
    ROOT / "integrations" / "website_localization_cms_terminal_notification.py",
)
HTTP = load(
    "blun_test_terminal_notification_http",
    ROOT
    / "integrations"
    / "website_localization_cms_terminal_notification_http.py",
)


class FakeTransport:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if isinstance(self.responder, Exception):
            raise self.responder
        return (
            self.responder(body)
            if callable(self.responder)
            else self.responder
        )


class BrokenHeaders(dict):
    def items(self):
        raise RuntimeError("private authentication failure")


def terminal(**overrides):
    values = {
        "state": "terminal",
        "event_id": "cms-event-184",
        "site_id": "public-site",
        "plan_id": "plan-cms-event-184",
        "website_version": "release-42",
        "source_sequence": 42,
        "job_count": 23,
        "change_sha256": "a" * 64,
        "binding_sha256": "b" * 64,
        "remote_status": "published",
        "lifecycle_sha256": "c" * 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def notification(**overrides):
    connection = sqlite3.connect(":memory:")
    outbox = NOTIFY.DurableCMSTerminalNotifier(connection)
    outbox.register(terminal(**overrides), now=100)
    raw = connection.execute(
        "SELECT payload_json FROM cms_source_terminal_notification"
    ).fetchone()[0]
    connection.close()
    return json.loads(raw)


def response_for(body, mutate=None):
    payload = json.loads(body.decode("utf-8"))
    acknowledgement = {
        "schema": HTTP.ACK_SCHEMA,
        "notification_id": payload["notification_id"],
        "event_id": payload["event_id"],
        "site_id": payload["site_id"],
        "status": "accepted",
        "notification_sha256": hashlib.sha256(body).hexdigest(),
    }
    if mutate is not None:
        mutate(acknowledgement)
    raw = json.dumps(
        acknowledgement,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return HTTP.HTTPResult(
        200,
        (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw))),
        ),
        raw,
    )


def adapter(transport, authentication=None, **overrides):
    return HTTP.HTTPTerminalNotifierAdapter(
        "https://cms.example.test/v1/localization/terminal-notifications",
        authentication or (lambda _request: {"Authorization": "Bearer secret"}),
        transport=transport,
        **overrides,
    )


class HTTPTerminalNotifierAdapterTests(unittest.TestCase):
    def test_posts_exact_immutable_notification_with_bound_authentication(self):
        transport = FakeTransport(response_for)
        authentication_requests = []

        def authentication(request):
            authentication_requests.append(dict(request))
            request["body_sha256"] = "0" * 64
            return {"Authorization": "HMAC exact-body-hash"}

        callback = adapter(transport, authentication)
        payload = notification()
        result = callback(payload)

        self.assertEqual(len(transport.calls), 1)
        url, headers, body, timeout = transport.calls[0]
        expected_body = HTTP._canonical(payload)
        expected_hash = hashlib.sha256(expected_body).hexdigest()
        self.assertEqual(body, expected_body)
        self.assertEqual(url, callback.endpoint)
        self.assertEqual(timeout, 30.0)
        self.assertEqual(headers["Idempotency-Key"], payload["notification_id"])
        self.assertEqual(
            headers["X-Localization-Terminal-Notification-Sha256"],
            expected_hash,
        )
        self.assertEqual(authentication_requests, [{
            "schema": HTTP.AUTH_SCHEMA,
            "method": "POST",
            "origin": "https://cms.example.test",
            "path": "/v1/localization/terminal-notifications",
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "body_sha256": expected_hash,
        }])
        self.assertEqual(result["notification_sha256"], expected_hash)
        self.assertNotIn("secret", repr(callback))

    def test_outbox_owns_retries_and_replays_the_same_request(self):
        calls = 0

        def responder(body):
            nonlocal calls
            calls += 1
            if calls == 1:
                return HTTP.HTTPResult(503, (), b"")
            return response_for(body)

        transport = FakeTransport(responder)
        callback = adapter(transport)
        connection = sqlite3.connect(":memory:")
        outbox = NOTIFY.DurableCMSTerminalNotifier(
            connection, base_delay_seconds=2,
        )
        outbox.register(terminal(), max_attempts=2, now=100)

        first = outbox.run_once(
            callback, "notification-worker", now=100, lease_seconds=60,
        )
        self.assertEqual((first.status, first.error_code), (
            "retry_wait", "notification_http.http_status",
        ))
        self.assertIsNone(outbox.run_once(
            callback, "notification-worker", now=101, lease_seconds=60,
        ))
        second = outbox.run_once(
            callback, "notification-worker", now=102, lease_seconds=60,
        )
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][2], transport.calls[1][2])
        connection.close()

    def test_statuses_have_one_attempt_and_bounded_retry_semantics(self):
        for status, code, retryable in (
            (302, "notification_http.redirect", False),
            (401, "notification_http.http_status", False),
            (408, "notification_http.http_status", True),
            (425, "notification_http.http_status", True),
            (429, "notification_http.http_status", True),
            (503, "notification_http.http_status", True),
        ):
            with self.subTest(status=status):
                transport = FakeTransport(
                    lambda _body, status=status: HTTP.HTTPResult(
                        status, (), b"private gateway body",
                    )
                )
                with self.assertRaises(
                    HTTP.HTTPTerminalNotificationFailed
                ) as caught:
                    adapter(transport)(notification())
                self.assertEqual(
                    (caught.exception.code, caught.exception.retryable),
                    (code, retryable),
                )
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("private", str(caught.exception))

    def test_endpoint_and_authentication_block_before_transport(self):
        invalid_endpoints = (
            "http://cms.example.test/notify",
            "https://user:secret@cms.example.test/notify",
            "https://cms.example.test/notify?token=secret",
            "https://cms.example.test",
        )
        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    HTTP.HTTPTerminalNotifierAdapter(
                        endpoint, lambda _request: {"Authorization": "x"},
                    )

        for supplied in (
            {},
            {"Content-Type": "text/plain"},
            {"Authorization": "one", "authorization": "two"},
            {"Authorization": "Bearer x\nX-Evil: yes"},
            {"Bad Header": "value"},
            BrokenHeaders(Authorization="Bearer private"),
        ):
            with self.subTest(supplied=supplied):
                transport = FakeTransport(response_for)
                with self.assertRaises(
                    HTTP.HTTPTerminalNotificationFailed
                ) as caught:
                    adapter(
                        transport,
                        lambda _request, supplied=supplied: supplied,
                    )(notification())
                self.assertEqual(
                    caught.exception.code, "notification_http.authentication",
                )
                self.assertEqual(transport.calls, [])

    def test_request_binding_is_recomputed_before_authentication(self):
        payload = notification()
        authentication_calls = []
        transport = FakeTransport(response_for)
        for mutate in (
            lambda value: value.update(notification_id="terminal-" + "0" * 64),
            lambda value: value.update(job_count=True),
            lambda value: value.update(terminal_status="processing"),
            lambda value: value.update(lifecycle_sha256=None),
            lambda value: value.update(extra="unexpected"),
        ):
            with self.subTest(mutate=mutate):
                changed = dict(payload)
                mutate(changed)
                with self.assertRaises(
                    HTTP.HTTPTerminalNotificationFailed
                ) as caught:
                    adapter(
                        transport,
                        lambda request: authentication_calls.append(request),
                    )(changed)
                self.assertEqual(
                    caught.exception.code, "notification_http.request_invalid",
                )
        self.assertEqual(authentication_calls, [])
        self.assertEqual(transport.calls, [])

    def test_response_parser_and_binding_fail_closed(self):
        valid = response_for(HTTP._canonical(notification()))
        cases = (
            HTTP.HTTPResult(
                200, (("Content-Type", "text/html"),), b"<p>accepted</p>",
            ),
            HTTP.HTTPResult(
                200, (("Content-Type", "application/json"),), b"{\"x\":1,\"x\":2}",
            ),
            HTTP.HTTPResult(
                200,
                (("Content-Type", "application/json"), ("content-type", "text/json")),
                valid.body,
            ),
            HTTP.HTTPResult(
                200,
                (("Content-Type", "application/json"), ("Content-Length", "1")),
                valid.body,
            ),
            HTTP.HTTPResult(
                200,
                (("Content-Type", "application/json"),),
                b"\xef\xbb\xbf" + valid.body,
            ),
            HTTP.HTTPResult(
                200,
                (("Content-Type", "application/json"),),
                b"x" * (HTTP.MAX_RESPONSE_BYTES + 1),
            ),
            lambda body: response_for(
                body, lambda value: value.update(event_id="other-event"),
            ),
            lambda body: response_for(
                body, lambda value: value.update(notification_sha256="0" * 64),
            ),
        )
        for responder in cases:
            with self.subTest(responder=responder):
                transport = FakeTransport(responder)
                with self.assertRaises(
                    HTTP.HTTPTerminalNotificationFailed
                ) as caught:
                    adapter(transport)(notification())
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(transport.calls), 1)

    def test_private_network_failure_is_content_free_and_retryable(self):
        secret = "private-network-detail"
        transport = FakeTransport(RuntimeError(secret))
        with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as caught:
            adapter(transport)(notification())
        self.assertEqual((caught.exception.code, caught.exception.retryable), (
            "notification_http.network", True,
        ))
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_loopback_http_requires_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            HTTP.HTTPTerminalNotifierAdapter(
                "http://127.0.0.1:8080/notify",
                lambda _request: {"Authorization": "x"},
            )
        created = HTTP.HTTPTerminalNotifierAdapter(
            "http://[::1]:8080/notify",
            lambda _request: {"Authorization": "x"},
            allow_loopback_http=True,
        )
        self.assertEqual(created.origin, "http://[::1]:8080")


if __name__ == "__main__":
    unittest.main()
