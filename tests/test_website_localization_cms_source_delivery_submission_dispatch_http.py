from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests import test_website_localization_cms_client as cms_support
from tests import (
    test_website_localization_cms_source_delivery_submission_client
    as client_support,
)
from tests import (
    test_website_localization_cms_source_delivery_submission_dispatch_runtime
    as runtime_support,
)


HTTP = runtime_support.RUNTIME._HTTP
RUNTIME = runtime_support.RUNTIME


class Authenticator:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.site_id = "site-1"
        self.scope = None

    def __call__(self, request):
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        scope = self.scope or HTTP.SCOPES[request["path"]]
        principal = {
            "schema": HTTP.OPERATOR_PRINCIPAL_SCHEMA,
            "principal_id": "operator-1",
            "credential_id": "credential-1",
            "credential_version": "1",
            "scope": scope,
        }
        if request["path"] in HTTP.TENANT_PATHS:
            principal["schema"] = HTTP.TENANT_PRINCIPAL_SCHEMA
            principal["site_id"] = self.site_id
        return principal


class SubmissionDispatchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "public-submissions.sqlite3"
        self.support = client_support.SourceDeliverySubmissionClientTests(
            methodName="runTest"
        )
        self.support.setUp()
        self.authenticator = Authenticator()
        self.runtime = None

    def tearDown(self):
        if self.runtime is not None:
            if self.database.exists() and not self.database.is_symlink():
                os.chmod(self.database, 0o600)
            try:
                self.runtime.close(worker_timeout_seconds=1)
            except Exception:
                pass
        self.support.tearDown()
        self.directory.cleanup()

    def open(self, *, hosted=True, **kwargs):
        factory = (
            RUNTIME.open_hosted_cms_source_delivery_submission_dispatch
            if hosted
            else RUNTIME.open_durable_cms_source_delivery_submission_dispatch
        )
        self.runtime = factory(
            self.database,
            self.support.client,
            worker_id="cms-public-submission-worker",
            clock=lambda: self.now,
            lease_seconds=60,
            http_authenticator=self.authenticator,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
            **kwargs,
        ) if hosted else factory(
            self.database,
            self.support.client,
            worker_id="cms-public-submission-worker",
            clock=lambda: self.now,
            lease_seconds=60,
            http_authenticator=self.authenticator,
            **kwargs,
        )
        return self.runtime

    @staticmethod
    def canonical(value):
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def call(
        self, path, *, method=None, body=None, headers=None, scheme="https",
        query="",
    ):
        raw = b"" if body is None else (
            body if isinstance(body, bytes) else self.canonical(body)
        )
        environ = {
            "PATH_INFO": path,
            "REQUEST_METHOD": method or HTTP.METHODS.get(path, "GET"),
            "QUERY_STRING": query,
            "wsgi.url_scheme": scheme,
            "wsgi.input": io.BytesIO(raw),
            "CONTENT_LENGTH": str(len(raw)),
            "HTTP_AUTHORIZATION": "Bearer test",
        }
        if body is not None:
            environ["CONTENT_TYPE"] = "application/json; charset=utf-8"
        for name, value in (headers or {}).items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
        response = {}

        def start_response(status, response_headers):
            response["status"] = int(status.split()[0])
            response["headers"] = dict(response_headers)

        chunks = self.runtime.http(environ, start_response)
        response["raw"] = b"".join(chunks)
        response["json"] = json.loads(response["raw"])
        return response

    @staticmethod
    def enqueue_request(payload, **overrides):
        request = {
            "schema": HTTP.ENQUEUE_REQUEST_SCHEMA,
            "payload": payload,
            "source_max_attempts": 3,
            "delivery_max_attempts": 4,
            "client_max_attempts": 2,
        }
        request.update(overrides)
        return request

    def enqueue(self, payload=None, **overrides):
        payload = cms_support.event() if payload is None else payload
        _copied, identity, canonical = HTTP._DISPATCH._payload(payload)
        return self.call(
            HTTP.ENQUEUE_PATH,
            body=self.enqueue_request(payload, **overrides),
            headers={
                "Idempotency-Key": identity["request_id"],
                "X-Localization-Source-Payload-SHA256": hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest(),
            },
        )

    def test_capabilities_are_exact_pinned_and_content_free(self):
        runtime = self.open()
        response = self.call(HTTP.CAPABILITIES_PATH)

        self.assertEqual(response["status"], 200)
        capabilities = response["json"]["capabilities"]
        self.assertEqual(
            set(capabilities["operations"]),
            {"capabilities", "enqueue", "health", "openapi", "readiness", "status"},
        )
        self.assertEqual(
            capabilities["public_submission_capabilities_sha256"],
            runtime.expected_capabilities_sha256,
        )
        self.assertTrue(
            capabilities["semantics"]["authentication_precedes_json_parsing"]
        )
        self.assertTrue(
            capabilities["semantics"]["write_requires_ready_managed_worker"]
        )
        self.assertEqual(
            capabilities["operations"]["enqueue"]["error_statuses"],
            [400, 401, 403, 405, 409, 411, 413, 415, 503],
        )
        self.assertEqual(
            capabilities["operations"]["status"]["error_statuses"],
            [400, 401, 403, 404, 405, 411, 413, 415, 503],
        )
        unsigned = dict(capabilities)
        digest = unsigned.pop("sha256")
        self.assertEqual(digest, hashlib.sha256(self.canonical(unsigned)).hexdigest())
        rendered = response["raw"].decode("utf-8")
        self.assertNotIn("source_text", rendered)
        self.assertNotIn("target_text", rendered)

    def test_openapi_is_capability_bound_origin_free_and_content_free(self):
        self.open()
        response = self.call(HTTP.OPENAPI_PATH)

        self.assertEqual(response["status"], 200)
        self.assertEqual(set(response["json"]), {
            "schema", "openapi", "openapi_sha256", "capabilities_sha256",
        })
        capabilities = HTTP._capabilities_payload(
            self.runtime.expected_capabilities_sha256
        )
        document = response["json"]["openapi"]
        self.assertEqual(document, HTTP._OPENAPI.build_document(capabilities))
        self.assertEqual(
            response["json"]["openapi_sha256"],
            HTTP._OPENAPI.document_sha256(document),
        )
        self.assertEqual(
            response["json"]["capabilities_sha256"], capabilities["sha256"]
        )
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertNotIn("servers", document)
        self.assertEqual(set(document["paths"]), {
            operation["path"] for operation in capabilities["operations"].values()
        })
        for operation in capabilities["operations"].values():
            described = document["paths"][operation["path"]][
                operation["method"].lower()
            ]
            self.assertEqual(described["x-authentication-scope"], operation["scope"])
            self.assertEqual(described["x-principal-schema"], operation["principal_schema"])
            self.assertEqual(described["x-success-status"], operation["success_status"])
            self.assertEqual(described["x-error-statuses"], operation["error_statuses"])
            self.assertEqual(
                described["x-response-invariants"],
                operation["response_invariants"],
            )
            self.assertEqual(
                set(described["responses"]),
                {str(operation["success_status"]), *map(str, operation["error_statuses"])},
            )
            self.assertNotIn("default", described["responses"])
        rendered = response["raw"].decode("utf-8")
        for private in (
            cms_support.event()["localization"]["source_text"],
            "target_text", "Bearer test", "site-1",
        ):
            self.assertNotIn(private, rendered)

    def test_openapi_describes_all_three_exact_source_payloads(self):
        self.open()
        document = self.call(HTTP.OPENAPI_PATH)["json"]["openapi"]
        schemas = document["components"]["schemas"]
        contracts = HTTP._capabilities_payload(
            self.runtime.expected_capabilities_sha256
        )["source_payload_schemas"]

        self.assertEqual(schemas["SourcePayload"]["oneOf"], [
            {"$ref": "#/components/schemas/ContentChange"},
            {"$ref": "#/components/schemas/ContentCancellation"},
            {"$ref": "#/components/schemas/ContentTombstone"},
        ])
        self.assertEqual(
            set(schemas["SourcePayload"]["discriminator"]["mapping"]),
            set(contracts.values()),
        )
        fixtures = {
            "ContentChange": cms_support.event(),
            "ContentCancellation": cms_support.cancellation(),
            "ContentTombstone": cms_support.tombstone(),
        }
        for name, fixture in fixtures.items():
            schema = schemas[name]
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(fixture))
            self.assertEqual(set(schema["properties"]), set(fixture))
            self.assertEqual(
                schema["properties"]["schema"]["const"], fixture["schema"]
            )

        localization = schemas["LocalizationRequest"]
        self.assertFalse(localization["additionalProperties"])
        self.assertEqual(
            set(localization["properties"]),
            set(cms_support.event()["localization"]),
        )
        self.assertNotIn("target_locales", localization["required"])
        target = localization["properties"]["target_locales"]
        self.assertTrue(target["uniqueItems"])
        self.assertTrue(target["x-source-language-excluded"])
        self.assertEqual(tuple(target["items"]["enum"]), HTTP._OPENAPI.EU_TARGET_LOCALES)
        self.assertEqual(len(target["items"]["enum"]), 24)
        self.assertTrue(localization["properties"]["source_locale"]["x-canonical-bcp47"])
        self.assertEqual(
            localization["properties"]["source_text"]["x-max-utf8-bytes"],
            1_000_000,
        )

    def test_openapi_capabilities_schema_is_recursively_exact_and_closed(self):
        self.open()
        response = self.call(HTTP.OPENAPI_PATH)["json"]
        document = response["openapi"]
        capabilities = HTTP._capabilities_payload(
            self.runtime.expected_capabilities_sha256
        )
        schema = document["components"]["schemas"]["Capabilities"]

        def assert_exact(described, value):
            if isinstance(value, dict):
                self.assertEqual(described["type"], "object")
                self.assertFalse(described["additionalProperties"])
                self.assertEqual(described["required"], sorted(value))
                self.assertEqual(set(described["properties"]), set(value))
                for name, content in value.items():
                    assert_exact(described["properties"][name], content)
                return
            if value is None:
                self.assertEqual(described, {"type": "null"})
                return
            if isinstance(value, list):
                self.assertEqual(described["type"], "array")
                self.assertFalse(described["items"])
                self.assertEqual(described["minItems"], len(value))
                self.assertEqual(described["maxItems"], len(value))
                self.assertEqual(len(described["prefixItems"]), len(value))
                for item_schema, item in zip(described["prefixItems"], value):
                    assert_exact(item_schema, item)
                return
            expected_type = (
                "boolean" if isinstance(value, bool)
                else "integer" if isinstance(value, int)
                else "number" if isinstance(value, float)
                else "string"
            )
            self.assertEqual(described, {"type": expected_type, "const": value})

        core = {
            name: value for name, value in schema.items()
            if name not in {"description", "x-capabilities-sha256"}
        }
        assert_exact(core, capabilities)
        self.assertEqual(
            document["components"]["schemas"]["CapabilitiesResponse"]
            ["properties"]["capabilities"],
            {"$ref": "#/components/schemas/Capabilities"},
        )
        self.assertEqual(
            capabilities["openapi_document_schema"], document["x-schema"]
        )
        self.assertEqual(schema["x-capabilities-sha256"], capabilities["sha256"])

    def test_openapi_models_exact_errors_and_degraded_monitor_responses(self):
        self.open()
        document = self.call(HTTP.OPENAPI_PATH)["json"]["openapi"]
        capabilities = HTTP._capabilities_payload(
            self.runtime.expected_capabilities_sha256
        )

        for name, operation in capabilities["operations"].items():
            described = document["paths"][operation["path"]][
                operation["method"].lower()
            ]
            for status in operation["error_statuses"]:
                schema = described["responses"][str(status)]["content"][
                    "application/json"
                ]["schema"]
                if name in {"health", "readiness"} and status == 503:
                    expected = name.title() + "Response"
                    self.assertEqual(schema["oneOf"], [
                        {"$ref": "#/components/schemas/" + expected},
                        {"$ref": "#/components/schemas/Error"},
                    ])
                else:
                    self.assertEqual(
                        schema, {"$ref": "#/components/schemas/Error"}
                    )

    def test_openapi_encodes_runtime_state_invariants(self):
        self.open()
        schemas = self.call(HTTP.OPENAPI_PATH)["json"]["openapi"][
            "components"
        ]["schemas"]

        status = schemas["SubmissionStatus"]
        self.assertEqual(status["x-invariants"], [
            "attempts_lte_client_max_attempts",
            "leased_iff_lease_expires_at",
            "accepted_iff_remote_binding_complete",
        ])
        self.assertEqual(
            status["allOf"][0]["then"]["properties"]["lease_expires_at"]["type"],
            "number",
        )
        self.assertEqual(
            status["allOf"][0]["else"]["properties"]["lease_expires_at"],
            {"type": "null"},
        )
        self.assertEqual(
            status["allOf"][1]["then"]["properties"]["remote_status"]["type"],
            "string",
        )
        self.assertEqual(
            schemas["Health"]["allOf"][0]["then"]["properties"]["failed"],
            {"const": 0},
        )
        ready = schemas["Readiness"]["oneOf"]
        self.assertEqual(ready[0]["properties"]["worker_state"], {"const": "running"})
        self.assertEqual(ready[0]["properties"]["error_code"], {"type": "null"})
        self.assertEqual(
            ready[1]["properties"]["error_code"],
            {"$ref": "#/components/schemas/ErrorCode"},
        )

    def test_enqueue_commits_before_202_and_status_requires_full_identity(self):
        self.open()
        accepted = self.enqueue()

        self.assertEqual(accepted["status"], 202)
        self.assertFalse(accepted["json"]["accepted_implies_publication"])
        status = accepted["json"]["status"]
        self.assertEqual(status["status"], "pending")
        self.assertEqual(
            (
                status["source_max_attempts"],
                status["delivery_max_attempts"],
                status["client_max_attempts"],
            ),
            (3, 4, 2),
        )
        row = self.runtime.status(status["operation"], status["request_id"])
        self.assertEqual(row.payload_sha256, status["payload_sha256"])

        query = {
            "schema": HTTP.STATUS_REQUEST_SCHEMA,
            **{
                key: status[key]
                for key in (
                    "operation", "request_id", "event_id", "site_id",
                    "payload_sha256",
                )
            },
        }
        found = self.call(HTTP.STATUS_PATH, body=query)
        self.assertEqual(found["status"], 200)
        self.assertEqual(found["json"]["status"], status)
        self.assertNotIn(
            cms_support.event()["localization"]["source_text"],
            found["raw"].decode("utf-8"),
        )

    def test_change_cancellation_and_tombstone_are_independent_durable_rows(self):
        self.open()
        change = cms_support.event()
        payloads = (
            change,
            cms_support.cancellation(change),
            cms_support.tombstone(change),
        )

        accepted = [self.enqueue(payload)["json"]["status"] for payload in payloads]

        self.assertEqual(
            [item["operation"] for item in accepted],
            ["change", "cancellation", "tombstone"],
        )
        with self.runtime._lock:
            rows = self.runtime._connection.execute(
                "SELECT operation, count(*) FROM cms_public_submission_outbox "
                "GROUP BY operation ORDER BY operation"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("cancellation", 1), ("change", 1), ("tombstone", 1)],
        )

    def test_authentication_precedes_json_parsing_and_receives_exact_hash(self):
        self.open()
        self.authenticator.failure = RuntimeError("identity provider unavailable")
        raw = b"{not-json"
        response = self.call(HTTP.ENQUEUE_PATH, body=raw)

        self.assertEqual(response["status"], 503)
        self.assertEqual(
            response["json"]["error_code"],
            "submission_dispatch_http.authentication_unavailable",
        )
        self.assertEqual(
            self.authenticator.calls[-1]["body_sha256"],
            hashlib.sha256(raw).hexdigest(),
        )

    def test_tenant_header_and_scope_mismatches_block_without_persistence(self):
        self.open()
        payload = cms_support.event()
        _copy, identity, canonical = HTTP._DISPATCH._payload(payload)
        headers = {
            "Idempotency-Key": identity["request_id"],
            "X-Localization-Source-Payload-SHA256": hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest(),
        }
        for mutate in ("site", "idempotency", "hash", "scope"):
            with self.subTest(mutate=mutate):
                self.authenticator.site_id = (
                    "site-2" if mutate == "site" else "site-1"
                )
                self.authenticator.scope = (
                    "wrong:scope" if mutate == "scope" else None
                )
                changed = dict(headers)
                if mutate == "idempotency":
                    changed["Idempotency-Key"] = "another-request"
                if mutate == "hash":
                    changed["X-Localization-Source-Payload-SHA256"] = "0" * 64
                response = self.call(
                    HTTP.ENQUEUE_PATH,
                    body=self.enqueue_request(payload),
                    headers=changed,
                )
                self.assertIn(response["status"], {400, 403})
        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ):
            self.runtime.status("change", payload["event_id"])

    def test_unmanaged_and_stopped_workers_reject_before_json_parsing(self):
        self.open(hosted=False)
        response = self.call(HTTP.ENQUEUE_PATH, body=b"{not-json")
        self.assertEqual(response["status"], 503)
        self.assertEqual(
            response["json"]["error_code"],
            "submission_dispatch_http.runtime_not_ready",
        )

        self.runtime.start_worker(idle_delay_seconds=10)
        self.runtime.stop_worker()
        response = self.call(HTTP.ENQUEUE_PATH, body=b"{still-not-json")
        self.assertEqual(response["status"], 503)
        self.assertEqual(
            response["json"]["error_code"],
            "submission_dispatch_http.runtime_not_ready",
        )

    def test_replay_is_idempotent_and_changed_budget_conflicts(self):
        self.open()
        first = self.enqueue()
        second = self.enqueue()
        conflict = self.enqueue(client_max_attempts=3)

        self.assertEqual(first["json"], second["json"])
        self.assertEqual(conflict["status"], 409)
        self.assertEqual(
            conflict["json"]["error_code"],
            "submission_dispatch_http.idempotency_collision",
        )
        with self.runtime._lock:
            count = self.runtime._connection.execute(
                "SELECT count(*) FROM cms_public_submission_outbox"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_cross_tenant_and_wrong_hash_status_match_missing(self):
        self.open()
        accepted = self.enqueue()["json"]["status"]
        base = {
            "schema": HTTP.STATUS_REQUEST_SCHEMA,
            **{
                key: accepted[key]
                for key in (
                    "operation", "request_id", "event_id", "site_id",
                    "payload_sha256",
                )
            },
        }
        self.authenticator.site_id = "site-2"
        tenant = self.call(HTTP.STATUS_PATH, body=base)
        self.authenticator.site_id = "site-1"
        wrong_hash = self.call(
            HTTP.STATUS_PATH, body={**base, "payload_sha256": "0" * 64}
        )
        missing = self.call(
            HTTP.STATUS_PATH,
            body={**base, "request_id": "missing-request"},
        )

        for response in (tenant, wrong_hash, missing):
            self.assertEqual(response["status"], 404)
            self.assertEqual(
                response["json"]["error_code"],
                "submission_dispatch_http.submission_not_found",
            )

    def test_health_and_readiness_are_separate_and_stopped_is_not_ready(self):
        self.open()
        health = self.call(HTTP.HEALTH_PATH)
        readiness = self.call(HTTP.READINESS_PATH)
        self.assertEqual((health["status"], readiness["status"]), (200, 200))
        self.assertEqual(health["json"]["health"]["status"], "ok")
        self.assertEqual(readiness["json"]["readiness"]["status"], "ready")

        self.runtime.stop_worker()
        health = self.call(HTTP.HEALTH_PATH)
        readiness = self.call(HTTP.READINESS_PATH)
        self.assertEqual(health["status"], 200)
        self.assertEqual(readiness["status"], 503)
        self.assertEqual(
            readiness["json"]["readiness"]["worker_state"], "stopped"
        )

    def test_permission_drift_blocks_before_public_network_access(self):
        self.open()
        calls = len(self.support.transport.calls)
        os.chmod(self.database, 0o644)

        for path in (HTTP.CAPABILITIES_PATH, HTTP.HEALTH_PATH, HTTP.ENQUEUE_PATH):
            with self.subTest(path=path):
                response = (
                    self.enqueue() if path == HTTP.ENQUEUE_PATH
                    else self.call(path)
                )
                self.assertEqual(response["status"], 503)
        self.assertEqual(len(self.support.transport.calls), calls)

    def test_transport_framing_and_schema_are_fail_closed(self):
        self.open()
        cases = (
            (self.call(HTTP.CAPABILITIES_PATH, scheme="http"), 400),
            (self.call(HTTP.CAPABILITIES_PATH, query="private=1"), 400),
            (self.call(HTTP.CAPABILITIES_PATH, method="POST"), 405),
            (self.call("/missing"), 404),
            (self.call(HTTP.ENQUEUE_PATH, body=b"{}"), 400),
        )
        for response, expected in cases:
            self.assertEqual(response["status"], expected)
            self.assertEqual(response["json"]["status"], "BLOCK")
            self.assertEqual(response["headers"]["Cache-Control"], "no-store")

    def test_invalid_http_authenticator_creates_no_database(self):
        with self.assertRaises(
            RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ) as caught:
            RUNTIME.open_durable_cms_source_delivery_submission_dispatch(
                self.database,
                self.support.client,
                worker_id="worker",
                lease_seconds=60,
                http_authenticator="not-callable",
            )
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_runtime.configuration_invalid",
        )
        self.assertFalse(self.database.exists())
        self.assertEqual(self.support.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
