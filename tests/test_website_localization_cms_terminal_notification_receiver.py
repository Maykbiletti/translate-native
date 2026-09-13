from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
    "blun_test_terminal_receiver_outbox",
    ROOT / "integrations" / "website_localization_cms_terminal_notification.py",
)
HTTP = load(
    "blun_test_terminal_receiver_http",
    ROOT / "integrations" / "website_localization_cms_terminal_notification_http.py",
)
RECEIVER = load(
    "blun_test_terminal_receiver",
    ROOT
    / "integrations"
    / "website_localization_cms_terminal_notification_receiver.py",
)


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


def principal(site_id="public-site", **overrides):
    value = {
        "schema": RECEIVER.PRINCIPAL_SCHEMA,
        "principal_id": "website-cms",
        "credential_id": "cms-key",
        "credential_version": "v1",
        "scope": RECEIVER.WRITE_SCOPE,
        "site_id": site_id,
    }
    value.update(overrides)
    return value


class WSGITransport:
    def __init__(self, application, *, lose_first=False):
        self.application = application
        self.lose_first = lose_first
        self.calls = 0

    def post(self, url, headers, body, *, timeout):
        del timeout
        self.calls += 1
        parsed = urllib.parse.urlsplit(url)
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "wsgi.url_scheme": parsed.scheme,
            "CONTENT_TYPE": headers["Content-Type"],
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        for name, value in headers.items():
            if name.lower() not in {"content-type", "content-length"}:
                environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = tuple(response_headers)

        chunks = self.application(environ, start_response)
        if self.lose_first and self.calls == 1:
            return HTTP.HTTPResult(503, (), b"")
        return HTTP.HTTPResult(
            captured["status"], captured["headers"], b"".join(chunks),
        )


class TerminalNotificationReceiverTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.inbox = RECEIVER.DurableCMSTerminalNotificationInbox(self.connection)
        self.authentication_calls = []

        def authenticate(request, headers):
            self.authentication_calls.append((copy.deepcopy(request), dict(headers)))
            if headers.get("authorization") != "Bearer exact":
                return principal(site_id="another-site")
            return principal(site_id=request["site_id"])

        self.application = RECEIVER.CMSTerminalNotificationReceiverApplication(
            self.inbox,
            authenticate,
            origin="https://cms.example.test",
            path="/v1/localization/terminal-notifications",
            clock=lambda: 123.5,
        )

    def tearDown(self):
        self.connection.close()

    def adapter(self, transport=None):
        return HTTP.HTTPTerminalNotifierAdapter(
            "https://cms.example.test/v1/localization/terminal-notifications",
            lambda _request: {"Authorization": "Bearer exact"},
            transport=transport or WSGITransport(self.application),
        )

    def test_adapter_to_receiver_stores_before_exact_acknowledgement(self):
        payload = notification()
        result = self.adapter()(payload)

        body = HTTP._canonical(payload)
        expected_sha = hashlib.sha256(body).hexdigest()
        self.assertEqual(result, {
            "schema": RECEIVER.ACK_SCHEMA,
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "status": "accepted",
            "notification_sha256": expected_sha,
        })
        stored = self.inbox.status(payload["event_id"])
        self.assertEqual((stored.notification_id, stored.payload_sha256), (
            payload["notification_id"], expected_sha,
        ))
        request, headers = self.authentication_calls[0]
        self.assertEqual(request, {
            "schema": RECEIVER.AUTH_SCHEMA,
            "method": "POST",
            "origin": "https://cms.example.test",
            "path": "/v1/localization/terminal-notifications",
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "body_sha256": expected_sha,
        })
        self.assertEqual(headers["authorization"], "Bearer exact")

    def test_lost_ack_replay_converges_on_one_durable_row(self):
        payload = notification()
        transport = WSGITransport(self.application, lose_first=True)
        callback = self.adapter(transport)
        source = sqlite3.connect(":memory:")
        outbox = NOTIFY.DurableCMSTerminalNotifier(
            source, base_delay_seconds=2, max_delay_seconds=2,
        )
        outbox.register(terminal(), max_attempts=2, now=100)

        first = outbox.run_once(callback, "worker", now=100, lease_seconds=60)
        second = outbox.run_once(callback, "worker", now=102, lease_seconds=60)

        self.assertEqual(first.status, "retry_wait")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(transport.calls, 2)
        rows = self.connection.execute(
            "SELECT COUNT(*), MIN(received_at), MAX(received_at) "
            "FROM cms_terminal_notification_inbox"
        ).fetchone()
        self.assertEqual(tuple(rows), (1, 123.5, 123.5))
        self.assertEqual(self.inbox.status(payload["event_id"]).received_at, 123.5)
        source.close()

    def test_changed_replay_under_same_event_is_a_permanent_collision(self):
        self.adapter()(notification())
        changed = notification(website_version="release-43", source_sequence=43)
        transport = WSGITransport(self.application)

        with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as caught:
            self.adapter(transport)(changed)

        self.assertEqual((caught.exception.code, caught.exception.retryable), (
            "notification_http.http_status", False,
        ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM cms_terminal_notification_inbox"
        ).fetchone()[0], 1)

    def test_authentication_and_site_authorization_fail_before_storage(self):
        app = RECEIVER.CMSTerminalNotificationReceiverApplication(
            self.inbox,
            lambda _request, _headers: principal(site_id="another-site"),
            origin="https://cms.example.test",
            clock=lambda: 10,
        )
        transport = WSGITransport(app)
        callback = HTTP.HTTPTerminalNotifierAdapter(
            "https://cms.example.test/v1/localization/terminal-notifications",
            lambda _request: {"Authorization": "Bearer wrong-site"},
            transport=transport,
        )
        with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as caught:
            callback(notification())
        self.assertEqual((caught.exception.code, caught.exception.retryable), (
            "notification_http.http_status", False,
        ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM cms_terminal_notification_inbox"
        ).fetchone()[0], 0)

    def test_header_body_and_transport_constraints_fail_closed(self):
        payload = notification()
        body = HTTP._canonical(payload)
        body_sha = hashlib.sha256(body).hexdigest()
        base = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": RECEIVER.DEFAULT_PATH,
            "QUERY_STRING": "",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json; charset=utf-8",
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_AUTHORIZATION": "Bearer exact",
            "HTTP_IDEMPOTENCY_KEY": payload["notification_id"],
            "HTTP_X_LOCALIZATION_TERMINAL_NOTIFICATION_ID": payload[
                "notification_id"
            ],
            "HTTP_X_LOCALIZATION_TERMINAL_NOTIFICATION_SHA256": body_sha,
        }
        cases = (
            ("http", {}, 400),
            ("query", {"QUERY_STRING": "debug=1"}, 400),
            ("transfer", {"HTTP_TRANSFER_ENCODING": "chunked"}, 400),
            ("hash", {
                "HTTP_X_LOCALIZATION_TERMINAL_NOTIFICATION_SHA256": "0" * 64,
            }, 400),
            ("content-type", {"CONTENT_TYPE": "text/plain"}, 415),
        )
        for name, changes, expected_status in cases:
            with self.subTest(name=name):
                environ = dict(base)
                environ.update(changes)
                if name == "http":
                    environ["wsgi.url_scheme"] = "http"
                environ["wsgi.input"] = io.BytesIO(body)
                captured = {}
                chunks = self.application(
                    environ,
                    lambda status, _headers: captured.update(status=status),
                )
                response = json.loads(b"".join(chunks))
                self.assertEqual(int(captured["status"].split()[0]), expected_status)
                self.assertEqual(response["status"], "BLOCK")
                self.assertNotIn("Bearer", repr(response))

    def test_noncanonical_json_and_duplicate_keys_are_rejected(self):
        payload = notification()
        canonical = HTTP._canonical(payload)
        noncanonical = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        duplicate = canonical[:-1] + b',"site_id":"public-site"}'
        for name, body in (("noncanonical", noncanonical), ("duplicate", duplicate)):
            with self.subTest(name=name):
                with self.assertRaises(
                    RECEIVER.TerminalNotificationReceiverBlocked
                ) as caught:
                    RECEIVER._parse_notification(body)
                self.assertEqual(caught.exception.status, 400)

    def test_receiver_configuration_rejects_ambiguous_origins_and_paths(self):
        for origin in (
            "http://cms.example.test",
            "https://user:secret@cms.example.test",
            "https://cms.example.test/notify",
            "https://cms.example.test:99999",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                RECEIVER.CMSTerminalNotificationReceiverApplication(
                    self.inbox,
                    lambda _request, _headers: principal(),
                    origin=origin,
                    clock=lambda: 1,
                )
        with self.assertRaises(ValueError):
            RECEIVER.CMSTerminalNotificationReceiverApplication(
                self.inbox,
                lambda _request, _headers: principal(),
                origin="https://cms.example.test",
                path="//ambiguous",
                clock=lambda: 1,
            )

    def test_restart_preserves_exact_idempotency_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "terminal.sqlite3")
            first_connection = sqlite3.connect(path)
            first = RECEIVER.DurableCMSTerminalNotificationInbox(first_connection)
            payload = notification()
            body = HTTP._canonical(payload)
            body_sha = hashlib.sha256(body).hexdigest()
            first.accept(payload, body, body_sha, now=75)
            first_connection.close()

            second_connection = sqlite3.connect(path)
            second = RECEIVER.DurableCMSTerminalNotificationInbox(second_connection)
            replay = second.accept(payload, body, body_sha, now=99)
            self.assertEqual(replay["notification_sha256"], body_sha)
            self.assertEqual(second.status(payload["event_id"]).received_at, 75)
            second_connection.execute(
                "UPDATE cms_terminal_notification_inbox SET site_id = 'other-site'"
            )
            second_connection.commit()
            with self.assertRaises(
                RECEIVER.TerminalNotificationReceiverBlocked
            ) as caught:
                second.status(payload["event_id"])
            self.assertEqual(caught.exception.code, (
                "notification_receiver.stored_binding_invalid"
            ))
            second_connection.close()

    def test_parallel_exact_replays_store_one_notification(self):
        payload = notification()

        def send(_index):
            return self.adapter()(payload)["status"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(send, range(24)))

        self.assertEqual(statuses, ["accepted"] * 24)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM cms_terminal_notification_inbox"
        ).fetchone()[0], 1)

    def test_damaged_schema_and_private_authenticator_errors_are_redacted(self):
        self.connection.execute(
            "ALTER TABLE cms_terminal_notification_inbox ADD COLUMN injected TEXT"
        )
        self.connection.commit()
        transport = WSGITransport(self.application)
        with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as damaged:
            self.adapter(transport)(notification())
        self.assertEqual((damaged.exception.code, damaged.exception.retryable), (
            "notification_http.http_status", True,
        ))

        fresh = RECEIVER.DurableCMSTerminalNotificationInbox(
            sqlite3.connect(":memory:")
        )
        app = RECEIVER.CMSTerminalNotificationReceiverApplication(
            fresh,
            lambda _request, _headers: (_ for _ in ()).throw(
                RuntimeError("private verifier detail")
            ),
            origin="https://cms.example.test",
            clock=lambda: 1,
        )
        transport = WSGITransport(app)
        callback = HTTP.HTTPTerminalNotifierAdapter(
            "https://cms.example.test/v1/localization/terminal-notifications",
            lambda _request: {"Authorization": "Bearer exact"},
            transport=transport,
        )
        with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as auth:
            callback(notification())
        self.assertEqual((auth.exception.code, auth.exception.retryable), (
            "notification_http.http_status", True,
        ))
        self.assertNotIn("private", str(auth.exception))
        fresh.connection.close()


if __name__ == "__main__":
    unittest.main()
