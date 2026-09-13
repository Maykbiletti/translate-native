from __future__ import annotations

import copy
import dataclasses
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


HTTP = load(
    "blun_test_submission_write_http",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_http.py",
)


@dataclasses.dataclass(frozen=True)
class Status:
    operation: str
    request_id: str
    event_id: str
    site_id: str
    payload_sha256: str
    capabilities_sha256: str
    status: str
    attempts: int
    delivery_max_attempts: int
    source_max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None
    lease_expired: bool
    last_error_code: str | None
    response_sha256: str | None


class RuntimeFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Runtime:
    def __init__(self):
        self.calls = []
        self.ready = True
        self.failure = None
        self.source_ready = False
        self.digest = "a" * 64
        self.binding = {
            "schema": "blun.cms-source-delivery-runtime-capability-binding.v1",
            "status": "verified",
            "database_role": "source_delivery",
            "delivery_capabilities_sha256": self.digest,
            "runtime_capabilities_sha256": "b" * 64,
            "commercial_rendering_registry_sha256": "c" * 64,
            "binding_sha256": "d" * 64,
        }

    def submission_capabilities(self):
        raise AssertionError("capabilities were not requested")

    def worker_readiness(self):
        self.calls.append(("readiness",))
        return {
            "status": "ready" if self.ready else "not_ready",
            "worker_state": "running" if self.ready else "stopped",
        }

    def website_capability_binding(self):
        self.calls.append(("binding",))
        return copy.deepcopy(self.binding)

    def _enqueue(self, payload, source_max_attempts, delivery_max_attempts):
        if self.failure is not None:
            raise self.failure
        schema = payload["schema"]
        if schema == HTTP._SUBMISSION._ADAPTER.CHANGE_SCHEMA:
            operation, request_id = "change", payload["event_id"]
        elif schema == HTTP._SUBMISSION._ADAPTER.CANCELLATION_SCHEMA:
            operation, request_id = "cancellation", payload["cancellation_id"]
        else:
            operation, request_id = "tombstone", payload["tombstone_id"]
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.calls.append((
            "enqueue", operation, request_id, source_max_attempts,
            delivery_max_attempts,
        ))
        return Status(
            operation=operation,
            request_id=request_id,
            event_id=payload["event_id"],
            site_id=payload["site_id"],
            payload_sha256=hashlib.sha256(raw).hexdigest(),
            capabilities_sha256=self.digest,
            status="pending",
            attempts=0,
            delivery_max_attempts=delivery_max_attempts,
            source_max_attempts=source_max_attempts,
            next_attempt_at=100.0,
            lease_expires_at=None,
            lease_expired=False,
            last_error_code=None,
            response_sha256=None,
        )

    def enqueue_change(self, payload, *, source_max_attempts, delivery_max_attempts):
        return self._enqueue(payload, source_max_attempts, delivery_max_attempts)

    def enqueue_removal(self, payload, *, source_max_attempts, delivery_max_attempts):
        return self._enqueue(payload, source_max_attempts, delivery_max_attempts)

    def submission_status(self, operation, request_id):
        self.calls.append(("status", operation, request_id))
        if self.failure is not None:
            raise self.failure
        accepted = self.source_ready
        return HTTP._SUBMISSION.HMACCMSSourceDeliverySubmissionStatus(
            schema=HTTP._SUBMISSION.STATUS_SCHEMA,
            operation=operation,
            request_id=request_id,
            event_id="event-1",
            site_id="site-1",
            payload_sha256="e" * 64,
            status="accepted" if accepted else "pending",
            stage="source_acceptance" if accepted else "website_acceptance",
            website_status="succeeded" if accepted else "pending",
            website_attempts=1 if accepted else 0,
            website_delivery_max_attempts=4,
            sidecar_status="succeeded" if accepted else None,
            sidecar_attempts=1 if accepted else None,
            sidecar_delivery_max_attempts=5,
            source_max_attempts=3,
            next_attempt_at=100.0,
            lease_expired=False,
            error_code=None,
            website_capability_binding=copy.deepcopy(self.binding),
        )

    def submission_lifecycle(self, operation, request_id):
        self.calls.append(("lifecycle", operation, request_id))
        status = self.submission_status(operation, request_id)
        self.calls.pop()
        source_status = None
        source_binding = None
        if self.source_ready:
            source_status = {
                "schema": "blun.cms-source-service-status.v3",
                "event_id": "event-1", "site_id": "site-1",
                "website_version": "v1", "source_sequence": 1,
                "change_sha256": "8" * 64,
                "dispatch_status": "pending", "dispatch_attempts": 0,
                "dispatch_max_attempts": 3, "dispatch_error_code": None,
                "plan_id": None, "job_count": None,
                "lifecycle_state": None, "lifecycle_poll_attempts": 0,
                "lifecycle_error_code": None, "remote_status": None,
                "lifecycle_sha256": None, "required_locales": [],
                "approved_locales": [], "blocked_locales": [],
                "queue_counts": {}, "notification_state": "disabled",
                "notification_id": None, "notification_sha256": None,
                "notification_attempts": 0, "notification_max_attempts": None,
                "notification_error_code": None,
                "terminal_processing_state": "disabled",
                "terminal_processing_poll_attempts": 0,
                "terminal_processing_failures": 0,
                "terminal_processing_error_code": None,
                "receiver_processing_state": None,
                "receiver_processing_attempts": None,
                "receiver_processing_max_attempts": None,
                "receiver_processing_error_code": None,
                "receiver_processed_at": None,
            }
            source_binding = {
                "schema": HTTP._SUBMISSION._AUTH._HTTP._SOURCE_HTTP.CAPABILITY_BINDING_SCHEMA,
                "status": "verified", "capabilities_sha256": "9" * 64,
                "commercial_rendering_registry_sha256": "7" * 64,
                "database_roles": list(
                    HTTP._SUBMISSION._AUTH._HTTP._SOURCE_HTTP.CAPABILITY_DATABASE_ROLES
                ),
            }
        return HTTP._SUBMISSION.HMACCMSSourceDeliverySubmissionLifecycle(
            schema=HTTP._SUBMISSION.LIFECYCLE_SCHEMA,
            status="accepted" if self.source_ready else status.status,
            stage="source_processing" if self.source_ready else status.stage,
            submission=status.as_payload(),
            source_status=source_status,
            source_capability_binding=source_binding,
            website_capability_binding=copy.deepcopy(self.binding),
            sidecar_capabilities_sha256="f" * 64,
            source_capabilities_sha256="9" * 64,
        )


class SubmissionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.auth_requests = []
        self.principal_site = "site-1"
        self.principal_scope = None
        self.auth_failure = None

        def authenticate(request):
            self.auth_requests.append(copy.deepcopy(request))
            if self.auth_failure is not None:
                raise self.auth_failure
            return {
                "schema": (
                    HTTP.READ_PRINCIPAL_SCHEMA
                    if request["path"] in HTTP.READ_PATHS
                    else HTTP.PRINCIPAL_SCHEMA
                ),
                "principal_id": "website-backend",
                "credential_id": "public-ingress",
                "credential_version": "1",
                "scope": self.principal_scope or HTTP.SCOPES[request["path"]],
                "site_id": self.principal_site,
            }

        self.application = HTTP.build_submission_http(
            self.runtime, authenticate,
        )

    @staticmethod
    def change():
        return {
            "schema": HTTP._SUBMISSION._ADAPTER.CHANGE_SCHEMA,
            "event_id": "event-1",
            "site_id": "site-1",
            "content": "A price of €19.90 remains protected.",
        }

    @staticmethod
    def removal(schema):
        identity = (
            "cancellation_id"
            if schema == HTTP._SUBMISSION._ADAPTER.CANCELLATION_SCHEMA
            else "tombstone_id"
        )
        return {
            "schema": schema,
            identity: "remove-1",
            "event_id": "event-1",
            "site_id": "site-1",
        }

    def request(self, path=HTTP.CHANGE_PATH, payload=None, **overrides):
        if payload is None:
            payload = self.change()
        payload_key = "change" if path == HTTP.CHANGE_PATH else "removal"
        request = {
            "schema": HTTP.REQUEST_SCHEMAS[path],
            payload_key: payload,
            "source_max_attempts": 3,
            "delivery_max_attempts": 4,
        }
        body = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if payload["schema"] == HTTP._SUBMISSION._ADAPTER.CHANGE_SCHEMA:
            request_id = payload["event_id"]
        elif payload["schema"] == HTTP._SUBMISSION._ADAPTER.CANCELLATION_SCHEMA:
            request_id = payload["cancellation_id"]
        else:
            request_id = payload["tombstone_id"]
        payload_raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        environ = {
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json; charset=utf-8",
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_IDEMPOTENCY_KEY": request_id,
            "HTTP_X_LOCALIZATION_SOURCE_PAYLOAD_SHA256": hashlib.sha256(
                payload_raw
            ).hexdigest(),
            "wsgi.input": io.BytesIO(body),
        }
        environ.update(overrides)
        captured = {}
        response_body = b"".join(self.application(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=dict(headers),
            ),
        ))
        return int(captured["status"].split(" ", 1)[0]), captured, json.loads(response_body)

    def read_request(self, path=HTTP.STATUS_PATH, **overrides):
        request = {
            "schema": HTTP.REQUEST_SCHEMAS[path],
            "operation": "change",
            "request_id": "event-1",
            "event_id": "event-1",
            "site_id": "site-1",
            "payload_sha256": "e" * 64,
        }
        body = json.dumps(
            request, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        environ = {
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        environ.update(overrides)
        captured = {}
        response_body = b"".join(self.application(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=dict(headers),
            ),
        ))
        return int(captured["status"].split(" ", 1)[0]), captured, json.loads(response_body)

    def test_change_is_authenticated_bound_and_durably_accepted(self):
        status, metadata, response = self.request()

        self.assertEqual(status, 202)
        self.assertEqual(metadata["headers"]["Cache-Control"], "no-store")
        self.assertEqual(response["schema"], HTTP.CHANGE_RESPONSE_SCHEMA)
        self.assertEqual(response["api_schema"], HTTP.API_SCHEMA)
        self.assertEqual(response["operation"], "change")
        self.assertEqual(response["request_id"], "event-1")
        self.assertEqual(response["site_id"], "site-1")
        self.assertEqual(response["source_max_attempts"], 3)
        self.assertEqual(response["delivery_max_attempts"], 4)
        self.assertFalse(response["accepted_implies_publication"])
        expected_body = json.dumps({
            "schema": HTTP.CHANGE_REQUEST_SCHEMA,
            "change": self.change(),
            "source_max_attempts": 3,
            "delivery_max_attempts": 4,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self.assertEqual(
            self.auth_requests[0]["body_sha256"],
            hashlib.sha256(expected_body).hexdigest(),
        )
        self.assertEqual(self.runtime.calls[0], ("readiness",))
        self.assertEqual(self.runtime.calls[1], ("binding",))
        self.assertEqual(self.runtime.calls[2], ("enqueue", "change", "event-1", 3, 4))

    def test_each_removal_type_uses_its_own_idempotency_identity(self):
        for schema, operation in (
            (HTTP._SUBMISSION._ADAPTER.CANCELLATION_SCHEMA, "cancellation"),
            (HTTP._SUBMISSION._ADAPTER.TOMBSTONE_SCHEMA, "tombstone"),
        ):
            with self.subTest(schema=schema):
                self.runtime.calls.clear()
                payload = self.removal(schema)
                status, _metadata, response = self.request(
                    HTTP.REMOVAL_PATH, payload,
                )
                self.assertEqual(status, 202)
                self.assertEqual(response["schema"], HTTP.REMOVAL_RESPONSE_SCHEMA)
                self.assertEqual(response["operation"], operation)
                self.assertEqual(response["request_id"], "remove-1")
                self.assertEqual(self.runtime.calls[-1][1], operation)

    def test_invalid_authentication_site_and_bindings_never_touch_runtime(self):
        cases = (
            ("scope", {"HTTP_IDEMPOTENCY_KEY": "event-1"}, 403),
            ("site", {"HTTP_IDEMPOTENCY_KEY": "event-1"}, 404),
            ("idempotency", {"HTTP_IDEMPOTENCY_KEY": "event-2"}, 400),
            ("payload", {"HTTP_X_LOCALIZATION_SOURCE_PAYLOAD_SHA256": "f" * 64}, 400),
        )
        for name, overrides, expected in cases:
            with self.subTest(name=name):
                self.runtime.calls.clear()
                self.principal_scope = "wrong:scope" if name == "scope" else None
                self.principal_site = "site-2" if name == "site" else "site-1"
                status, _metadata, _response = self.request(**overrides)
                self.assertEqual(status, expected)
                self.assertEqual(self.runtime.calls, [])
        self.principal_scope = None
        self.principal_site = "site-1"

    def test_unavailable_authentication_and_worker_fail_closed(self):
        self.auth_failure = RuntimeError("private identity provider detail")
        status, _metadata, response = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(response["error_code"], "submission_http.authentication_unavailable")
        self.assertEqual(self.runtime.calls, [])
        self.assertNotIn("private", repr(response))

        self.auth_failure = None
        self.runtime.ready = False
        status, _metadata, response = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(response["error_code"], "submission_http.runtime_not_ready")
        self.assertEqual(self.runtime.calls, [("readiness",)])

    def test_invalid_json_and_transport_shape_fail_before_runtime(self):
        for overrides, expected in (
            ({"wsgi.url_scheme": "http"}, 400),
            ({"QUERY_STRING": "retry=1"}, 400),
            ({"REQUEST_METHOD": "GET"}, 405),
            ({"CONTENT_TYPE": "text/plain"}, 415),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, 400),
        ):
            with self.subTest(overrides=overrides):
                self.runtime.calls.clear()
                status, _metadata, _response = self.request(**overrides)
                self.assertEqual(status, expected)
                self.assertEqual(self.runtime.calls, [])

        duplicate = b'{"schema":"x","schema":"y"}'
        environ = {
            "PATH_INFO": HTTP.CHANGE_PATH,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(duplicate)),
            "HTTP_IDEMPOTENCY_KEY": "event-1",
            "HTTP_X_LOCALIZATION_SOURCE_PAYLOAD_SHA256": "a" * 64,
            "wsgi.input": io.BytesIO(duplicate),
        }
        captured = {}
        response = json.loads(b"".join(self.application(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=dict(headers),
            ),
        )))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertEqual(response["error_code"], "submission_http.json_invalid")
        self.assertEqual(self.runtime.calls, [])

    def test_idempotency_collision_is_content_free(self):
        self.runtime.failure = RuntimeFailure(
            "source_delivery_runtime.idempotency_collision"
        )

        status, _metadata, response = self.request()

        self.assertEqual(status, 409)
        self.assertEqual(response, {
            "schema": HTTP.ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": "submission_http.idempotency_collision",
        })

    def test_tampered_runtime_status_is_never_accepted(self):
        original = self.runtime._enqueue

        def tampered(*args, **kwargs):
            status = original(*args, **kwargs)
            return dataclasses.replace(status, payload_sha256="f" * 64)

        self.runtime._enqueue = tampered

        status, _metadata, response = self.request()

        self.assertEqual(status, 503)
        self.assertEqual(response["error_code"], "submission_http.runtime_response_invalid")

    def test_builder_rejects_incomplete_dependencies(self):
        with self.assertRaises(TypeError):
            HTTP.build_submission_http(object(), lambda _request: {})
        with self.assertRaises(TypeError):
            HTTP.build_submission_http(self.runtime, object())

    def test_status_is_authenticated_and_keeps_acceptance_distinct(self):
        status, metadata, response = self.read_request()

        self.assertEqual(status, 200)
        self.assertEqual(metadata["headers"]["Cache-Control"], "no-store")
        self.assertEqual(response["schema"], HTTP.RESPONSE_SCHEMAS[HTTP.STATUS_PATH])
        self.assertEqual(response["api_schema"], HTTP.API_SCHEMA)
        self.assertEqual(response["result"]["status"], "pending")
        self.assertEqual(response["result"]["stage"], "website_acceptance")
        self.assertFalse(response["accepted_implies_publication"])
        self.assertEqual(self.runtime.calls, [("status", "change", "event-1")])
        self.assertEqual(
            self.auth_requests[-1]["schema"], HTTP.READ_AUTH_REQUEST_SCHEMA,
        )

    def test_lifecycle_route_does_not_collapse_pending_submission(self):
        status, _metadata, response = self.read_request(HTTP.LIFECYCLE_PATH)

        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["stage"], "website_acceptance")
        self.assertIsNone(response["result"]["source_status"])
        self.assertEqual(self.runtime.calls, [("lifecycle", "change", "event-1")])

    def test_lifecycle_exposes_validated_source_state_without_content(self):
        self.runtime.source_ready = True

        status, _metadata, response = self.read_request(HTTP.LIFECYCLE_PATH)

        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["stage"], "source_processing")
        self.assertEqual(
            response["result"]["source_status"]["dispatch_status"],
            "pending",
        )
        self.assertNotIn("content", repr(response["result"]))

    def test_lifecycle_rejects_cross_site_source_projection(self):
        self.runtime.source_ready = True
        original = self.runtime.submission_lifecycle

        def tampered(operation, request_id):
            value = original(operation, request_id)
            source = copy.deepcopy(value.source_status)
            source["site_id"] = "site-2"
            return dataclasses.replace(value, source_status=source)

        self.runtime.submission_lifecycle = tampered

        status, _metadata, response = self.read_request(HTTP.LIFECYCLE_PATH)

        self.assertEqual(status, 503)
        self.assertEqual(
            response["error_code"], "submission_http.runtime_response_invalid",
        )

    def test_read_scope_site_and_identity_fail_before_runtime(self):
        self.principal_scope = "wrong:scope"
        status, _metadata, _response = self.read_request()
        self.assertEqual(status, 403)
        self.assertEqual(self.runtime.calls, [])

        self.principal_scope = None
        self.principal_site = "site-2"
        status, _metadata, _response = self.read_request()
        self.assertEqual(status, 404)
        self.assertEqual(self.runtime.calls, [])

    def test_cross_bound_runtime_status_and_missing_submission_fail_closed(self):
        original = self.runtime.submission_status

        def tampered(operation, request_id):
            value = original(operation, request_id)
            return dataclasses.replace(value, payload_sha256="0" * 64)

        self.runtime.submission_status = tampered
        status, _metadata, response = self.read_request()
        self.assertEqual(status, 503)
        self.assertEqual(
            response["error_code"], "submission_http.runtime_response_invalid",
        )

        self.runtime.calls.clear()
        self.runtime.submission_status = original
        self.runtime.failure = RuntimeFailure(
            "source_delivery_runtime.status_not_found"
        )
        status, _metadata, response = self.read_request()
        self.assertEqual(status, 404)
        self.assertEqual(
            response["error_code"], "submission_http.submission_not_found",
        )


if __name__ == "__main__":
    unittest.main()
