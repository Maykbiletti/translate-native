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
from tests import test_website_localization_cms_source_delivery as delivery_support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = load(
    "blun_test_website_localization_cms_source_delivery_http_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_runtime.py",
)
HTTP = RUNTIME._HTTP


class Authenticator:
    def __init__(self):
        self.requests = []
        self.error = None
        self.scope = None
        self.site_id = "site-1"

    def __call__(self, request):
        self.requests.append(copy.deepcopy(request))
        if self.error is not None:
            raise self.error
        tenant = request["path"] in HTTP.TENANT_PATHS
        result = {
            "schema": (
                HTTP.TENANT_PRINCIPAL_SCHEMA
                if tenant else HTTP.PRINCIPAL_SCHEMA
            ),
            "principal_id": "website-backend",
            "credential_id": "website-credential",
            "credential_version": "1",
            "scope": self.scope or HTTP.SCOPES[request["path"]],
        }
        if tenant:
            result["site_id"] = self.site_id
        return result


class SourceDeliveryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "delivery.sqlite3"
        self.client = delivery_support.ScriptedClient()
        self.authenticator = Authenticator()
        self.runtime = RUNTIME.open_hosted_cms_source_delivery(
            self.database,
            self.client,
            worker_id="website-source-worker",
            clock=lambda: self.now,
            lease_seconds=60,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
            http_authenticator=self.authenticator,
        )
        self.app = self.runtime.http

    def tearDown(self):
        self.runtime._owner_pid = os.getpid()
        if self.database.exists() and not self.database.is_symlink():
            os.chmod(self.database, 0o600)
        try:
            self.runtime.close(worker_timeout_seconds=1)
        except Exception:
            pass
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

    def change_request(self, change=None):
        return {
            "schema": HTTP.CHANGE_REQUEST_SCHEMA,
            "change": cms_support.event() if change is None else change,
            "source_max_attempts": 4,
            "delivery_max_attempts": 3,
        }

    def removal_request(self, removal=None):
        return {
            "schema": HTTP.REMOVAL_REQUEST_SCHEMA,
            "removal": (
                cms_support.cancellation() if removal is None else removal
            ),
            "source_max_attempts": 4,
            "delivery_max_attempts": 3,
        }

    def status_request(self, operation="change", request_id="event-1"):
        return {
            "schema": HTTP.STATUS_REQUEST_SCHEMA,
            "operation": operation,
            "request_id": request_id,
            "site_id": self.authenticator.site_id,
        }

    def call(
        self,
        path,
        *,
        method=None,
        value=None,
        overrides=None,
        bind_payload=None,
        request_id=None,
    ):
        body = b"" if value is None else self.encode(value)
        environ = {
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REQUEST_METHOD": method or HTTP.METHODS.get(path, "POST"),
            "wsgi.url_scheme": "https",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_AUTHORIZATION": "Bearer test-token",
        }
        if value is not None:
            environ["CONTENT_TYPE"] = "application/json; charset=utf-8"
        if bind_payload is not None:
            environ["HTTP_IDEMPOTENCY_KEY"] = request_id
            environ["HTTP_X_LOCALIZATION_SOURCE_PAYLOAD_SHA256"] = (
                hashlib.sha256(self.encode(bind_payload)).hexdigest()
            )
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

    def enqueue_change(self, change=None, **kwargs):
        change = cms_support.event() if change is None else change
        request = self.change_request(change)
        return self.call(
            HTTP.CHANGE_PATH,
            value=request,
            bind_payload=change,
            request_id=change["event_id"],
            **kwargs,
        )

    def enqueue_removal(self, removal=None, **kwargs):
        removal = cms_support.cancellation() if removal is None else removal
        request = self.removal_request(removal)
        request_id = removal.get("cancellation_id", removal.get("tombstone_id"))
        return self.call(
            HTTP.REMOVAL_PATH,
            value=request,
            bind_payload=removal,
            request_id=request_id,
            **kwargs,
        )

    def test_capabilities_are_authenticated_complete_and_hashed(self):
        status, headers, response = self.call(
            HTTP.CAPABILITIES_PATH, method="GET",
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "no-store")
        capabilities = response["capabilities"]
        digest = capabilities.pop("sha256")
        self.assertEqual(digest, hashlib.sha256(
            self.encode(capabilities)
        ).hexdigest())
        self.assertEqual(set(capabilities["operations"]), {
            "capabilities", "change", "health", "readiness", "removal",
            "status",
        })
        self.assertTrue(
            capabilities["semantics"]["write_requires_ready_worker"]
        )
        auth = self.authenticator.requests[-1]
        self.assertEqual(auth["body_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertNotIn("source_text", json.dumps(response))

    def test_optional_composition_and_invalid_auth_preflight(self):
        self.assertIsInstance(
            self.app, HTTP.CMSSourceDeliveryHTTPApplication,
        )
        unconfigured = Path(self.directory.name) / "manual.sqlite3"
        runtime = RUNTIME.open_durable_cms_source_delivery(
            unconfigured,
            self.client,
            worker_id="manual-worker",
            clock=lambda: self.now,
            lease_seconds=60,
        )
        try:
            self.assertIsNone(runtime.http)
        finally:
            runtime.close()

        invalid = Path(self.directory.name) / "invalid.sqlite3"
        with self.assertRaises(
            RUNTIME.DurableCMSSourceDeliveryRuntimeBlocked
        ) as caught:
            RUNTIME.open_hosted_cms_source_delivery(
                invalid,
                self.client,
                worker_id="website-source-worker",
                clock=lambda: self.now,
                lease_seconds=60,
                http_authenticator="not-callable",
            )
        self.assertEqual(
            caught.exception.code,
            "source_delivery_runtime.configuration_invalid",
        )
        self.assertFalse(invalid.exists())

    def test_change_is_bound_persisted_and_site_scoped(self):
        change = cms_support.event()
        request = self.change_request(change)
        status, _headers, response = self.enqueue_change(change)

        self.assertEqual(status, "202 Accepted")
        queue = response["queue"]
        self.assertEqual(
            (queue["operation"], queue["request_id"], queue["site_id"]),
            ("change", change["event_id"], change["site_id"]),
        )
        self.assertEqual(queue["payload_sha256"], hashlib.sha256(
            self.encode(change)
        ).hexdigest())
        self.assertEqual(
            (queue["source_max_attempts"], queue["delivery_max_attempts"]),
            (4, 3),
        )
        auth = self.authenticator.requests[-1]
        self.assertEqual(auth["method"], "POST")
        self.assertEqual(auth["path"], HTTP.CHANGE_PATH)
        self.assertEqual(auth["body_sha256"], hashlib.sha256(
            self.encode(request)
        ).hexdigest())
        self.assertEqual(self.client.calls, [])

        wrong_site = copy.deepcopy(change)
        wrong_site["site_id"] = "site-2"
        denied = self.enqueue_change(wrong_site)
        self.assertEqual(denied[0], "403 Forbidden")
        self.assertEqual(
            denied[2]["error_code"], "source_delivery_http.site_rejected",
        )
        self.assertNotIn(change["localization"]["source_text"], json.dumps(
            (response, denied[2]),
        ))

    def test_http_intake_reaches_one_remote_delivery_attempt(self):
        self.runtime.close(worker_timeout_seconds=1)
        self.runtime = RUNTIME.open_hosted_cms_source_delivery(
            self.database,
            self.client,
            worker_id="website-source-worker",
            clock=lambda: self.now,
            lease_seconds=60,
            active_delay_seconds=0.01,
            idle_delay_seconds=0.01,
            blocked_delay_seconds=0.01,
            http_authenticator=self.authenticator,
        )
        self.app = self.runtime.http
        change = cms_support.event()

        accepted = self.enqueue_change(change)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.client.calls:
            time.sleep(0.01)

        self.assertEqual(accepted[0], "202 Accepted")
        self.assertEqual(self.client.calls, [("change", change, 4)])
        status = self.call(
            HTTP.STATUS_PATH, value=self.status_request(),
        )
        self.assertEqual(status[2]["status"]["status"], "succeeded")

    def test_binding_headers_are_required_and_exact(self):
        change = cms_support.event()
        request = self.change_request(change)
        missing = self.call(HTTP.CHANGE_PATH, value=request)
        wrong_id = self.enqueue_change(
            change,
            overrides={"HTTP_IDEMPOTENCY_KEY": "another-event"},
        )
        wrong_hash = self.enqueue_change(
            change,
            overrides={
                "HTTP_X_LOCALIZATION_SOURCE_PAYLOAD_SHA256": "0" * 64,
            },
        )

        for response in (missing, wrong_id, wrong_hash):
            self.assertEqual(response[0], "400 Bad Request")
            self.assertEqual(
                response[2]["error_code"],
                "source_delivery_http.binding_invalid",
            )
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_change_replay_converges_and_changed_policy_conflicts(self):
        change = cms_support.event()
        first = self.enqueue_change(change)
        replay = self.enqueue_change(change)
        changed = self.change_request(change)
        changed["delivery_max_attempts"] = 4
        conflict = self.call(
            HTTP.CHANGE_PATH,
            value=changed,
            bind_payload=change,
            request_id=change["event_id"],
        )

        self.assertEqual((first[0], replay[0]), ("202 Accepted", "202 Accepted"))
        self.assertEqual(first[2], replay[2])
        self.assertEqual(conflict[0], "409 Conflict")
        self.assertEqual(
            conflict[2]["error_code"],
            "source_delivery_http.idempotency_collision",
        )

    def test_cancellation_and_tombstone_have_separate_identities(self):
        cancellation = cms_support.cancellation()
        tombstone = cms_support.tombstone()

        cancel_response = self.enqueue_removal(cancellation)
        tombstone_response = self.enqueue_removal(tombstone)

        self.assertEqual(cancel_response[0], "202 Accepted")
        self.assertEqual(tombstone_response[0], "202 Accepted")
        self.assertEqual(
            cancel_response[2]["queue"]["operation"], "cancellation",
        )
        self.assertEqual(
            tombstone_response[2]["queue"]["operation"], "tombstone",
        )
        self.assertNotEqual(
            cancel_response[2]["queue"]["request_id"],
            tombstone_response[2]["queue"]["request_id"],
        )

    def test_status_is_read_only_tenant_bound_and_content_free(self):
        change = cms_support.event()
        self.enqueue_change(change)

        result = self.call(
            HTTP.STATUS_PATH, value=self.status_request(),
        )
        self.assertEqual(result[0], "200 OK")
        self.assertEqual(result[2]["status"]["request_id"], "event-1")
        self.assertNotIn(change["localization"]["source_text"], json.dumps(
            result[2],
        ))

        cross_site_request = self.status_request()
        self.authenticator.site_id = "site-2"
        cross_site = self.call(
            HTTP.STATUS_PATH,
            value=cross_site_request,
        )
        self.assertEqual(cross_site[0], "404 Not Found")
        self.assertEqual(
            cross_site[2]["error_code"],
            "source_delivery_http.status_not_found",
        )

        guessed = self.call(
            HTTP.STATUS_PATH,
            value=self.status_request(request_id="event-1"),
        )
        self.assertEqual(guessed[0], "404 Not Found")
        self.assertEqual(guessed[2], cross_site[2])

        self.authenticator.site_id = "site-1"
        missing = self.call(
            HTTP.STATUS_PATH,
            value=self.status_request(request_id="missing"),
        )
        self.assertEqual(missing[0], "404 Not Found")

    def test_health_and_readiness_are_bound_and_fail_closed(self):
        ready = self.call(HTTP.READINESS_PATH, method="GET")
        health = self.call(HTTP.HEALTH_PATH, method="GET")
        self.assertEqual((ready[0], health[0]), ("200 OK", "200 OK"))
        self.assertEqual(ready[2]["readiness"]["status"], "ready")
        self.assertEqual(health[2]["health"]["status"], "ok")
        self.assertEqual(
            ready[2]["capabilities_sha256"],
            health[2]["capabilities_sha256"],
        )

        self.runtime.stop_worker()
        not_ready = self.call(HTTP.READINESS_PATH, method="GET")
        rejected = self.enqueue_change()
        self.assertEqual(not_ready[0], "503 Service Unavailable")
        self.assertEqual(rejected[0], "503 Service Unavailable")
        self.assertEqual(
            rejected[2]["error_code"],
            "source_delivery_http.runtime_not_ready",
        )

    def test_tampered_storage_blocks_health_and_writes(self):
        self.database.chmod(0o640)
        health = self.call(HTTP.HEALTH_PATH, method="GET")
        write = self.enqueue_change()

        self.assertEqual(health[0], "503 Service Unavailable")
        self.assertEqual(
            health[2]["error_code"], "source_delivery_http.runtime_blocked",
        )
        self.assertEqual(write[0], "503 Service Unavailable")
        self.assertEqual(self.client.calls, [])

    def test_corrupt_row_preserves_content_free_integrity_reason(self):
        change = cms_support.event()
        self.enqueue_change(change)
        with self.runtime._lock:
            self.runtime._connection.execute(
                "UPDATE cms_source_delivery_outbox SET payload_json = '{}'"
            )

        response = self.call(HTTP.HEALTH_PATH, method="GET")

        self.assertEqual(response[0], "503 Service Unavailable")
        self.assertEqual(response[2]["schema"], HTTP.HEALTH_RESPONSE_SCHEMA)
        self.assertEqual(response[2]["health"], {
            "schema": "blun.cms-source-delivery-health.v1",
            "status": "blocked",
            "counts": {},
            "operations": {},
            "due": 0,
            "expired_leases": 0,
            "failed": 0,
            "contract_mismatches": 0,
            "error_code": "source_delivery.integrity",
        })
        self.assertNotIn(
            change["localization"]["source_text"], json.dumps(response[2]),
        )

    def test_authentication_and_scope_block_before_parsing_or_runtime(self):
        source = cms_support.event()["localization"]["source_text"]
        raw = b'{"schema":"x","schema":"y"}'
        self.authenticator.error = RuntimeError(source)
        unavailable = self.call(
            HTTP.CHANGE_PATH,
            value=None,
            overrides={
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(raw)),
                "wsgi.input": io.BytesIO(raw),
            },
        )
        self.assertEqual(unavailable[0], "503 Service Unavailable")
        self.assertEqual(
            unavailable[2]["error_code"],
            "source_delivery_http.authentication_unavailable",
        )
        self.assertNotIn(source, json.dumps(unavailable[2]))

        self.authenticator.error = None
        self.authenticator.scope = HTTP.SCOPES[HTTP.HEALTH_PATH]
        denied = self.enqueue_change()
        self.assertEqual(denied[0], "403 Forbidden")
        self.assertEqual(
            denied[2]["error_code"], "source_delivery_http.scope_rejected",
        )
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_framing_schema_and_duplicate_json_fail_closed(self):
        request = self.change_request()
        cases = (
            ({"wsgi.url_scheme": "http"}, "source_delivery_http.https_required"),
            ({"QUERY_STRING": "x=1"}, "source_delivery_http.query_rejected"),
            (
                {"HTTP_TRANSFER_ENCODING": "chunked"},
                "source_delivery_http.transfer_encoding_rejected",
            ),
            ({"CONTENT_TYPE": "text/plain"}, "source_delivery_http.content_type_invalid"),
            ({"CONTENT_LENGTH": ""}, "source_delivery_http.content_length_required"),
            (
                {"CONTENT_LENGTH": str(HTTP.MAX_BODY_BYTES + 1)},
                "source_delivery_http.body_too_large",
            ),
        )
        for overrides, code in cases:
            with self.subTest(code=code):
                response = self.call(
                    HTTP.CHANGE_PATH, value=request, overrides=overrides,
                )
                self.assertEqual(response[2]["error_code"], code)

        raw = b'{"schema":"x","schema":"y"}'
        duplicate = self.call(
            HTTP.CHANGE_PATH,
            value=None,
            overrides={
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(raw)),
                "wsgi.input": io.BytesIO(raw),
            },
        )
        self.assertEqual(duplicate[0], "400 Bad Request")
        self.assertEqual(
            duplicate[2]["error_code"], "source_delivery_http.json_invalid",
        )
        body = self.call(
            HTTP.HEALTH_PATH, method="GET", value={"unexpected": True},
        )
        self.assertEqual(
            body[2]["error_code"], "source_delivery_http.body_not_allowed",
        )
        method = self.call(HTTP.CHANGE_PATH, method="GET", value=request)
        self.assertEqual(method[0], "405 Method Not Allowed")

    def test_runtime_response_tampering_and_private_error_never_leak(self):
        change = cms_support.event()
        valid = self.runtime.enqueue_change(change)
        source = change["localization"]["source_text"]
        request = self.change_request(change)

        with mock.patch.object(
            self.runtime,
            "enqueue_change",
            return_value=replace(valid, site_id="site-2"),
        ):
            altered = self.call(
                HTTP.CHANGE_PATH,
                value=request,
                bind_payload=change,
                request_id=change["event_id"],
            )
        with mock.patch.object(
            self.runtime, "health", side_effect=RuntimeError(source),
        ):
            failed = self.call(HTTP.HEALTH_PATH, method="GET")

        self.assertEqual(altered[0], "503 Service Unavailable")
        self.assertEqual(
            altered[2]["error_code"],
            "source_delivery_http.runtime_response_invalid",
        )
        self.assertEqual(failed[0], "503 Service Unavailable")
        self.assertEqual(
            failed[2]["error_code"], "source_delivery_http.runtime_blocked",
        )
        self.assertNotIn(source, json.dumps((altered[2], failed[2])))

    def test_capability_drift_blocks_complete_response(self):
        original = HTTP.SCOPES[HTTP.CHANGE_PATH]
        HTTP.SCOPES[HTTP.CHANGE_PATH] = "weakened:write"
        try:
            response = self.call(HTTP.CAPABILITIES_PATH, method="GET")
        finally:
            HTTP.SCOPES[HTTP.CHANGE_PATH] = original

        self.assertEqual(response[0], "503 Service Unavailable")
        self.assertEqual(
            response[2]["error_code"],
            "source_delivery_http.capabilities_invalid",
        )

    def test_parallel_exact_requests_converge(self):
        change = cms_support.event()

        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(
                lambda _index: self.enqueue_change(change), range(16),
            ))

        self.assertTrue(all(item[0] == "202 Accepted" for item in responses))
        self.assertEqual(len({
            json.dumps(item[2], sort_keys=True) for item in responses
        }), 1)
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
