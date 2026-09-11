from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


NOTIFY = load(
    "blun_test_terminal_receiver_runtime_outbox",
    ROOT / "integrations" / "website_localization_cms_terminal_notification.py",
)
HTTP = load(
    "blun_test_terminal_receiver_runtime_http",
    ROOT / "integrations" / "website_localization_cms_terminal_notification_http.py",
)
RUNTIME = load(
    "blun_test_terminal_receiver_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_terminal_notification_receiver_runtime.py",
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


def principal(site_id="public-site", scope=None):
    return {
        "schema": RUNTIME._RECEIVER.PRINCIPAL_SCHEMA,
        "principal_id": "website-cms",
        "credential_id": "cms-key",
        "credential_version": "v1",
        "scope": scope or RUNTIME._RECEIVER.WRITE_SCOPE,
        "site_id": site_id,
    }


def authenticate(request, headers):
    if headers.get("authorization") != "Bearer exact":
        raise RuntimeError("private credential failure")
    scopes = {
        RUNTIME._RECEIVER.DEFAULT_PATH: RUNTIME._RECEIVER.WRITE_SCOPE,
        RUNTIME._RECEIVER.STATUS_PATH: RUNTIME._RECEIVER.STATUS_SCOPE,
        RUNTIME._RECEIVER.READINESS_PATH: RUNTIME._RECEIVER.READINESS_SCOPE,
        RUNTIME._RECEIVER.CAPABILITIES_PATH: (
            RUNTIME._RECEIVER.CAPABILITIES_SCOPE
        ),
        RUNTIME._RECEIVER.HEALTH_PATH: RUNTIME._RECEIVER.HEALTH_SCOPE,
    }
    return principal(request.get("site_id", "public-site"), scopes[request["path"]])


class WSGITransport:
    def __init__(self, application):
        self.application = application

    def post(self, url, headers, body, *, timeout):
        del timeout
        request_headers = dict(headers)
        request_headers["Content-Length"] = str(len(body))
        return self.request("POST", url, request_headers, body)

    def request(self, method, url, headers, body=b""):
        parsed = urllib.parse.urlsplit(url)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "wsgi.url_scheme": parsed.scheme,
            "wsgi.input": io.BytesIO(body),
        }
        if "Content-Type" in headers:
            environ["CONTENT_TYPE"] = headers["Content-Type"]
        if "Content-Length" in headers:
            environ["CONTENT_LENGTH"] = headers["Content-Length"]
        for name, value in headers.items():
            if name.lower() not in {"content-type", "content-length"}:
                environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = tuple(response_headers)

        result = self.application(environ, start_response)
        return HTTP.HTTPResult(
            captured["status"], captured["headers"], b"".join(result),
        )


def control_request(runtime, path, *, method="GET", value=None, headers=None):
    body = b"" if value is None else RUNTIME._RECEIVER._canonical(value)
    request_headers = {"Authorization": "Bearer exact"}
    if value is not None:
        request_headers.update({
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "X-Localization-Terminal-Status-SHA256": hashlib.sha256(
                body
            ).hexdigest(),
        })
    if headers:
        request_headers.update(headers)
    return WSGITransport(runtime).request(
        method, "https://cms.example.test" + path, request_headers, body,
    )


def adapter(application):
    return HTTP.HTTPTerminalNotifierAdapter(
        "https://cms.example.test/v1/localization/terminal-notifications",
        lambda _request: {"Authorization": "Bearer exact"},
        transport=WSGITransport(application),
    )


def processing_ack(payload):
    body = HTTP._canonical(payload)
    return {
        "schema": RUNTIME._RECEIVER.PROCESSING_ACK_SCHEMA,
        "notification_id": payload["notification_id"],
        "event_id": payload["event_id"],
        "site_id": payload["site_id"],
        "status": "processed",
        "notification_sha256": hashlib.sha256(body).hexdigest(),
    }


def open_runtime(path):
    return RUNTIME.open_durable_terminal_notification_receiver(
        path,
        authenticate,
        origin="https://cms.example.test",
        clock=lambda: 123.5,
    )


class DurableTerminalReceiverRuntimeTests(unittest.TestCase):
    @staticmethod
    def wait_for(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("condition was not reached")

    def test_safe_file_runtime_accepts_and_reports_one_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.sqlite3"
            runtime = open_runtime(path)
            payload = notification()

            acknowledgement = adapter(runtime)(payload)

            self.assertEqual(acknowledgement["notification_id"], (
                payload["notification_id"]
            ))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            status = runtime.status(payload["event_id"])
            self.assertEqual((status.site_id, status.received_at), (
                "public-site", 123.5,
            ))
            self.assertEqual(runtime.health(), (
                RUNTIME.DurableTerminalReceiverRuntimeHealth(
                    "ok",
                    "open",
                    1,
                    {
                        "pending": 1,
                        "leased": 0,
                        "retry_wait": 0,
                        "succeeded": 0,
                        "failed": 0,
                    },
                    1,
                    0,
                    0,
                )
            ))
            runtime.close()

    def test_restart_replays_the_original_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.sqlite3"
            payload = notification()
            first = open_runtime(path)
            first_ack = adapter(first)(payload)
            first.close()

            second = open_runtime(path)
            second_ack = adapter(second)(payload)

            self.assertEqual(second_ack, first_ack)
            self.assertEqual(second.status(payload["event_id"]).received_at, 123.5)
            self.assertEqual(second.health().received, 1)
            second.close()

    def test_invalid_configuration_creates_no_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.sqlite3"
            for changes in (
                {"origin": "http://cms.example.test"},
                {"path": "//ambiguous"},
                {"path": RUNTIME._RECEIVER.STATUS_PATH},
                {"path": RUNTIME._RECEIVER.READINESS_PATH},
                {"path": RUNTIME._RECEIVER.CAPABILITIES_PATH},
                {"path": RUNTIME._RECEIVER.HEALTH_PATH},
                {"authenticate": None},
                {"processing_max_attempts": 0},
                {
                    "processing_base_delay_seconds": 10,
                    "processing_max_delay_seconds": 5,
                },
            ):
                with self.subTest(changes=changes), self.assertRaises(
                    RUNTIME.DurableTerminalReceiverRuntimeBlocked
                ):
                    options = {
                        "authenticate": authenticate,
                        "origin": "https://cms.example.test",
                        "path": RUNTIME._RECEIVER.DEFAULT_PATH,
                    }
                    options.update(changes)
                    RUNTIME.open_durable_terminal_notification_receiver(
                        path, **options,
                    )
                self.assertFalse(path.exists())

    def test_unsafe_files_and_directories_block_before_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            source.write_bytes(b"")
            source.chmod(0o600)
            hardlink = root / "hardlink.sqlite3"
            os.link(source, hardlink)
            symlink = root / "symlink.sqlite3"
            symlink.symlink_to(source)
            open_file = root / "open.sqlite3"
            open_file.write_bytes(b"")
            open_file.chmod(0o644)
            for path in (source, hardlink, symlink, open_file):
                with self.subTest(path=path), self.assertRaises(
                    RUNTIME.DurableTerminalReceiverRuntimeBlocked
                ):
                    open_runtime(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o777)
            try:
                with self.assertRaises(
                    RUNTIME.DurableTerminalReceiverRuntimeBlocked
                ):
                    open_runtime(root / "terminal.sqlite3")
            finally:
                root.chmod(0o700)

    def test_path_replacement_and_permission_drift_block_every_access(self):
        for operation in ("replace", "permissions"):
            with (
                self.subTest(operation=operation),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "terminal.sqlite3"
                runtime = open_runtime(path)
                if operation == "replace":
                    path.rename(Path(directory) / "original.sqlite3")
                    path.write_bytes(b"")
                    path.chmod(0o600)
                else:
                    path.chmod(0o640)

                with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as caught:
                    adapter(runtime)(notification())
                self.assertEqual((caught.exception.code, caught.exception.retryable), (
                    "notification_http.http_status", True,
                ))
                with self.assertRaises(
                    RUNTIME.DurableTerminalReceiverRuntimeBlocked
                ):
                    runtime.health()
                if operation == "permissions":
                    path.chmod(0o600)
                runtime.close()

    def test_close_and_foreign_process_state_fail_closed(self):
        runtime = open_runtime(":memory:")
        runtime.close()
        self.assertEqual(runtime.state, "closed")
        with self.assertRaises(RUNTIME.DurableTerminalReceiverRuntimeBlocked):
            runtime.health()
        with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as closed:
            adapter(runtime)(notification())
        self.assertTrue(closed.exception.retryable)

        foreign = open_runtime(":memory:")
        with mock.patch.object(RUNTIME.os, "getpid", return_value=os.getpid() + 1):
            self.assertEqual(foreign.state, "foreign-process")
            with self.assertRaises(
                RUNTIME.DurableTerminalReceiverRuntimeBlocked
            ):
                foreign.health()
        foreign.close()

    def test_parallel_runtime_requests_converge_and_close_waits(self):
        runtime = open_runtime(":memory:")
        payload = notification()

        def send(_index):
            return adapter(runtime)(payload)["status"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(send, range(24)))

        self.assertEqual(statuses, ["accepted"] * 24)
        self.assertEqual(runtime.health().received, 1)
        runtime.close()

    def test_runtime_runs_one_durable_host_processing_callback(self):
        runtime = open_runtime(":memory:")
        payload = notification()
        adapter(runtime)(payload)

        outcome = runtime.process_next(
            processing_ack,
            "cms-consumer",
            now=124,
            lease_seconds=30,
        )

        self.assertEqual((outcome.status, outcome.attempt), ("succeeded", 1))
        health = runtime.health()
        self.assertEqual((health.status, health.processing_due), ("ok", 0))
        self.assertEqual(health.processing_counts["succeeded"], 1)
        runtime.close()

    def test_authenticated_status_reports_exact_processing_lifecycle(self):
        runtime = open_runtime(":memory:")
        payload = notification()
        adapter(runtime)(payload)
        request = {
            "schema": RUNTIME._RECEIVER.STATUS_REQUEST_SCHEMA,
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
        }

        pending_result = control_request(
            runtime, RUNTIME._RECEIVER.STATUS_PATH,
            method="POST", value=request,
        )
        pending = json.loads(pending_result.body)
        self.assertEqual(pending_result.status, 200)
        self.assertEqual(pending, {
            "schema": RUNTIME._RECEIVER.STATUS_RESPONSE_SCHEMA,
            "notification_id": payload["notification_id"],
            "event_id": payload["event_id"],
            "site_id": payload["site_id"],
            "terminal_status": payload["terminal_status"],
            "notification_sha256": hashlib.sha256(
                RUNTIME._RECEIVER._canonical(payload)
            ).hexdigest(),
            "processing_status": "pending",
            "attempts": 0,
            "max_attempts": 5,
            "next_attempt_at": 123.5,
            "lease_expires_at": None,
            "lease_expired": False,
            "last_error_code": None,
            "processed_at": None,
        })
        runtime.process_next(
            processing_ack, "cms-consumer", now=124, lease_seconds=30,
        )
        succeeded = json.loads(control_request(
            runtime, RUNTIME._RECEIVER.STATUS_PATH,
            method="POST", value=request,
        ).body)
        self.assertEqual((
            succeeded["processing_status"],
            succeeded["attempts"],
            succeeded["processed_at"],
        ), ("succeeded", 1, 124.0))
        self.assertNotIn("payload", repr(succeeded))
        runtime.close()

    def test_capabilities_are_authenticated_hashed_and_match_active_routes(self):
        requests = []

        def record_authenticate(request, headers):
            requests.append((dict(request), dict(headers)))
            return principal(
                request.get("site_id", "public-site"),
                RUNTIME._RECEIVER.CAPABILITIES_SCOPE,
            )

        runtime = RUNTIME.open_durable_terminal_notification_receiver(
            ":memory:",
            record_authenticate,
            origin="https://cms.example.test",
            clock=lambda: 123.5,
        )
        runtime.inbox.processing_status = mock.Mock()
        runtime.worker_readiness = mock.Mock()
        runtime.worker_health = mock.Mock()
        changes = runtime._connection.total_changes

        response = control_request(
            runtime, RUNTIME._RECEIVER.CAPABILITIES_PATH,
        )

        self.assertEqual(response.status, 200)
        value = json.loads(response.body)
        self.assertEqual(
            value["schema"], RUNTIME._RECEIVER.CAPABILITIES_RESPONSE_SCHEMA,
        )
        capabilities = value["capabilities"]
        claimed = capabilities.pop("sha256")
        self.assertEqual(
            claimed,
            hashlib.sha256(
                RUNTIME._RECEIVER._canonical(capabilities)
            ).hexdigest(),
        )
        self.assertEqual(
            capabilities["schema"], RUNTIME._RECEIVER.CAPABILITIES_SCHEMA,
        )
        self.assertEqual(set(capabilities["operations"]), {
            "capabilities", "health", "notification", "readiness", "status",
        })
        expected = {
            "capabilities": (
                "GET", RUNTIME._RECEIVER.CAPABILITIES_PATH,
                RUNTIME._RECEIVER.CAPABILITIES_SCOPE,
            ),
            "notification": (
                "POST", RUNTIME._RECEIVER.DEFAULT_PATH,
                RUNTIME._RECEIVER.WRITE_SCOPE,
            ),
            "health": (
                "GET", RUNTIME._RECEIVER.HEALTH_PATH,
                RUNTIME._RECEIVER.HEALTH_SCOPE,
            ),
            "readiness": (
                "GET", RUNTIME._RECEIVER.READINESS_PATH,
                RUNTIME._RECEIVER.READINESS_SCOPE,
            ),
            "status": (
                "POST", RUNTIME._RECEIVER.STATUS_PATH,
                RUNTIME._RECEIVER.STATUS_SCOPE,
            ),
        }
        for name, (method, path, scope) in expected.items():
            operation = capabilities["operations"][name]
            self.assertEqual(
                (operation["method"], operation["path"], operation["scope"]),
                (method, path, scope),
            )
        self.assertEqual(
            capabilities["limits"]["max_body_bytes"],
            RUNTIME._RECEIVER.MAX_BODY_BYTES,
        )
        self.assertEqual(requests[0][0], {
            "schema": RUNTIME._RECEIVER.AUTH_SCHEMA,
            "method": "GET",
            "origin": "https://cms.example.test",
            "path": RUNTIME._RECEIVER.CAPABILITIES_PATH,
            "body_sha256": hashlib.sha256(b"").hexdigest(),
        })
        self.assertEqual(runtime._connection.total_changes, changes)
        runtime.inbox.processing_status.assert_not_called()
        runtime.worker_readiness.assert_not_called()
        runtime.worker_health.assert_not_called()
        self.assertNotIn("public-site", json.dumps(capabilities))
        runtime.close()

    def test_capabilities_advertise_custom_intake_and_drift_blocks(self):
        custom_path = "/receiver/v2/terminal"

        def custom_authenticate(request, _headers):
            return principal(
                request.get("site_id", "public-site"),
                RUNTIME._RECEIVER.CAPABILITIES_SCOPE,
            )

        runtime = RUNTIME.open_durable_terminal_notification_receiver(
            ":memory:",
            custom_authenticate,
            origin="https://cms.example.test",
            path=custom_path,
        )
        advertised = json.loads(control_request(
            runtime, RUNTIME._RECEIVER.CAPABILITIES_PATH,
        ).body)["capabilities"]
        self.assertEqual(
            advertised["operations"]["notification"]["path"], custom_path,
        )

        incomplete_fields = set(RUNTIME._RECEIVER.NOTIFICATION_FIELDS)
        incomplete_fields.remove("terminal_status")
        with mock.patch.object(
            RUNTIME._RECEIVER, "NOTIFICATION_FIELDS", incomplete_fields,
        ):
            blocked = control_request(
                runtime, RUNTIME._RECEIVER.CAPABILITIES_PATH,
            )
        self.assertEqual(blocked.status, 503)
        self.assertEqual(json.loads(blocked.body)["error_code"], (
            "notification_receiver.capabilities_invalid"
        ))
        runtime.close()

    def test_status_is_read_only_and_cross_site_matches_missing_event(self):
        runtime = open_runtime(":memory:")
        payload = notification()
        adapter(runtime)(payload)
        changes = runtime._connection.total_changes

        def status(event_id, site_id):
            return control_request(
                runtime,
                RUNTIME._RECEIVER.STATUS_PATH,
                method="POST",
                value={
                    "schema": RUNTIME._RECEIVER.STATUS_REQUEST_SCHEMA,
                    "event_id": event_id,
                    "site_id": site_id,
                },
            )

        foreign = status(payload["event_id"], "another-site")
        missing = status("missing-event", "public-site")
        self.assertEqual((foreign.status, foreign.body), (
            missing.status, missing.body,
        ))
        self.assertEqual(foreign.status, 404)
        self.assertEqual(runtime._connection.total_changes, changes)
        runtime.close()

    def test_readiness_route_tracks_managed_worker_without_counts(self):
        runtime = open_runtime(":memory:")
        unmanaged = control_request(
            runtime, RUNTIME._RECEIVER.READINESS_PATH,
        )
        self.assertEqual(unmanaged.status, 503)
        self.assertEqual(json.loads(unmanaged.body)["worker_state"], "unmanaged")
        runtime.start_worker(
            processing_ack,
            "cms-consumer",
            active_delay_seconds=0.01,
            idle_delay_seconds=60,
            blocked_delay_seconds=60,
        )
        self.wait_for(lambda: runtime.worker_state == "running")
        ready = control_request(runtime, RUNTIME._RECEIVER.READINESS_PATH)
        self.assertEqual(ready.status, 200)
        self.assertEqual(json.loads(ready.body), runtime.worker_readiness())
        self.assertNotIn("received", ready.body.decode("utf-8"))
        runtime.stop_worker(timeout_seconds=1)
        stopped = control_request(runtime, RUNTIME._RECEIVER.READINESS_PATH)
        self.assertEqual(stopped.status, 503)
        self.assertEqual(json.loads(stopped.body)["worker_state"], "stopped")
        runtime.close()

    def test_health_route_reports_aggregate_state_without_mutating_it(self):
        runtime = open_runtime(":memory:")
        payload = notification()
        adapter(runtime)(payload)
        before = runtime._connection.total_changes

        healthy_result = control_request(
            runtime, RUNTIME._RECEIVER.HEALTH_PATH,
        )
        healthy = json.loads(healthy_result.body)
        self.assertEqual(healthy_result.status, 200)
        self.assertEqual(healthy, {
            "schema": RUNTIME._RECEIVER.HEALTH_RESPONSE_SCHEMA,
            "status": "ok",
            "runtime_state": "open",
            "worker_state": "unmanaged",
            "inbox_status": "ok",
            "received": 1,
            "processing_counts": {
                "pending": 1,
                "leased": 0,
                "retry_wait": 0,
                "succeeded": 0,
                "failed": 0,
            },
            "processing_due": 1,
            "expired_leases": 0,
            "failed": 0,
            "error_code": None,
        })
        self.assertEqual(runtime._connection.total_changes, before)
        self.assertNotIn(payload["event_id"], repr(healthy))
        self.assertNotIn(payload["site_id"], repr(healthy))
        self.assertNotIn(payload["notification_id"], repr(healthy))

        runtime.process_next(
            lambda _payload: (_ for _ in ()).throw(
                RuntimeError("private CMS handler failure")
            ),
            "cms-consumer",
            now=124,
        )
        before = runtime._connection.total_changes
        blocked_result = control_request(
            runtime, RUNTIME._RECEIVER.HEALTH_PATH,
        )
        blocked = json.loads(blocked_result.body)
        self.assertEqual(blocked_result.status, 503)
        self.assertEqual((
            blocked["status"], blocked["inbox_status"],
            blocked["processing_counts"]["failed"], blocked["failed"],
            blocked["error_code"],
        ), (
            "blocked", "blocked", 1, 1,
            "notification_receiver.storage_blocked",
        ))
        self.assertEqual(runtime._connection.total_changes, before)
        self.assertNotIn("private CMS handler failure", repr(blocked))
        runtime.close()

    def test_health_route_reports_worker_and_storage_failures_fail_closed(self):
        runtime = open_runtime(":memory:")
        runtime.start_worker(
            processing_ack,
            "cms-consumer",
            active_delay_seconds=0.01,
            idle_delay_seconds=60,
            blocked_delay_seconds=60,
        )
        self.wait_for(lambda: runtime.worker_state == "running")
        runtime.stop_worker(timeout_seconds=1)
        stopped = json.loads(control_request(
            runtime, RUNTIME._RECEIVER.HEALTH_PATH,
        ).body)
        self.assertEqual((
            stopped["status"], stopped["worker_state"],
            stopped["inbox_status"], stopped["error_code"],
        ), (
            "blocked", "stopped", "ok",
            "notification_receiver.worker_not_ready",
        ))

        secret = "private damaged store detail"
        runtime.health = mock.Mock(side_effect=RuntimeError(secret))
        unavailable_result = control_request(
            runtime, RUNTIME._RECEIVER.HEALTH_PATH,
        )
        unavailable = json.loads(unavailable_result.body)
        self.assertEqual(unavailable_result.status, 503)
        self.assertEqual(unavailable, {
            "schema": RUNTIME._RECEIVER.HEALTH_RESPONSE_SCHEMA,
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
        })
        self.assertNotIn(secret, unavailable_result.body.decode("utf-8"))

        runtime.worker_health = mock.Mock(return_value={
            **unavailable,
            "unexpected": "field",
        })
        drift = control_request(runtime, RUNTIME._RECEIVER.HEALTH_PATH)
        self.assertEqual(drift.status, 503)
        self.assertEqual(json.loads(drift.body)["error_code"], (
            "notification_receiver.health_invalid"
        ))
        runtime.close()

    def test_control_routes_require_separate_scopes_before_store_access(self):
        def write_only(request, _headers):
            return principal(request.get("site_id", "public-site"))

        runtime = RUNTIME.open_durable_terminal_notification_receiver(
            ":memory:",
            write_only,
            origin="https://cms.example.test",
            clock=lambda: 123.5,
        )
        runtime.inbox.processing_status = mock.Mock()
        runtime.worker_health = mock.Mock()
        status = control_request(
            runtime,
            RUNTIME._RECEIVER.STATUS_PATH,
            method="POST",
            value={
                "schema": RUNTIME._RECEIVER.STATUS_REQUEST_SCHEMA,
                "event_id": "cms-event-184",
                "site_id": "public-site",
            },
        )
        readiness = control_request(
            runtime, RUNTIME._RECEIVER.READINESS_PATH,
        )
        capabilities = control_request(
            runtime, RUNTIME._RECEIVER.CAPABILITIES_PATH,
        )
        health = control_request(runtime, RUNTIME._RECEIVER.HEALTH_PATH)
        self.assertEqual(
            (status.status, readiness.status, capabilities.status, health.status),
            (403, 403, 403, 403),
        )
        runtime.inbox.processing_status.assert_not_called()
        runtime.worker_health.assert_not_called()
        runtime.close()

    def test_control_transport_and_body_bindings_fail_closed(self):
        runtime = open_runtime(":memory:")
        value = {
            "schema": RUNTIME._RECEIVER.STATUS_REQUEST_SCHEMA,
            "event_id": "cms-event-184",
            "site_id": "public-site",
        }
        body = RUNTIME._RECEIVER._canonical(value)
        transport = WSGITransport(runtime)
        cases = (
            transport.request(
                "POST",
                "https://cms.example.test" + RUNTIME._RECEIVER.STATUS_PATH,
                {
                    "Authorization": "Bearer exact",
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                    "X-Localization-Terminal-Status-SHA256": "0" * 64,
                },
                body,
            ),
            transport.request(
                "GET",
                "http://cms.example.test" + RUNTIME._RECEIVER.READINESS_PATH,
                {"Authorization": "Bearer exact"},
            ),
            transport.request(
                "POST",
                "https://cms.example.test"
                + RUNTIME._RECEIVER.READINESS_PATH,
                {"Authorization": "Bearer exact"},
            ),
            transport.request(
                "GET",
                "https://cms.example.test"
                + RUNTIME._RECEIVER.READINESS_PATH,
                {
                    "Authorization": "Bearer exact",
                    "Content-Length": "1",
                },
                b"x",
            ),
            transport.request(
                "GET",
                "https://cms.example.test"
                + RUNTIME._RECEIVER.CAPABILITIES_PATH,
                {
                    "Authorization": "Bearer exact",
                    "Content-Type": "application/json; charset=utf-8",
                },
            ),
            transport.request(
                "GET",
                "https://cms.example.test" + RUNTIME._RECEIVER.HEALTH_PATH,
                {
                    "Authorization": "Bearer exact",
                    "Content-Length": "1",
                },
                b"x",
            ),
        )
        self.assertEqual(tuple(result.status for result in cases), (
            400, 400, 405, 400, 415, 400,
        ))
        runtime.close()

    def test_private_control_authentication_failure_is_redacted(self):
        secret = "private credential and customer detail"

        def broken_authenticate(_request, _headers):
            raise RuntimeError(secret)

        runtime = RUNTIME.open_durable_terminal_notification_receiver(
            ":memory:",
            broken_authenticate,
            origin="https://cms.example.test",
        )
        response = control_request(
            runtime, RUNTIME._RECEIVER.READINESS_PATH,
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error_code"], (
            "notification_receiver.authentication_unavailable"
        ))
        self.assertNotIn(secret, response.body.decode("utf-8"))
        runtime.close()

    def test_hosted_runtime_processes_notification_and_reports_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.sqlite3"
            processed = []

            def callback(payload):
                processed.append(payload["notification_id"])
                return processing_ack(payload)

            runtime = (
                RUNTIME.open_hosted_durable_terminal_notification_receiver(
                    path,
                    authenticate,
                    callback,
                    origin="https://cms.example.test",
                    worker_id="cms-consumer",
                    active_delay_seconds=0.01,
                    idle_delay_seconds=0.01,
                    blocked_delay_seconds=0.01,
                    clock=lambda: 123.5,
                )
            )
            try:
                self.wait_for(lambda: runtime.worker_state == "running")
                payload = notification()
                acknowledgement = adapter(runtime)(payload)
                self.assertEqual(acknowledgement["status"], "accepted")
                self.wait_for(lambda: processed == [payload["notification_id"]])
                self.assertEqual(runtime.worker_readiness(), {
                    "schema": "blun.cms-terminal-receiver-readiness.v1",
                    "status": "ready",
                    "worker_state": "running",
                    "inbox_status": "ok",
                    "error_code": None,
                })
                self.assertEqual(
                    runtime.health().processing_counts["succeeded"], 1,
                )
            finally:
                runtime.close(worker_timeout_seconds=1)
            self.assertEqual(runtime.worker_state, "closed")

    def test_stopped_host_rejects_new_intake_without_storing_it(self):
        runtime = open_runtime(":memory:")
        runtime.start_worker(
            processing_ack,
            "cms-consumer",
            active_delay_seconds=0.01,
            idle_delay_seconds=60,
            blocked_delay_seconds=60,
        )
        self.wait_for(lambda: runtime.worker_state == "running")
        runtime.stop_worker(timeout_seconds=1)
        payload = notification(event_id="cms-event-stopped")
        with self.assertRaises(HTTP.HTTPTerminalNotificationFailed) as caught:
            adapter(runtime)(payload)
        self.assertEqual((caught.exception.code, caught.exception.retryable), (
            "notification_http.http_status", True,
        ))
        self.assertEqual(runtime.health().received, 0)
        self.assertEqual(runtime.worker_readiness()["status"], "not_ready")
        runtime.close()

    def test_worker_failure_is_visible_content_free_and_blocks_intake(self):
        runtime = open_runtime(":memory:")
        secret = "private callback and website text"
        runtime.process_next = mock.Mock(side_effect=RuntimeError(secret))
        runtime.start_worker(
            processing_ack,
            "cms-consumer",
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
            blocked_delay_seconds=0.01,
        )
        try:
            self.wait_for(lambda: runtime.worker_state == "failed")
            readiness = runtime.worker_readiness()
            self.assertEqual(readiness, {
                "schema": "blun.cms-terminal-receiver-readiness.v1",
                "status": "not_ready",
                "worker_state": "failed",
                "inbox_status": None,
                "error_code": "notification_receiver.worker_blocked",
            })
            self.assertNotIn(secret, repr(readiness))
            with self.assertRaises(HTTP.HTTPTerminalNotificationFailed):
                adapter(runtime)(notification(event_id="cms-event-failed"))
            with self.assertRaises(
                RUNTIME.DurableTerminalReceiverRuntimeBlocked
            ):
                runtime.start_worker(processing_ack, "cms-consumer")
        finally:
            runtime.close()

    def test_terminal_handler_failure_keeps_worker_alive_but_blocks_intake(self):
        runtime = open_runtime(":memory:")

        def callback(_payload):
            raise RuntimeError("private permanent handler failure")

        runtime.start_worker(
            callback,
            "cms-consumer",
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
            blocked_delay_seconds=0.01,
        )
        self.wait_for(lambda: runtime.worker_state == "running")
        adapter(runtime)(notification())
        try:
            self.wait_for(lambda: runtime.health().failed == 1)
            self.assertEqual(runtime.worker_state, "running")
            self.assertEqual(runtime.worker_readiness(), {
                "schema": "blun.cms-terminal-receiver-readiness.v1",
                "status": "not_ready",
                "worker_state": "running",
                "inbox_status": "blocked",
                "error_code": "notification_receiver.storage_blocked",
            })
            with self.assertRaises(HTTP.HTTPTerminalNotificationFailed):
                adapter(runtime)(notification(event_id="cms-event-later"))
            self.assertEqual(runtime.health().received, 1)
        finally:
            runtime.close()

    def test_idle_worker_shutdown_is_interruptible(self):
        runtime = open_runtime(":memory:")
        runtime.start_worker(
            processing_ack,
            "cms-consumer",
            active_delay_seconds=60,
            idle_delay_seconds=60,
            blocked_delay_seconds=60,
        )
        self.wait_for(lambda: runtime.worker_state == "running")
        started = time.monotonic()
        runtime.close(worker_timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_close_timeout_preserves_database_until_callback_returns(self):
        runtime = open_runtime(":memory:")
        payload = notification()
        adapter(runtime)(payload)
        entered = threading.Event()
        release = threading.Event()

        def callback(value):
            entered.set()
            release.wait(2)
            return processing_ack(value)

        runtime.start_worker(
            callback,
            "cms-consumer",
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
            blocked_delay_seconds=0.01,
        )
        self.assertTrue(entered.wait(1))
        with self.assertRaises(
            RUNTIME.DurableTerminalReceiverRuntimeBlocked
        ):
            runtime.close(worker_timeout_seconds=0.01)
        self.assertEqual(runtime.state, "open")
        release.set()
        runtime.close(worker_timeout_seconds=1)
        self.assertEqual(runtime.state, "closed")

    def test_hosted_invalid_worker_configuration_creates_no_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.sqlite3"
            for changes in (
                {"callback": None},
                {"worker_id": "not a worker"},
                {"lease_seconds": 0},
                {"idle_delay_seconds": 0},
            ):
                options = {
                    "callback": processing_ack,
                    "worker_id": "cms-consumer",
                    "lease_seconds": 60,
                    "active_delay_seconds": 0.01,
                    "idle_delay_seconds": 1,
                    "blocked_delay_seconds": 1,
                }
                options.update(changes)
                with self.subTest(changes=changes), self.assertRaises(
                    RUNTIME.DurableTerminalReceiverRuntimeBlocked
                ):
                    RUNTIME.open_hosted_durable_terminal_notification_receiver(
                        path,
                        authenticate,
                        origin="https://cms.example.test",
                        **options,
                    )
                self.assertFalse(path.exists())

    def test_foreign_process_cannot_control_managed_worker(self):
        runtime = open_runtime(":memory:")
        with mock.patch.object(RUNTIME.os, "getpid", return_value=os.getpid() + 1):
            self.assertEqual(runtime.worker_state, "foreign-process")
            for operation in (
                lambda: runtime.start_worker(processing_ack, "cms-consumer"),
                runtime.stop_worker,
                runtime.worker_readiness,
            ):
                with self.assertRaises(
                    RUNTIME.DurableTerminalReceiverRuntimeBlocked
                ):
                    operation()
        runtime.close()

    def test_health_detects_semantically_tampered_storage(self):
        runtime = open_runtime(":memory:")
        payload = notification()
        body = HTTP._canonical(payload)
        adapter(runtime)(payload)
        runtime._connection.execute(
            "UPDATE cms_terminal_notification_inbox SET payload_sha256 = ?",
            (hashlib.sha256(body + b"changed").hexdigest(),),
        )
        with self.assertRaises(
            RUNTIME.DurableTerminalReceiverRuntimeBlocked
        ):
            runtime.health()
        runtime.close()


if __name__ == "__main__":
    unittest.main()
