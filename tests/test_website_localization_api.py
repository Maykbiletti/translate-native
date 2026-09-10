from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import sqlite3
import sys
import unicodedata
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


API = load(
    "blun_test_website_localization_api",
    ROOT / "integrations" / "website_localization_api.py",
)
CMS = load(
    "blun_test_website_localization_api_cms",
    ROOT / "integrations" / "website_localization_cms.py",
)
QUEUE = CMS._QUEUE
RELEASE = CMS._RELEASE


class Authority:
    def __init__(self, key=b"test-event-key", accepted_key_ids=("event-key-1",)):
        self.key = key
        self.accepted_key_ids = accepted_key_ids

    def sign(self, value, *, key_id="event-key-1"):
        payload = CMS._canonical_json(value).encode("utf-8")
        return CMS.CMSMessageSignature(
            "hmac-sha256-test",
            key_id,
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return (
            signature.algorithm == "hmac-sha256-test"
            and signature.key_id in self.accepted_key_ids
            and hmac.compare_digest(signature.signature, expected)
        )


def event(**overrides):
    localization = {
        "source_id": "pricing.hero",
        "source_revision": "cms-1",
        "source_text": "Save up to €480 a year. All prices exclude VAT.",
        "source_locale": "en-IE",
        "content_type": "commercial",
        "glossary_version": "public-1",
        "policy_version": "commercial-1",
        "provider_id": "customer-llm",
        "model_id": "model",
        "model_version": "1",
        "software_version": "6.42.18",
        "target_locales": ["mt-MT", "fi-FI"],
    }
    value = {
        "schema": CMS.CHANGE_SCHEMA,
        "event_id": "event-1",
        "site_id": "site-1",
        "website_version": "web-1",
        "source_sequence": 1,
        "localization": localization,
    }
    for key, item in overrides.items():
        (localization if key in localization else value)[key] = item
    return value


def cancellation(value=None, **overrides):
    value = value or event()
    request = {
        "schema": CMS.CANCELLATION_SCHEMA,
        "cancellation_id": "cancel-1",
        "event_id": value["event_id"],
        "site_id": value["site_id"],
        "website_version": value["website_version"],
        "source_id": value["localization"]["source_id"],
        "source_sequence": value["source_sequence"],
    }
    request.update(overrides)
    return request


def tombstone(value=None, **overrides):
    value = value or event()
    request = {
        "schema": CMS.TOMBSTONE_SCHEMA,
        "tombstone_id": "tombstone-1",
        "event_id": value["event_id"],
        "site_id": value["site_id"],
        "website_version": value["website_version"],
        "source_id": value["localization"]["source_id"],
        "source_sequence": value["source_sequence"],
    }
    request.update(overrides)
    return request


class WebsiteLocalizationAPITests(unittest.TestCase):
    def setUp(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.release = RELEASE.LocalizationReleaseStore(
            self.release_connection, self.queue,
        )
        self.cms_connection = sqlite3.connect(":memory:")
        self.bridge = CMS.WebsiteLocalizationCMSBridge(
            self.cms_connection, self.queue, self.release,
        )
        self.authority = Authority()
        self.api = API.WebsiteLocalizationAPI(
            self.bridge,
            self.authority,
            clock=lambda: 100,
            approval_authority=self.authority,
            publication_authority=self.authority,
        )

    def tearDown(self):
        self.cms_connection.close()
        self.release_connection.close()
        self.queue_connection.close()

    def request(self, value=None, *, raw=None, signature=None, **environment):
        value = event() if value is None else value
        raw = (
            json.dumps(value, ensure_ascii=False).encode("utf-8")
            if raw is None else raw
        )
        signature = signature or self.authority.sign(value)
        environ = {
            "PATH_INFO": API.CHANGE_PATH,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json; charset=utf-8",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
            "HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM": signature.algorithm,
            "HTTP_X_LOCALIZATION_KEY_ID": signature.key_id,
            "HTTP_X_LOCALIZATION_SIGNATURE": signature.signature,
        }
        environ.update(environment)
        captured = {}
        result = b"".join(self.api(
            environ,
            lambda status, headers: captured.update(
                status=status, headers=headers,
            ),
        ))
        return captured["status"], dict(captured["headers"]), json.loads(result)

    def status_request(
        self,
        event_id="event-1",
        *,
        site_id="site-1",
        request_id="status-1",
        requested_at=100,
        authority=None,
        key_id="event-key-1",
    ):
        value = {
            "schema": API.STATUS_REQUEST_SCHEMA,
            "request_id": request_id,
            "event_id": event_id,
            "site_id": site_id,
            "requested_at": requested_at,
        }
        authority = authority or self.authority
        return self.request(
            value,
            signature=authority.sign(value, key_id=key_id),
            PATH_INFO=API.STATUS_PATH,
        )

    def lifecycle_request(
        self,
        event_id="event-1",
        *,
        site_id="site-1",
        request_id="lifecycle-1",
        requested_at=100,
        authority=None,
        key_id="event-key-1",
    ):
        value = {
            "schema": API.LIFECYCLE_REQUEST_SCHEMA,
            "request_id": request_id,
            "event_id": event_id,
            "site_id": site_id,
            "requested_at": requested_at,
        }
        authority = authority or self.authority
        return self.request(
            value,
            signature=authority.sign(value, key_id=key_id),
            PATH_INFO=API.LIFECYCLE_PATH,
        )

    def capabilities_request(
        self,
        *,
        request_id="capabilities-1",
        requested_at=100,
        authority=None,
        key_id="event-key-1",
    ):
        value = {
            "schema": API.CAPABILITIES_REQUEST_SCHEMA,
            "request_id": request_id,
            "requested_at": requested_at,
        }
        authority = authority or self.authority
        return self.request(
            value,
            signature=authority.sign(value, key_id=key_id),
            PATH_INFO=API.CAPABILITIES_PATH,
        )

    def cancellation_request(self, value=None, *, authority=None, key_id="event-key-1"):
        value = value or cancellation()
        authority = authority or self.authority
        return self.request(
            value,
            signature=authority.sign(value, key_id=key_id),
            PATH_INFO=API.CANCELLATION_PATH,
        )

    def tombstone_request(self, value=None, *, authority=None, key_id="event-key-1"):
        value = value or tombstone()
        authority = authority or self.authority
        return self.request(
            value,
            signature=authority.sign(value, key_id=key_id),
            PATH_INFO=API.TOMBSTONE_PATH,
        )

    def test_signed_v2_change_enqueues_each_locale_and_replays_idempotently(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first[0], "202 Accepted")
        self.assertEqual(second[0], "200 OK")
        self.assertEqual(first[2]["job_count"], 2)
        self.assertEqual(first[2]["inserted_jobs"], 2)
        self.assertEqual(second[2]["inserted_jobs"], 0)
        self.assertEqual(self.queue.plan_counts(first[2]["plan_id"])["pending"], 2)
        self.assertNotIn("source_text", json.dumps(first[2]))
        self.assertEqual(first[1]["Cache-Control"], "no-store")

    def test_signed_cancellation_is_idempotent_visible_and_stops_lifecycle(self):
        self.request()

        first = self.cancellation_request()
        replay = self.cancellation_request()
        progress = self.status_request(request_id="status-cancelled")
        lifecycle = self.lifecycle_request(request_id="lifecycle-cancelled")

        self.assertEqual(first[0], "202 Accepted")
        self.assertEqual(replay[0], "200 OK")
        self.assertTrue(first[2]["newly_cancelled"])
        self.assertFalse(replay[2]["newly_cancelled"])
        self.assertTrue(progress[2]["cancelled"])
        self.assertEqual(lifecycle[2]["status"], "cancelled")
        for payload in (first[2], replay[2], progress[2], lifecycle[2]):
            self.assertNotIn("source_text", json.dumps(payload))

    def test_tombstone_endpoint_is_signed_content_free_and_requires_publication(self):
        self.request()
        rejected = self.tombstone_request()
        self.assertEqual((rejected[0], rejected[2]["error"]), (
            "409 Conflict", "cms.tombstone.not_published",
        ))

        calls = []

        def accept(value, signature, verifier, authority, **options):
            calls.append((value, signature, verifier, authority, options))
            return CMS.TombstoneAccepted(
                value["tombstone_id"], value["event_id"],
                "blun-cms-tombstone-test", "pending", True,
            )

        self.bridge.request_tombstone = accept
        accepted = self.tombstone_request()
        self.assertEqual(accepted[0], "202 Accepted")
        self.assertEqual(accepted[2]["delivery_id"], "blun-cms-tombstone-test")
        self.assertNotIn("source_text", json.dumps(accepted[2]))
        self.assertEqual(len(calls), 1)

    def test_public_cancellation_recovers_the_prequeue_crash_gap(self):
        enqueue_plan = self.queue.enqueue_plan

        def fail_before_queue(*_args, **_kwargs):
            raise QUEUE.LocalizationQueueBlocked("simulated queue outage")

        self.queue.enqueue_plan = fail_before_queue
        try:
            failed = self.request()
        finally:
            self.queue.enqueue_plan = enqueue_plan
        self.assertEqual(failed[0], "503 Service Unavailable")

        cancelled = self.cancellation_request()
        replay = self.request()
        progress = self.status_request(request_id="status-crash-cancelled")
        lifecycle = self.lifecycle_request(request_id="lifecycle-crash-cancelled")

        self.assertEqual(cancelled[0], "202 Accepted")
        self.assertEqual((replay[0], replay[2]["status"]), ("200 OK", "cancelled"))
        self.assertEqual(progress[2]["counts"]["cancelled"], 2)
        self.assertEqual(lifecycle[2]["status"], "cancelled")
        self.assertEqual(
            self.queue_connection.execute(
                "SELECT COUNT(*) FROM localization_jobs"
            ).fetchone()[0],
            0,
        )

    def test_prequeue_crash_is_visible_without_exposing_or_mutating_content(self):
        enqueue_plan = self.queue.enqueue_plan

        def fail_before_queue(*_args, **_kwargs):
            raise QUEUE.LocalizationQueueBlocked("simulated queue outage")

        self.queue.enqueue_plan = fail_before_queue
        try:
            failed = self.request()
        finally:
            self.queue.enqueue_plan = enqueue_plan
        self.assertEqual(failed[0], "503 Service Unavailable")
        changes_before = (
            self.queue_connection.total_changes,
            self.release_connection.total_changes,
            self.cms_connection.total_changes,
        )

        progress = self.status_request(request_id="status-prequeue-recovery")
        lifecycle = self.lifecycle_request(
            request_id="lifecycle-prequeue-recovery",
        )

        self.assertEqual(progress[0], "200 OK")
        self.assertTrue(progress[2]["queue_recovery_pending"])
        self.assertEqual(sum(progress[2]["counts"].values()), 0)
        self.assertEqual(
            [item["status"] for item in progress[2]["locales"]],
            ["awaiting_queue_resume", "awaiting_queue_resume"],
        )
        self.assertEqual(lifecycle[0], "200 OK")
        self.assertEqual(lifecycle[2]["schema"], API.LIFECYCLE_RESPONSE_SCHEMA)
        self.assertEqual(lifecycle[2]["status"], "queue_recovery")
        self.assertEqual(lifecycle[2]["blocked_locales"], [
            ["fi-FI", "queue.awaiting_resume"],
            ["mt-MT", "queue.awaiting_resume"],
        ])
        rendered = json.dumps((progress[2], lifecycle[2]))
        self.assertNotIn("source_text", rendered)
        self.assertNotIn("Save up", rendered)
        self.assertEqual(changes_before, (
            self.queue_connection.total_changes,
            self.release_connection.total_changes,
            self.cms_connection.total_changes,
        ))

    def test_cancellation_rejects_wrong_binding_and_other_accepted_key(self):
        self.request()
        wrong = cancellation(website_version="web-other")
        status, _, payload = self.cancellation_request(wrong)
        self.assertEqual((status, payload["error"]), (
            "400 Bad Request", "cms.cancellation.binding_invalid",
        ))

        self.authority.accepted_key_ids = ("event-key-1", "event-key-2")
        status, _, payload = self.cancellation_request(key_id="event-key-2")
        self.assertEqual((status, payload["error"]), (
            "401 Unauthorized", "cms.cancellation.scope_rejected",
        ))
        self.assertEqual(
            self.cms_connection.execute(
                "SELECT COUNT(*) FROM cms_event_cancellations"
            ).fetchone()[0],
            0,
        )

    def test_default_policy_routes_all_remaining_eu_locales(self):
        value = event()
        del value["localization"]["target_locales"]
        status, _, payload = self.request(value)
        self.assertEqual(status, "202 Accepted")
        self.assertEqual(payload["job_count"], 23)
        self.assertEqual(payload["inserted_jobs"], 23)

    def test_signed_capabilities_expose_exact_current_locales_and_profile_bindings(self):
        changes_before = (
            self.queue_connection.total_changes,
            self.release_connection.total_changes,
            self.cms_connection.total_changes,
        )

        status, headers, payload = self.capabilities_request()

        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["status"], "CAPABILITIES")
        capabilities = payload["capabilities"]
        self.assertEqual(capabilities["schema"], CMS.CAPABILITIES_SCHEMA)
        self.assertEqual(
            capabilities["cancellation_schema"], CMS.CANCELLATION_SCHEMA,
        )
        self.assertEqual(capabilities["tombstone_schema"], CMS.TOMBSTONE_SCHEMA)
        self.assertEqual(
            capabilities["tombstone_delivery_schema"],
            CMS.TOMBSTONE_DELIVERY_SCHEMA,
        )
        publication_http = capabilities["publication_http"]
        self.assertEqual(
            publication_http["schema"], CMS.PUBLICATION_HTTP_CONTRACT_SCHEMA,
        )
        self.assertEqual(set(publication_http), {
            "schema", "method", "request_content_type", "response_content_types",
            "delivery_semantics", "binding_headers", "operations", "sha256",
        })
        unsigned_publication_http = dict(publication_http)
        publication_http_digest = unsigned_publication_http.pop("sha256")
        self.assertEqual(
            publication_http_digest,
            CMS._hash(CMS._canonical_json(unsigned_publication_http)),
        )
        publication_operations = {
            item["name"]: item for item in publication_http["operations"]
        }
        self.assertEqual(set(publication_operations), {"publication", "tombstone"})
        self.assertTrue(all(set(item) == {
            "name", "payload_schema", "request_schema",
            "acknowledgement_schema", "response_schema",
            "acknowledgement_status",
        } for item in publication_operations.values()))
        self.assertEqual(
            publication_operations["publication"]["request_schema"],
            CMS.PUBLICATION_HTTP_REQUEST_SCHEMA,
        )
        self.assertEqual(
            publication_operations["publication"]["payload_schema"],
            CMS.PUBLICATION_SCHEMA,
        )
        self.assertEqual(
            publication_operations["publication"]["acknowledgement_schema"],
            CMS.ACK_SCHEMA,
        )
        self.assertEqual(
            publication_operations["publication"]["acknowledgement_status"],
            "accepted",
        )
        self.assertEqual(
            publication_operations["tombstone"]["response_schema"],
            CMS.TOMBSTONE_HTTP_RESPONSE_SCHEMA,
        )
        self.assertEqual(
            publication_operations["tombstone"]["acknowledgement_status"],
            "deleted",
        )
        self.assertEqual(publication_http["delivery_semantics"], "at-least-once")
        self.assertEqual(publication_http["method"], "POST")
        self.assertEqual(
            publication_http["response_content_types"],
            ["application/json", "application/json; charset=utf-8"],
        )
        self.assertEqual(
            publication_http["binding_headers"],
            [
                {"name": name, "binding": binding}
                for name, binding in CMS.PUBLICATION_HTTP_BINDING_HEADERS
            ],
        )
        self.assertNotIn("endpoint", json.dumps(publication_http))
        self.assertNotIn("credential", json.dumps(publication_http))
        contract = payload["api_contract"]
        self.assertEqual(contract["schema"], API.API_CAPABILITIES_SCHEMA)
        unsigned_contract = dict(contract)
        digest = unsigned_contract.pop("sha256")
        self.assertEqual(digest, hashlib.sha256(
            API._canonical_json(unsigned_contract).encode("utf-8")
        ).hexdigest())
        operations = {
            item["name"]: item for item in contract["operations"]
        }
        self.assertEqual(set(operations), {
            "change", "cancellation", "tombstone", "status", "lifecycle",
            "capabilities",
        })
        self.assertTrue(all(
            item["method"] == "POST" for item in operations.values()
        ))
        self.assertEqual(
            operations["change"]["request_schema"], CMS.CHANGE_SCHEMA,
        )
        self.assertEqual(
            operations["status"]["request_schema"],
            API.STATUS_REQUEST_SCHEMA,
        )
        self.assertEqual(
            operations["lifecycle"]["response_schema"],
            API.LIFECYCLE_RESPONSE_SCHEMA,
        )
        self.assertTrue(operations["lifecycle"]["enabled"])
        self.assertTrue(operations["tombstone"]["enabled"])
        self.assertEqual(len(capabilities["locales"]), 24)
        self.assertEqual(
            [item["locale"] for item in capabilities["locales"]],
            [profile.locale for profile in CMS._PLANNER.EU_OFFICIAL_LOCALES],
        )
        native_names = {
            item["locale"]: item["native_name"]
            for item in capabilities["locales"]
        }
        self.assertEqual(native_names["cs-CZ"], "čeština")
        self.assertEqual(native_names["el-GR"], "ελληνικά")
        self.assertEqual(native_names["lv-LV"], "latviešu")
        self.assertTrue(all(
            unicodedata.is_normalized("NFC", name)
            for name in native_names.values()
        ))
        maltese = next(
            item for item in capabilities["locales"] if item["locale"] == "mt-MT"
        )
        self.assertEqual(maltese["native_name"], "Malti")
        self.assertEqual(
            maltese["quality_profile_sha256"],
            CMS._PLANNER.quality_profile_for("mt-MT")["sha256"],
        )
        claimed = capabilities.pop("sha256")
        self.assertEqual(claimed, CMS._hash(CMS._canonical_json(capabilities)))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("native_review_focus", json.dumps(payload))
        self.assertEqual(changes_before, (
            self.queue_connection.total_changes,
            self.release_connection.total_changes,
            self.cms_connection.total_changes,
        ))

    def test_capabilities_report_optional_routes_as_disabled(self):
        configured_api = self.api
        self.api = API.WebsiteLocalizationAPI(
            self.bridge, self.authority, clock=lambda: 100,
        )
        try:
            status, _, payload = self.capabilities_request(
                request_id="capabilities-standalone",
            )
        finally:
            self.api = configured_api

        operations = {
            item["name"]: item
            for item in payload["api_contract"]["operations"]
        }
        self.assertEqual(status, "200 OK")
        self.assertFalse(operations["lifecycle"]["enabled"])
        self.assertFalse(operations["tombstone"]["enabled"])
        self.assertTrue(operations["change"]["enabled"])
        self.assertTrue(operations["status"]["enabled"])

    def test_capabilities_are_purpose_bound_fresh_and_authenticated(self):
        status_request = {
            "schema": API.STATUS_REQUEST_SCHEMA,
            "request_id": "wrong-purpose",
            "event_id": "event-1",
            "site_id": "site-1",
            "requested_at": 100,
        }
        status, _, payload = self.request(
            status_request, PATH_INFO=API.CAPABILITIES_PATH,
        )
        self.assertEqual(
            (status, payload["error"]),
            ("400 Bad Request", "cms.capabilities.request_invalid"),
        )
        status, _, payload = self.capabilities_request(requested_at=1000)
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.capabilities.request_expired"),
        )
        value = {
            "schema": API.CAPABILITIES_REQUEST_SCHEMA,
            "request_id": "forged",
            "requested_at": 100,
        }
        forged = self.authority.sign(value)
        forged = CMS.CMSMessageSignature(forged.algorithm, forged.key_id, "0" * 64)
        status, _, payload = self.request(
            value, signature=forged, PATH_INFO=API.CAPABILITIES_PATH,
        )
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.capabilities.signature_rejected"),
        )

    def test_capabilities_block_an_inconsistent_locale_registry(self):
        original = CMS._PLANNER.EU_OFFICIAL_LOCALES
        CMS._PLANNER.EU_OFFICIAL_LOCALES = original[:-1] + (original[0],)
        try:
            status, _, payload = self.capabilities_request()
        finally:
            CMS._PLANNER.EU_OFFICIAL_LOCALES = original
        self.assertEqual(
            (status, payload["error"]),
            ("503 Service Unavailable", "cms.capabilities.registry_invalid"),
        )
        self.assertNotIn("locales", payload)

    def test_capabilities_block_an_incomplete_http_schema_registry(self):
        current = self.bridge.localization_capabilities

        def incomplete():
            value = current()
            del value["change_schema"]
            return value

        self.bridge.localization_capabilities = incomplete
        try:
            status, _, payload = self.capabilities_request(
                request_id="capabilities-incomplete-http",
            )
        finally:
            self.bridge.localization_capabilities = current

        self.assertEqual(
            (status, payload["error"]),
            ("503 Service Unavailable", "cms.capabilities.registry_invalid"),
        )
        self.assertNotIn("api_contract", payload)
        self.assertNotIn("capabilities", payload)

        original_headers = CMS.PUBLICATION_HTTP_BINDING_HEADERS
        CMS.PUBLICATION_HTTP_BINDING_HEADERS = (("Idempotency-Key", "other"),)
        try:
            status, _, payload = self.capabilities_request(
                request_id="capabilities-invalid-publication-headers",
            )
        finally:
            CMS.PUBLICATION_HTTP_BINDING_HEADERS = original_headers
        self.assertEqual(
            (status, payload["error"]),
            ("503 Service Unavailable", "cms.capabilities.registry_invalid"),
        )
        self.assertNotIn("capabilities", payload)

        original = CMS.PUBLICATION_HTTP_REQUEST_SCHEMA
        CMS.PUBLICATION_HTTP_REQUEST_SCHEMA = ""
        try:
            status, _, payload = self.capabilities_request(
                request_id="capabilities-invalid-publication-http",
            )
        finally:
            CMS.PUBLICATION_HTTP_REQUEST_SCHEMA = original
        self.assertEqual(
            (status, payload["error"]),
            ("503 Service Unavailable", "cms.capabilities.registry_invalid"),
        )
        self.assertNotIn("capabilities", payload)

    def test_signature_idempotency_and_source_sequence_collisions_fail_closed(self):
        value = event()
        forged = self.authority.sign(value)
        forged = CMS.CMSMessageSignature(forged.algorithm, forged.key_id, "0" * 64)
        status, _, payload = self.request(value, signature=forged)
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.event.signature_rejected"),
        )
        self.assertEqual(
            self.queue.connection.execute(
                "SELECT COUNT(*) FROM localization_jobs",
            ).fetchone()[0],
            0,
        )
        self.request(value)
        status, _, payload = self.request(event(source_revision="cms-2"))
        self.assertEqual(
            (status, payload["error"]),
            ("409 Conflict", "cms.event.idempotency_collision"),
        )
        collision = event(event_id="event-2", website_version="web-2")
        status, _, payload = self.request(collision)
        self.assertEqual(
            (status, payload["error"]),
            ("409 Conflict", "cms.event.sequence_collision"),
        )

    def test_transport_rejects_ambiguous_routing_types_and_framing(self):
        cases = [
            ({"wsgi.url_scheme": "http"}, "400 Bad Request", "api.https.required"),
            ({"PATH_INFO": "/wrong"}, "404 Not Found", "api.path.not_found"),
            ({"REQUEST_METHOD": "GET"}, "405 Method Not Allowed", "api.method.not_allowed"),
            ({"QUERY_STRING": "site=other"}, "400 Bad Request", "api.query.rejected"),
            ({"CONTENT_TYPE": "text/plain"}, "415 Unsupported Media Type", "api.content_type.invalid"),
            ({"CONTENT_TYPE": "application/json; profile=x"}, "415 Unsupported Media Type", "api.content_type.invalid"),
            ({"CONTENT_LENGTH": ""}, "411 Length Required", "api.content_length.required"),
            ({"CONTENT_LENGTH": "+10"}, "411 Length Required", "api.content_length.required"),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, "400 Bad Request", "api.transfer_encoding.rejected"),
        ]
        for environ, expected_status, code in cases:
            with self.subTest(code=code):
                status, _, payload = self.request(**environ)
                self.assertEqual((status, payload["error"]), (expected_status, code))

    def test_malformed_ambiguous_and_oversized_json_never_echoes_content(self):
        bad = [
            b'{"private-customer-text":',
            b'{"schema":1,"schema":2}',
            b'{"private-customer-text":NaN}',
            b"\xef\xbb\xbf{}",
            b"\xff",
            b"[" * 2000,
        ]
        for raw in bad:
            with self.subTest(raw=raw[:20]):
                status, _, payload = self.request(raw=raw)
                self.assertEqual(status, "400 Bad Request")
                self.assertNotIn("private-customer-text", json.dumps(payload))
        raw = b"x" * (API.MAX_MESSAGE_BYTES + 1)
        status, _, payload = self.request(raw=raw)
        self.assertEqual(
            (status, payload["error"]),
            ("413 Content Too Large", "api.body.too_large"),
        )

    def test_truncated_body_and_invalid_event_are_content_free(self):
        raw = json.dumps(event()).encode("utf-8")
        status, _, payload = self.request(
            raw=raw, CONTENT_LENGTH=str(len(raw) + 1),
        )
        self.assertEqual(
            (status, payload["error"]),
            ("400 Bad Request", "api.body.invalid"),
        )
        value = event(source_text="private-customer-text")
        value["localization"]["provider_id"] = ""
        status, _, payload = self.request(value)
        self.assertEqual(
            (status, payload["error"]),
            ("400 Bad Request", "cms.localization.invalid"),
        )
        self.assertNotIn("private-customer-text", json.dumps(payload))

    def test_signed_status_reports_each_locale_without_customer_text(self):
        self.request()
        status, headers, payload = self.status_request()
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["status"], "PROGRESS")
        self.assertFalse(payload["queue_recovery_pending"])
        self.assertEqual(payload["site_id"], "site-1")
        self.assertEqual(payload["source_sequence"], 1)
        self.assertEqual(payload["counts"]["pending"], 2)
        self.assertEqual(
            [item["target_locale"] for item in payload["locales"]],
            ["fi-FI", "mt-MT"],
        )
        self.assertNotIn("source_text", json.dumps(payload))
        self.assertNotIn("Save up", json.dumps(payload))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_prequeue_recovery_status_rejects_tampered_stored_event(self):
        enqueue_plan = self.queue.enqueue_plan

        def fail_before_queue(*_args, **_kwargs):
            raise QUEUE.LocalizationQueueBlocked("simulated queue outage")

        self.queue.enqueue_plan = fail_before_queue
        try:
            self.request()
        finally:
            self.queue.enqueue_plan = enqueue_plan
        self.cms_connection.execute(
            "UPDATE cms_change_events SET event_json = '{}'"
        )
        self.cms_connection.commit()

        status, _, payload = self.status_request(
            request_id="status-tampered-prequeue",
        )

        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error"], "cms.event.tampered")
        self.assertNotIn("Save up", json.dumps(payload))

    def test_signed_lifecycle_reports_processing_without_mutating_state(self):
        self.request()
        changes_before = (
            self.queue_connection.total_changes,
            self.release_connection.total_changes,
            self.cms_connection.total_changes,
        )

        status, headers, payload = self.lifecycle_request()

        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["schema"], API.LIFECYCLE_RESPONSE_SCHEMA)
        self.assertEqual(payload["status"], "processing")
        self.assertEqual(payload["required_locales"], ["fi-FI", "mt-MT"])
        self.assertEqual(payload["approved_locales"], [])
        self.assertEqual(
            payload["blocked_locales"],
            [["fi-FI", "approval.missing"], ["mt-MT", "approval.missing"]],
        )
        self.assertEqual(payload["queue_counts"]["pending"], 2)
        self.assertIsNone(payload["delivery"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("source_text", json.dumps(payload))
        self.assertNotIn("Save up", json.dumps(payload))
        self.assertEqual(changes_before, (
            self.queue_connection.total_changes,
            self.release_connection.total_changes,
            self.cms_connection.total_changes,
        ))

    def test_lifecycle_is_purpose_bound_fresh_and_tenant_scoped(self):
        self.request()
        wrong_schema = {
            "schema": API.STATUS_REQUEST_SCHEMA,
            "request_id": "lifecycle-purpose",
            "event_id": "event-1",
            "site_id": "site-1",
            "requested_at": 100,
        }
        status, _, payload = self.request(
            wrong_schema,
            PATH_INFO=API.LIFECYCLE_PATH,
        )
        self.assertEqual(
            (status, payload["error"]),
            ("400 Bad Request", "cms.lifecycle.request_invalid"),
        )
        status, _, payload = self.lifecycle_request(site_id="site-2")
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.lifecycle.scope_rejected"),
        )
        status, _, payload = self.lifecycle_request(requested_at=1000)
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.lifecycle.request_expired"),
        )

    def test_lifecycle_requires_release_authorities(self):
        self.request()
        self.api = API.WebsiteLocalizationAPI(
            self.bridge, self.authority, clock=lambda: 100,
        )
        status, _, payload = self.lifecycle_request()
        self.assertEqual(
            (status, payload["error"]),
            ("503 Service Unavailable", "cms.lifecycle.unavailable"),
        )

    def test_lifecycle_exposes_terminal_locale_failure_without_detail_prose(self):
        self.request()
        claim = self.queue.claim("worker", now=101, lease_seconds=10)
        self.queue.fail(
            claim,
            "provider.locale_unsupported",
            error_detail="private provider diagnostic",
            now=102,
        )
        self.api.clock = lambda: 102

        status, _, payload = self.lifecycle_request(
            request_id="lifecycle-failed", requested_at=102,
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["status"], "localization_failed")
        self.assertEqual(payload["queue_counts"]["failed"], 1)
        self.assertNotIn("private provider diagnostic", json.dumps(payload))

    def test_status_is_bound_to_exact_site_and_original_credential(self):
        self.request()
        status, _, payload = self.status_request(site_id="site-2")
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.status.scope_rejected"),
        )
        status, _, payload = self.status_request(event_id="unknown-event")
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.status.scope_rejected"),
        )
        multi = Authority(accepted_key_ids=("event-key-1", "event-key-2"))
        self.api.event_verifier = multi
        status, _, payload = self.status_request(
            authority=multi, key_id="event-key-2",
        )
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.status.scope_rejected"),
        )

    def test_status_requires_fresh_signature_and_exact_schema(self):
        self.request()
        status, _, payload = self.status_request(requested_at=1000)
        self.assertEqual(
            (status, payload["error"]),
            ("401 Unauthorized", "cms.status.request_expired"),
        )
        value = {
            "schema": API.STATUS_REQUEST_SCHEMA,
            "request_id": "status-1",
            "event_id": "event-1",
            "site_id": "site-1",
            "requested_at": 100,
            "extra": True,
        }
        status, _, payload = self.request(value, PATH_INFO=API.STATUS_PATH)
        self.assertEqual(
            (status, payload["error"]),
            ("400 Bad Request", "cms.status.request_invalid"),
        )

    def test_status_exposes_hashed_failure_and_expired_lease(self):
        self.request()
        claim = self.queue.claim("worker", now=101, lease_seconds=5)
        self.queue.fail(
            claim,
            "provider.timeout",
            error_detail="private provider response",
            now=102,
        )
        claim = self.queue.claim("worker", now=103, lease_seconds=5)
        self.api.clock = lambda: 110
        status, _, payload = self.status_request(
            request_id="status-2", requested_at=110,
        )
        self.assertEqual(status, "200 OK")
        by_locale = {item["target_locale"]: item for item in payload["locales"]}
        self.assertTrue(by_locale[claim.target_locale]["lease_expired"])
        other = next(
            item for item in payload["locales"]
            if item["target_locale"] != claim.target_locale
        )
        self.assertEqual(other["last_error_code"], "provider.timeout")
        self.assertIsNotNone(other["last_error_detail_hash"])
        self.assertNotIn("private provider response", json.dumps(payload))

    def test_superseded_event_status_blocks_and_delayed_change_stays_superseded(self):
        self.request()
        self.request(event(event_id="event-3", source_sequence=3, website_version="web-3"))
        status, _, payload = self.status_request()
        self.assertEqual(
            (status, payload["error"]),
            ("409 Conflict", "cms.event.superseded"),
        )
        delayed = event(
            event_id="event-2", source_sequence=2, website_version="web-2",
        )
        status, _, payload = self.request(delayed)
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["status"], "superseded")

    def test_tampered_queue_never_returns_partial_or_ready_status(self):
        self.request()
        self.queue.connection.execute(
            "UPDATE localization_plan_jobs SET plan_id = ? WHERE target_locale = ?",
            ("blun-l10n-plan-tampered", "fi-FI"),
        )
        status, _, payload = self.status_request(request_id="status-tampered")
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error"], "cms.queue.identity_lost")
        self.assertNotIn("locales", payload)

    def test_corrupted_successful_result_never_appears_successful(self):
        self.request()
        claim = self.queue.claim("worker", now=101, lease_seconds=10)
        self.queue.complete(claim, {"job_id": claim.job_id}, now=102)
        self.queue.connection.execute(
            "UPDATE localization_jobs SET result_sha256 = ? WHERE job_id = ?",
            ("0" * 64, claim.job_id),
        )
        status, _, payload = self.status_request(request_id="status-corrupt")
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error"], "cms.queue.integrity_failed")
        self.assertNotIn(claim.job_id, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
