from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests import test_website_localization_cms_client as cms_support


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = load(
    "blun_test_website_localization_cms_source_http_runtime",
    ROOT / "integrations" / "website_localization_cms_source_runtime.py",
)
HTTP = RUNTIME._HTTP
SERVICE = RUNTIME._SERVICE


class ScriptedClient:
    timeout = 30.0

    def __init__(self):
        self.calls = []
        self.events = {}
        self.lifecycle_status = "processing"

    def submit_change(self, change):
        self.calls.append(("change", copy.deepcopy(change)))
        self.events[change["event_id"]] = copy.deepcopy(change)
        count = len(change["localization"]["target_locales"])
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.API_SCHEMA,
            "event_id": change["event_id"],
            "plan_id": "plan-" + change["event_id"],
            "job_count": count,
            "inserted_jobs": count,
            "status": "enqueued",
        }

    def cancel(self, request):
        self.calls.append(("cancellation", copy.deepcopy(request)))
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.API_SCHEMA,
            "cancellation_id": request["cancellation_id"],
            "event_id": request["event_id"],
            "status": "cancelled",
            "newly_cancelled": True,
        }

    def request_tombstone(self, request):
        self.calls.append(("tombstone", copy.deepcopy(request)))
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.API_SCHEMA,
            "tombstone_id": request["tombstone_id"],
            "event_id": request["event_id"],
            "delivery_id": "delivery-" + request["event_id"],
            "status": "pending",
            "newly_requested": True,
        }

    def lifecycle(self, event_id, site_id):
        self.calls.append(("lifecycle", event_id, site_id))
        change = self.events[event_id]
        required = sorted(change["localization"]["target_locales"])
        counts = {
            "failed": 0,
            "leased": 0,
            "pending": len(required),
            "retry_wait": 0,
            "succeeded": 0,
        }
        if self.lifecycle_status == "cancelled":
            counts["pending"] = 0
            counts["cancelled"] = len(required)
        return {
            "schema": SERVICE._DISPATCH._CLIENT._API.LIFECYCLE_RESPONSE_SCHEMA,
            "request_id": "lifecycle-" + event_id,
            "event_id": event_id,
            "site_id": site_id,
            "plan_id": "plan-" + event_id,
            "website_version": change["website_version"],
            "source_sequence": change["source_sequence"],
            "status": self.lifecycle_status,
            "required_locales": required,
            "approved_locales": [],
            "blocked_locales": [],
            "queue_counts": counts,
            "delivery": None,
            "tombstone": None,
        }


class Authenticator:
    def __init__(self):
        self.requests = []
        self.override_scope = None
        self.error = None
        self.site_id = cms_support.event()["site_id"]

    def __call__(self, request):
        self.requests.append(copy.deepcopy(request))
        if self.error is not None:
            raise self.error
        scope = self.override_scope or HTTP.SCOPES[request["path"]]
        principal = {
            "schema": (
                HTTP.STATUS_PRINCIPAL_SCHEMA
                if request["path"] == HTTP.STATUS_PATH
                else HTTP.PRINCIPAL_SCHEMA
            ),
            "principal_id": "source-cms",
            "credential_id": "source-cms-credential",
            "credential_version": "1",
            "scope": scope,
        }
        if request["path"] == HTTP.STATUS_PATH:
            principal["site_id"] = self.site_id
        return principal


class SourceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.client = ScriptedClient()
        self.authenticator = Authenticator()
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = (
            root / "changes.sqlite3",
            root / "removals.sqlite3",
            root / "lifecycle.sqlite3",
        )
        self.runtime = RUNTIME.open_durable_cms_source(
            *self.paths,
            self.client,
            change_worker_id="source-change-worker",
            removal_worker_id="source-removal-worker",
            lifecycle_worker_id="source-lifecycle-worker",
            http_authenticator=self.authenticator,
            clock=lambda: self.now,
            change_lease_seconds=60,
            removal_lease_seconds=60,
            lifecycle_lease_seconds=60,
            lifecycle_poll_interval_seconds=30,
        )
        self.app = self.runtime.http

    def tearDown(self):
        if os.getpid() == self.runtime._owner_pid:
            self.runtime.close()
        self.directory.cleanup()

    @staticmethod
    def encode(value):
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def call(self, path, *, method="POST", value=None, overrides=None):
        body = b"" if value is None else self.encode(value)
        environ = {
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REQUEST_METHOD": method,
            "wsgi.url_scheme": "https",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_AUTHORIZATION": "Bearer test-token",
        }
        if value is not None:
            environ["CONTENT_TYPE"] = "application/json; charset=utf-8"
        if overrides:
            environ.update(overrides)
        captured = {}
        raw = b"".join(self.app(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=tuple(headers),
            ),
        ))
        return captured["status"], dict(captured["headers"]), json.loads(raw)

    def change_request(self, change=None):
        return {
            "schema": HTTP.CHANGE_REQUEST_SCHEMA,
            "change": cms_support.event() if change is None else change,
            "max_attempts": 5,
        }

    def removal_request(self, removal=None):
        return {
            "schema": HTTP.REMOVAL_REQUEST_SCHEMA,
            "removal": cms_support.cancellation() if removal is None else removal,
            "max_attempts": 5,
        }

    def status_request(self, change=None):
        change = cms_support.event() if change is None else change
        return {
            "schema": HTTP.STATUS_REQUEST_SCHEMA,
            "event_id": change["event_id"],
            "site_id": change["site_id"],
        }

    def test_change_ingress_persists_then_worker_dispatches(self):
        request = self.change_request()
        source = request["change"]["localization"]["source_text"]

        status, headers, response = self.call(HTTP.CHANGE_PATH, value=request)

        self.assertEqual(status, "202 Accepted")
        self.assertEqual(response["schema"], HTTP.CHANGE_RESPONSE_SCHEMA)
        self.assertEqual(response["event_id"], request["change"]["event_id"])
        self.assertEqual(response["status"], "pending")
        self.assertNotIn(source, json.dumps(response))
        self.assertEqual(headers["Cache-Control"], "no-store")
        auth = self.authenticator.requests[-1]
        self.assertEqual(auth["path"], HTTP.CHANGE_PATH)
        self.assertEqual(auth["body_sha256"], hashlib.sha256(
            self.encode(request)
        ).hexdigest())
        self.assertNotIn(source, json.dumps(auth))

        outcome = self.runtime.run_once()
        self.assertEqual((outcome.phase, outcome.status), ("change", "succeeded"))
        self.assertEqual([call[0] for call in self.client.calls], ["change"])

    def test_identical_change_replay_is_idempotent_and_collision_blocks(self):
        request = self.change_request()
        first = self.call(HTTP.CHANGE_PATH, value=request)
        second = self.call(HTTP.CHANGE_PATH, value=request)
        changed = copy.deepcopy(request)
        changed["change"]["localization"]["source_text"] += " Changed."
        collision = self.call(HTTP.CHANGE_PATH, value=changed)

        self.assertEqual(first[2], second[2])
        self.assertEqual(collision[0], "409 Conflict")
        self.assertEqual(
            collision[2]["error_code"],
            "source_http.idempotency_collision",
        )
        count = self.runtime._service.changes.connection.execute(
            "SELECT COUNT(*) FROM cms_source_change_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.client.calls, [])

    def test_removal_ingress_persists_then_dispatches_before_changes(self):
        self.call(HTTP.CHANGE_PATH, value=self.change_request())
        status, _headers, response = self.call(
            HTTP.REMOVAL_PATH, value=self.removal_request(),
        )

        self.assertEqual(status, "202 Accepted")
        self.assertEqual(response["schema"], HTTP.REMOVAL_RESPONSE_SCHEMA)
        self.assertEqual((response["operation"], response["request_id"]), (
            "cancellation", "cancel-1",
        ))
        outcome = self.runtime.run_once()
        self.assertEqual((outcome.phase, outcome.operation), (
            "removal", "cancellation",
        ))
        self.assertEqual([call[0] for call in self.client.calls], ["cancellation"])

    def test_health_is_authenticated_content_free_and_blocks_on_tampering(self):
        self.call(HTTP.CHANGE_PATH, value=self.change_request())
        status, headers, response = self.call(
            HTTP.HEALTH_PATH, method="GET",
        )
        source = cms_support.event()["localization"]["source_text"]

        self.assertEqual(status, "200 OK")
        self.assertEqual(response["schema"], HTTP.HEALTH_RESPONSE_SCHEMA)
        self.assertEqual(response["health"]["status"], "ok")
        self.assertEqual(response["health"]["changes"]["counts"]["pending"], 1)
        self.assertNotIn(source, json.dumps(response))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.authenticator.requests[-1]["body_sha256"], (
            hashlib.sha256(b"").hexdigest()
        ))

        self.paths[0].chmod(0o640)
        blocked = self.call(HTTP.HEALTH_PATH, method="GET")
        self.assertEqual(blocked[0], "503 Service Unavailable")
        self.assertEqual(blocked[2]["error_code"], "source_http.runtime_blocked")

    def test_capabilities_are_authenticated_hashed_and_match_active_routes(self):
        source = cms_support.event()["localization"]["source_text"]
        methods = (
            "enqueue_change", "enqueue_removal", "status", "health",
            "worker_readiness", "require_worker_ready",
        )
        patches = [
            mock.patch.object(
                self.runtime, name, wraps=getattr(self.runtime, name),
            )
            for name in methods
        ]
        started = [patch.start() for patch in patches]
        try:
            status, headers, response = self.call(
                HTTP.CAPABILITIES_PATH, method="GET",
            )
        finally:
            for patch in patches:
                patch.stop()

        self.assertEqual(status, "200 OK")
        self.assertEqual(
            response["schema"], HTTP.CAPABILITIES_RESPONSE_SCHEMA,
        )
        capabilities = response["capabilities"]
        claimed = capabilities.pop("sha256")
        self.assertEqual(
            claimed,
            hashlib.sha256(self.encode(capabilities)).hexdigest(),
        )
        self.assertEqual(capabilities["schema"], HTTP.CAPABILITIES_SCHEMA)
        self.assertEqual(capabilities["api_schema"], HTTP.API_SCHEMA)
        self.assertEqual(set(capabilities["operations"]), {
            "capabilities", "change", "health", "readiness", "removal",
            "status",
        })
        for operation in capabilities["operations"].values():
            path = operation["path"]
            self.assertEqual(operation["method"], HTTP.METHODS[path])
            self.assertEqual(operation["scope"], HTTP.SCOPES[path])
        self.assertEqual(
            capabilities["operations"]["status"]["principal_schema"],
            HTTP.STATUS_PRINCIPAL_SCHEMA,
        )
        self.assertEqual(
            capabilities["operations"]["readiness"]["scope"],
            "source-readiness:read",
        )
        self.assertEqual(
            capabilities["operations"]["change"]["request_fields"],
            ["schema", "change", "max_attempts"],
        )
        self.assertEqual(
            capabilities["limits"]["max_body_bytes"], HTTP.MAX_BODY_BYTES,
        )
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(source, json.dumps(response))
        self.assertTrue(all(item.call_count == 0 for item in started))
        auth = self.authenticator.requests[-1]
        self.assertEqual(auth["path"], HTTP.CAPABILITIES_PATH)
        self.assertEqual(auth["method"], "GET")
        self.assertEqual(
            auth["body_sha256"], hashlib.sha256(b"").hexdigest(),
        )

    def test_capability_contract_drift_and_auth_failure_block(self):
        changed_methods = dict(HTTP.METHODS)
        changed_methods[HTTP.CHANGE_PATH] = "PUT"
        with mock.patch.object(HTTP, "METHODS", changed_methods):
            drift = self.call(HTTP.CAPABILITIES_PATH, method="GET")
        self.assertEqual(drift[0], "503 Service Unavailable")
        self.assertEqual(
            drift[2]["error_code"], "source_http.capabilities_invalid",
        )

        self.authenticator.override_scope = "source-health:read"
        rejected = self.call(HTTP.CAPABILITIES_PATH, method="GET")
        self.assertEqual(rejected[0], "403 Forbidden")
        self.assertEqual(
            rejected[2]["error_code"], "source_http.scope_rejected",
        )

    def test_readiness_tracks_worker_and_stopped_host_rejects_writes(self):
        unmanaged = self.call(HTTP.READINESS_PATH, method="GET")
        self.assertEqual(unmanaged[0], "503 Service Unavailable")
        self.assertEqual(unmanaged[2]["schema"], HTTP.READINESS_RESPONSE_SCHEMA)
        self.assertEqual(unmanaged[2]["readiness"]["worker_state"], "unmanaged")

        self.runtime.start_worker(
            active_delay_seconds=0.01,
            idle_delay_seconds=60,
            blocked_delay_seconds=60,
        )
        deadline = time.monotonic() + 2
        while self.runtime.worker_state != "running" and time.monotonic() < deadline:
            time.sleep(0.005)
        ready = self.call(HTTP.READINESS_PATH, method="GET")
        self.assertEqual(ready[0], "200 OK")
        self.assertEqual(ready[2]["readiness"]["status"], "ready")
        self.assertEqual(ready[2]["readiness"]["service_status"], "ok")
        self.assertEqual(
            self.authenticator.requests[-1]["path"], HTTP.READINESS_PATH,
        )

        self.runtime.stop_worker(timeout_seconds=1)
        stopped = self.call(HTTP.READINESS_PATH, method="GET")
        rejected = self.call(HTTP.CHANGE_PATH, value=self.change_request())
        self.assertEqual(stopped[0], "503 Service Unavailable")
        self.assertEqual(stopped[2]["readiness"]["worker_state"], "stopped")
        self.assertEqual(rejected[0], "503 Service Unavailable")
        self.assertEqual(rejected[2]["error_code"], "source_http.runtime_blocked")
        count = self.runtime._service.changes.connection.execute(
            "SELECT COUNT(*) FROM cms_source_change_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_status_is_site_bound_read_only_and_tracks_remote_lifecycle(self):
        change = cms_support.event()
        source = change["localization"]["source_text"]
        self.call(HTTP.CHANGE_PATH, value=self.change_request(change))

        queued = self.call(HTTP.STATUS_PATH, value=self.status_request(change))
        self.assertEqual(queued[0], "200 OK")
        self.assertEqual(queued[2]["schema"], HTTP.STATUS_RESPONSE_SCHEMA)
        self.assertEqual(queued[2]["status"]["dispatch_status"], "pending")
        self.assertIsNone(queued[2]["status"]["lifecycle_state"])
        self.assertEqual(self.client.calls, [])
        auth = self.authenticator.requests[-1]
        self.assertEqual(auth["path"], HTTP.STATUS_PATH)
        self.assertEqual(auth["body_sha256"], hashlib.sha256(
            self.encode(self.status_request(change))
        ).hexdigest())

        self.runtime.run_once()
        self.runtime.run_once()
        observed = self.call(
            HTTP.STATUS_PATH, value=self.status_request(change),
        )
        result = observed[2]["status"]
        self.assertEqual((result["lifecycle_state"], result["remote_status"]), (
            "watching", "processing",
        ))
        self.assertEqual(
            result["queue_counts"]["pending"],
            len(change["localization"]["target_locales"]),
        )
        self.assertNotIn(source, json.dumps(observed[2]))
        self.assertEqual([call[0] for call in self.client.calls], (
            ["change", "lifecycle"]
        ))

        self.paths[2].chmod(0o640)
        blocked = self.call(
            HTTP.STATUS_PATH, value=self.status_request(change),
        )
        self.assertEqual(blocked[0], "503 Service Unavailable")
        self.assertEqual(
            blocked[2]["error_code"], "source_http.runtime_blocked",
        )

    def test_status_hides_missing_and_cross_site_events(self):
        change = cms_support.event()
        self.call(HTTP.CHANGE_PATH, value=self.change_request(change))
        wrong_site = self.status_request(change)
        wrong_site["site_id"] = "another-site"
        missing = self.status_request(change)
        missing["event_id"] = "missing-event"

        with mock.patch.object(
            self.runtime, "status", wraps=self.runtime.status,
        ) as reader:
            response = self.call(HTTP.STATUS_PATH, value=wrong_site)
            self.assertEqual(response[0], "404 Not Found")
            self.assertEqual(
                response[2]["error_code"], "source_http.status_not_found",
            )
            reader.assert_not_called()
            response = self.call(HTTP.STATUS_PATH, value=missing)
            self.assertEqual(response[0], "404 Not Found")
            self.assertEqual(
                response[2]["error_code"], "source_http.status_not_found",
            )
            reader.assert_called_once()
        invalid = self.status_request(change)
        invalid["event_id"] = "not valid"
        response = self.call(HTTP.STATUS_PATH, value=invalid)
        self.assertEqual(response[0], "400 Bad Request")
        self.assertEqual(
            response[2]["error_code"], "source_http.request_invalid",
        )

        self.authenticator.site_id = "not valid"
        response = self.call(
            HTTP.STATUS_PATH, value=self.status_request(change),
        )
        self.assertEqual(response[0], "401 Unauthorized")
        self.assertEqual(
            response[2]["error_code"], "source_http.authentication_failed",
        )

    def test_status_accepts_terminal_cancelled_queue_shape(self):
        change = cms_support.event()
        self.client.lifecycle_status = "cancelled"
        self.runtime.enqueue_change(change)
        self.runtime.run_once()
        self.runtime.run_once()

        response = self.call(
            HTTP.STATUS_PATH, value=self.status_request(change),
        )
        self.assertEqual(response[0], "200 OK")
        status = response[2]["status"]
        self.assertEqual(status["remote_status"], "cancelled")
        self.assertEqual(
            status["queue_counts"]["cancelled"],
            len(change["localization"]["target_locales"]),
        )

    def test_status_rejects_unbound_runtime_response_and_private_failure(self):
        change = cms_support.event()
        source = change["localization"]["source_text"]
        self.runtime.enqueue_change(change)
        request = self.status_request(change)
        valid = self.runtime.status(change["event_id"], change["site_id"])

        with mock.patch.object(
            self.runtime, "status", return_value=replace(
                valid, site_id="another-site",
            ),
        ):
            unbound = self.call(HTTP.STATUS_PATH, value=request)
        self.assertEqual(unbound[0], "503 Service Unavailable")
        self.assertEqual(
            unbound[2]["error_code"],
            "source_http.runtime_response_invalid",
        )

        with mock.patch.object(
            self.runtime, "status", side_effect=RuntimeError(source),
        ):
            failed = self.call(HTTP.STATUS_PATH, value=request)
        self.assertEqual(failed[0], "503 Service Unavailable")
        self.assertEqual(failed[2]["error_code"], "source_http.runtime_blocked")
        self.assertNotIn(source, json.dumps((unbound[2], failed[2])))

    def test_scope_mismatch_and_authenticator_failure_block_before_store(self):
        request = self.change_request()
        self.authenticator.override_scope = "source-health:read"
        rejected = self.call(HTTP.CHANGE_PATH, value=request)
        self.assertEqual(rejected[0], "403 Forbidden")
        self.assertEqual(rejected[2]["error_code"], "source_http.scope_rejected")

        self.authenticator.override_scope = None
        self.authenticator.error = RuntimeError(
            request["change"]["localization"]["source_text"]
        )
        unavailable = self.call(HTTP.CHANGE_PATH, value=request)
        self.assertEqual(unavailable[0], "503 Service Unavailable")
        self.assertEqual(
            unavailable[2]["error_code"],
            "source_http.authentication_unavailable",
        )
        self.assertEqual(self.client.calls, [])
        count = self.runtime._service.changes.connection.execute(
            "SELECT COUNT(*) FROM cms_source_change_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_request_framing_and_schema_fail_closed(self):
        request = self.change_request()
        cases = (
            (
                {"wsgi.url_scheme": "http"},
                "400 Bad Request", "source_http.https_required",
            ),
            (
                {"QUERY_STRING": "x=1"},
                "400 Bad Request", "source_http.query_rejected",
            ),
            (
                {"HTTP_TRANSFER_ENCODING": "chunked"},
                "400 Bad Request", "source_http.transfer_encoding_rejected",
            ),
            (
                {"CONTENT_TYPE": "text/plain"},
                "415 Unsupported Media Type", "source_http.content_type_invalid",
            ),
            (
                {"CONTENT_LENGTH": ""},
                "411 Length Required", "source_http.content_length_required",
            ),
            (
                {"CONTENT_LENGTH": str(HTTP.MAX_BODY_BYTES + 1)},
                "413 Content Too Large", "source_http.body_too_large",
            ),
        )
        for overrides, expected_status, code in cases:
            with self.subTest(code=code):
                status, _headers, response = self.call(
                    HTTP.CHANGE_PATH, value=request, overrides=overrides,
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(response["error_code"], code)

        invalid = self.call(HTTP.CHANGE_PATH, value={
            "schema": HTTP.CHANGE_REQUEST_SCHEMA,
            "change": request["change"],
        })
        self.assertEqual(invalid[0], "400 Bad Request")
        self.assertEqual(invalid[2]["error_code"], "source_http.request_invalid")

        invalid_change = copy.deepcopy(request)
        del invalid_change["change"]["localization"]["source_locale"]
        rejected = self.call(HTTP.CHANGE_PATH, value=invalid_change)
        self.assertEqual(rejected[0], "400 Bad Request")
        self.assertEqual(
            rejected[2]["error_code"], "source_http.request_invalid",
        )

    def test_route_method_health_body_and_duplicate_json_are_rejected(self):
        missing = self.call("/wrong", value=self.change_request())
        method = self.call(HTTP.CHANGE_PATH, method="GET")
        body = self.call(
            HTTP.HEALTH_PATH,
            method="GET",
            value={"unexpected": True},
        )
        capabilities_body = self.call(
            HTTP.CAPABILITIES_PATH,
            method="GET",
            value={"unexpected": True},
        )
        readiness_body = self.call(
            HTTP.READINESS_PATH,
            method="GET",
            value={"unexpected": True},
        )
        raw = b'{"schema":"x","schema":"y"}'
        environ = {
            "PATH_INFO": HTTP.CHANGE_PATH,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        captured = {}
        duplicate = json.loads(b"".join(self.app(
            environ,
            lambda status, headers: captured.update(status=status),
        )))

        self.assertEqual(missing[0], "404 Not Found")
        self.assertEqual(method[0], "405 Method Not Allowed")
        self.assertEqual(body[0], "400 Bad Request")
        self.assertEqual(body[2]["error_code"], "source_http.body_not_allowed")
        self.assertEqual(capabilities_body[0], "400 Bad Request")
        self.assertEqual(
            capabilities_body[2]["error_code"],
            "source_http.body_not_allowed",
        )
        self.assertEqual(readiness_body[0], "400 Bad Request")
        self.assertEqual(
            readiness_body[2]["error_code"], "source_http.body_not_allowed",
        )
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertEqual(duplicate["error_code"], "source_http.json_invalid")

    def test_runtime_response_and_private_exception_never_leak(self):
        secret = cms_support.event()["localization"]["source_text"]
        with mock.patch.object(
            self.runtime,
            "enqueue_change",
            side_effect=RuntimeError(secret),
        ):
            failed = self.call(HTTP.CHANGE_PATH, value=self.change_request())
        self.assertEqual(failed[0], "503 Service Unavailable")
        self.assertEqual(failed[2]["error_code"], "source_http.runtime_blocked")
        self.assertNotIn(secret, json.dumps(failed[2]))

    def test_concurrent_replays_converge_on_one_durable_item(self):
        request = self.change_request()
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(
                lambda _index: self.call(HTTP.CHANGE_PATH, value=request),
                range(24),
            ))
        self.assertTrue(all(item[0] == "202 Accepted" for item in responses))
        count = self.runtime._service.changes.connection.execute(
            "SELECT COUNT(*) FROM cms_source_change_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_optional_http_composition_and_invalid_auth_preflight(self):
        self.runtime.close()
        root = Path(self.directory.name)
        paths = (
            root / "other-changes.sqlite3",
            root / "other-removals.sqlite3",
            root / "other-lifecycle.sqlite3",
        )
        no_http = RUNTIME.open_durable_cms_source(
            *paths,
            self.client,
            change_worker_id="change-worker",
            removal_worker_id="removal-worker",
            lifecycle_worker_id="lifecycle-worker",
            clock=lambda: self.now,
        )
        try:
            self.assertIsNone(no_http.http)
        finally:
            no_http.close()

        invalid_paths = (
            root / "invalid-changes.sqlite3",
            root / "invalid-removals.sqlite3",
            root / "invalid-lifecycle.sqlite3",
        )
        with self.assertRaises(RUNTIME.DurableCMSSourceRuntimeBlocked) as invalid:
            RUNTIME.open_durable_cms_source(
                *invalid_paths,
                self.client,
                change_worker_id="change-worker",
                removal_worker_id="removal-worker",
                lifecycle_worker_id="lifecycle-worker",
                http_authenticator=object(),
            )
        self.assertEqual(
            invalid.exception.code,
            "source_runtime.http_authenticator_invalid",
        )
        self.assertFalse(any(path.exists() for path in invalid_paths))


if __name__ == "__main__":
    unittest.main()
