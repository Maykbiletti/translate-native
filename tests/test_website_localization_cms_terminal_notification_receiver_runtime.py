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


def principal(site_id="public-site"):
    return {
        "schema": RUNTIME._RECEIVER.PRINCIPAL_SCHEMA,
        "principal_id": "website-cms",
        "credential_id": "cms-key",
        "credential_version": "v1",
        "scope": RUNTIME._RECEIVER.WRITE_SCOPE,
        "site_id": site_id,
    }


def authenticate(request, headers):
    if headers.get("authorization") != "Bearer exact":
        raise RuntimeError("private credential failure")
    return principal(request["site_id"])


class WSGITransport:
    def __init__(self, application):
        self.application = application

    def post(self, url, headers, body, *, timeout):
        del timeout
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

        result = self.application(environ, start_response)
        return HTTP.HTTPResult(
            captured["status"], captured["headers"], b"".join(result),
        )


def adapter(application):
    return HTTP.HTTPTerminalNotifierAdapter(
        "https://cms.example.test/v1/localization/terminal-notifications",
        lambda _request: {"Authorization": "Bearer exact"},
        transport=WSGITransport(application),
    )


def open_runtime(path):
    return RUNTIME.open_durable_terminal_notification_receiver(
        path,
        authenticate,
        origin="https://cms.example.test",
        clock=lambda: 123.5,
    )


class DurableTerminalReceiverRuntimeTests(unittest.TestCase):
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
                RUNTIME.DurableTerminalReceiverRuntimeHealth("ok", "open", 1)
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
                {"authenticate": None},
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
