from __future__ import annotations

import copy
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


SUBMISSION = load(
    "blun_test_submission_capabilities_http_runtime",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_runtime.py",
)
HTTP = load(
    "blun_test_submission_capabilities_http",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_capabilities_http.py",
)


class CapabilityView:
    def __init__(self, payload):
        self.payload = copy.deepcopy(payload)

    def as_payload(self):
        return copy.deepcopy(self.payload)


class Runtime:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.failure = None

    def submission_capabilities(self):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return CapabilityView(self.payload)


def capability_payload():
    binding = {
        "schema": "blun.cms-source-delivery-runtime-capability-binding.v1",
        "status": "verified",
        "database_role": "source_delivery",
        "delivery_capabilities_sha256": "a" * 64,
        "runtime_capabilities_sha256": "b" * 64,
        "commercial_rendering_registry_sha256": "c" * 64,
        "binding_sha256": "d" * 64,
    }
    body = {
        "schema": SUBMISSION.CAPABILITIES_SCHEMA,
        "operations": {
            "capabilities_http": {
                "kind": "read",
                "method": "GET",
                "path": HTTP.CAPABILITIES_PATH,
                "scope": HTTP.CAPABILITIES_SCOPE,
                "principal_schema": HTTP.PRINCIPAL_SCHEMA,
                "request_schema": None,
                "response_schema": HTTP.CAPABILITIES_RESPONSE_SCHEMA,
            },
            "enqueue_change": {
                "kind": "write",
                "request_schemas": [
                    SUBMISSION._ADAPTER.CHANGE_SCHEMA,
                ],
            },
            "enqueue_removal": {
                "kind": "write",
                "request_schemas": [
                    SUBMISSION._ADAPTER.CANCELLATION_SCHEMA,
                    SUBMISSION._ADAPTER.TOMBSTONE_SCHEMA,
                ],
            },
            "submission_status": {
                "kind": "read",
                "response_schema": SUBMISSION.STATUS_SCHEMA,
            },
            "submission_lifecycle": {
                "kind": "read",
                "response_schema": SUBMISSION.LIFECYCLE_SCHEMA,
            },
            "submission_health": {
                "kind": "read",
                "response_schema": SUBMISSION.HEALTH_SCHEMA,
            },
            "submission_pipeline_health": {
                "kind": "read",
                "response_schema": SUBMISSION.PIPELINE_HEALTH_SCHEMA,
            },
            "submission_readiness": {
                "kind": "read",
                "response_schema": SUBMISSION.READINESS_SCHEMA,
            },
            "submission_pipeline_readiness": {
                "kind": "read",
                "response_schema": SUBMISSION.PIPELINE_READINESS_SCHEMA,
            },
        },
        "retry_budgets": {
            "website_to_sidecar": {
                "configured_per_request": True,
                "minimum": 1,
                "maximum": 20,
            },
            "sidecar_to_source": {
                "configured_per_runtime": True,
                "maximum_attempts": 4,
            },
            "source_processing": {
                "configured_per_request": True,
                "minimum": 1,
                "maximum": 20,
            },
        },
        "semantics": {
            "content_free": True,
            "accepted_means": "durable_source_acceptance",
            "accepted_implies_publication": False,
            "translation_generation": False,
            "publication_authority": False,
        },
        "sidecar_capabilities_sha256": "e" * 64,
        "source_capabilities_sha256": "f" * 64,
        "website_capability_binding": binding,
    }
    body["sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return body


class SubmissionCapabilitiesHTTPTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime(capability_payload())
        self.auth_requests = []

        def authenticate(request):
            self.auth_requests.append(copy.deepcopy(request))
            return {
                "schema": HTTP.PRINCIPAL_SCHEMA,
                "principal_id": "deployment-operator",
                "credential_id": "capability-reader",
                "credential_version": "1",
                "scope": HTTP.CAPABILITIES_SCOPE,
            }

        self.authenticate = authenticate
        self.application = HTTP.build_submission_capabilities_http(
            self.runtime, self.authenticate,
        )

    def request(self, **overrides):
        environ = {
            "PATH_INFO": HTTP.CAPABILITIES_PATH,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "GET",
            "wsgi.url_scheme": "https",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
            "HTTP_AUTHORIZATION": "Proof redacted",
        }
        environ.update(overrides)
        captured = {}
        body = b"".join(self.application(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=dict(headers),
            ),
        ))
        return int(captured["status"].split()[0]), captured["headers"], json.loads(body)

    def test_authenticated_get_returns_exact_content_free_capabilities(self):
        status, headers, response = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(response, {
            "schema": HTTP.CAPABILITIES_RESPONSE_SCHEMA,
            "api_schema": HTTP.API_SCHEMA,
            "capabilities": capability_payload(),
        })
        self.assertEqual(self.runtime.calls, 1)
        self.assertEqual(len(self.auth_requests), 1)
        self.assertEqual(self.auth_requests[0]["schema"], HTTP.AUTH_REQUEST_SCHEMA)
        self.assertEqual(self.auth_requests[0]["method"], "GET")
        self.assertEqual(self.auth_requests[0]["path"], HTTP.CAPABILITIES_PATH)
        self.assertEqual(
            self.auth_requests[0]["body_sha256"], HTTP.EMPTY_SHA256,
        )
        rendered = repr(response)
        for private in (
            "source_text", "target_text", "site_id", "secret", "endpoint",
        ):
            self.assertNotIn(private, rendered)

    def test_authentication_runs_before_runtime_or_downstream_access(self):
        def unavailable(_request):
            raise OSError("private verifier failure")

        self.application = HTTP.build_submission_capabilities_http(
            self.runtime, unavailable,
        )
        status, _headers, response = self.request()

        self.assertEqual(status, 503)
        self.assertEqual(self.runtime.calls, 0)
        self.assertEqual(response, {
            "schema": HTTP.ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": (
                "submission_capabilities_http.authentication_unavailable"
            ),
        })
        self.assertNotIn("private", repr(response))

    def test_wrong_scope_blocks_without_runtime_access(self):
        def wrong_scope(_request):
            return {
                "schema": HTTP.PRINCIPAL_SCHEMA,
                "principal_id": "deployment-operator",
                "credential_id": "capability-reader",
                "credential_version": "1",
                "scope": "source-delivery-submission-capabilities:write",
            }

        self.application = HTTP.build_submission_capabilities_http(
            self.runtime, wrong_scope,
        )
        status, _headers, response = self.request()

        self.assertEqual(status, 403)
        self.assertEqual(self.runtime.calls, 0)
        self.assertEqual(
            response["error_code"],
            "submission_capabilities_http.scope_rejected",
        )

    def test_route_is_strictly_https_get_without_query_or_body(self):
        cases = (
            ({"wsgi.url_scheme": "http"}, 400, "https_required"),
            ({"REQUEST_METHOD": "POST"}, 405, "method_not_allowed"),
            ({"QUERY_STRING": "debug=1"}, 400, "query_rejected"),
            ({"CONTENT_LENGTH": "2"}, 400, "body_not_allowed"),
            ({"CONTENT_TYPE": "application/json"}, 400, "body_not_allowed"),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, 400, "transfer_encoding_rejected"),
            ({"PATH_INFO": "/wrong"}, 404, "route_not_found"),
        )
        for overrides, expected_status, code in cases:
            with self.subTest(code=code):
                before_auth = len(self.auth_requests)
                before_runtime = self.runtime.calls
                status, _headers, response = self.request(**overrides)
                self.assertEqual(status, expected_status)
                self.assertEqual(
                    response["error_code"],
                    "submission_capabilities_http." + code,
                )
                self.assertEqual(len(self.auth_requests), before_auth)
                self.assertEqual(self.runtime.calls, before_runtime)

    def test_tampered_or_substituted_capability_never_leaves_boundary(self):
        mutations = []

        wrong_schema = capability_payload()
        wrong_schema["schema"] = "foreign"
        mutations.append(wrong_schema)

        private_field = capability_payload()
        private_field["endpoint"] = "https://private.example"
        mutations.append(private_field)

        changed_contract = capability_payload()
        changed_contract["semantics"]["publication_authority"] = True
        changed_contract["sha256"] = hashlib.sha256(
            HTTP._canonical({
                key: value for key, value in changed_contract.items()
                if key != "sha256"
            })
        ).hexdigest()
        mutations.append(changed_contract)

        stale_hash = capability_payload()
        stale_hash["operations"]["submission_status"]["kind"] = "write"
        mutations.append(stale_hash)

        for payload in mutations:
            with self.subTest(mutation=repr(payload)[:80]):
                self.runtime.payload = payload
                status, _headers, response = self.request()
                self.assertEqual(status, 503)
                self.assertEqual(
                    response["error_code"],
                    "submission_capabilities_http.runtime_response_invalid",
                )
                self.assertNotIn("private.example", repr(response))

    def test_runtime_failure_is_content_free(self):
        self.runtime.failure = RuntimeError("database path is private")

        status, _headers, response = self.request()

        self.assertEqual(status, 503)
        self.assertEqual(
            response["error_code"],
            "submission_capabilities_http.runtime_blocked",
        )
        self.assertNotIn("database", repr(response))

    def test_builder_rejects_incomplete_dependencies(self):
        with self.assertRaises(TypeError):
            HTTP.build_submission_capabilities_http(object(), self.authenticate)
        with self.assertRaises(TypeError):
            HTTP.build_submission_capabilities_http(self.runtime, object())


if __name__ == "__main__":
    unittest.main()
