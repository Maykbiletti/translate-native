from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLIENT = load(
    "blun_test_website_localization_cms_client",
    ROOT / "integrations" / "website_localization_cms_client.py",
)
API = load(
    "blun_test_website_localization_cms_client_api",
    ROOT / "integrations" / "website_localization_api.py",
)
CMS = load(
    "blun_test_website_localization_cms_client_cms",
    ROOT / "integrations" / "website_localization_cms.py",
)


class Authority:
    def __init__(self, key=b"cms-client-key"):
        self.key = key
        self.signed = []

    def sign(self, payload):
        self.signed.append(payload)
        return SimpleNamespace(
            algorithm="hmac-sha256-test",
            key_id="cms-client-key-1",
            signature=hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        return (
            signature.algorithm == "hmac-sha256-test"
            and signature.key_id == "cms-client-key-1"
            and hmac.compare_digest(
                signature.signature,
                hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
            )
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
        "target_locales": ["fi-FI", "mt-MT"],
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


def cancellation(value=None):
    value = event() if value is None else value
    return {
        "schema": CMS.CANCELLATION_SCHEMA,
        "cancellation_id": "cancel-1",
        "event_id": value["event_id"],
        "site_id": value["site_id"],
        "website_version": value["website_version"],
        "source_id": value["localization"]["source_id"],
        "source_sequence": value["source_sequence"],
    }


def tombstone(value=None):
    value = event() if value is None else value
    return {
        "schema": CMS.TOMBSTONE_SCHEMA,
        "tombstone_id": "tombstone-1",
        "event_id": value["event_id"],
        "site_id": value["site_id"],
        "website_version": value["website_version"],
        "source_id": value["localization"]["source_id"],
        "source_sequence": value["source_sequence"],
    }


class WSGITransport:
    def __init__(self, application):
        self.application = application
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        parsed = urlsplit(url)
        environ = {
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": parsed.scheme,
            "CONTENT_TYPE": headers["Content-Type"],
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        for name, value in headers.items():
            normalized = name.upper().replace("-", "_")
            if normalized not in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                environ["HTTP_" + normalized] = value
        captured = {}
        response_body = b"".join(self.application(
            environ,
            lambda status, response_headers: captured.update(
                status=status, headers=tuple(response_headers),
            ),
        ))
        return CLIENT.HTTPResult(
            int(captured["status"].split(" ", 1)[0]),
            captured["headers"],
            response_body,
        )


class TransformingTransport:
    def __init__(self, transport, transform):
        self.transport = transport
        self.transform = transform
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        return self.transform(
            self.transport.post(url, headers, body, timeout=timeout)
        )


def replace_json(result, transform):
    value = json.loads(result.body)
    transform(value)
    body = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = tuple(
        (name, str(len(body)) if name.lower() == "content-length" else content)
        for name, content in result.headers
    )
    return CLIENT.HTTPResult(result.status, headers, body)


class CMSLocalizationHTTPClientTests(unittest.TestCase):
    def setUp(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = CMS._QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.release = CMS._RELEASE.LocalizationReleaseStore(
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
        self.transport = WSGITransport(self.api)
        ids = iter((
            "capabilities-1", "status-1", "lifecycle-1",
            "unused-1", "unused-2",
        ))
        self.client = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test",
            lambda: {"Authorization": "Bearer host-token"},
            self.authority,
            transport=self.transport,
            clock=lambda: 100,
            request_id_factory=lambda: next(ids),
        )

    def tearDown(self):
        self.cms_connection.close()
        self.release_connection.close()
        self.queue_connection.close()

    def test_all_six_operations_cross_the_real_signed_api_boundary(self):
        source = event()
        capabilities = self.client.capabilities()
        accepted = self.client.submit_change(source)
        progress = self.client.status(source["event_id"], source["site_id"])
        lifecycle = self.client.lifecycle(source["event_id"], source["site_id"])
        cancelled = self.client.cancel(cancellation(source))

        calls = []

        def accept_tombstone(value, signature, verifier, authority, **options):
            calls.append((value, signature, verifier, authority, options))
            return CMS.TombstoneAccepted(
                value["tombstone_id"], value["event_id"],
                "blun-cms-tombstone-client", "pending", True,
            )

        self.bridge.request_tombstone = accept_tombstone
        deletion = self.client.request_tombstone(tombstone(source))

        self.assertEqual(capabilities["status"], "CAPABILITIES")
        self.assertEqual(accepted["status"], "enqueued")
        self.assertEqual(progress["status"], "PROGRESS")
        self.assertEqual(lifecycle["status"], "processing")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(deletion["status"], "pending")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [urlsplit(item[0]).path for item in self.transport.calls],
            [
                API.CAPABILITIES_PATH, API.CHANGE_PATH, API.STATUS_PATH,
                API.LIFECYCLE_PATH, API.CANCELLATION_PATH, API.TOMBSTONE_PATH,
            ],
        )
        rendered = json.dumps(
            (capabilities, accepted, progress, lifecycle, cancelled, deletion),
            ensure_ascii=False,
        )
        self.assertNotIn(source["localization"]["source_text"], rendered)

        cancelled_progress = self.client.status(
            source["event_id"], source["site_id"], request_id="cancelled-status-1",
        )
        self.assertTrue(cancelled_progress["cancelled"])

    def test_requests_are_canonical_native_unicode_signed_and_never_mutated(self):
        source = event()
        original = copy.deepcopy(source)

        self.client.submit_change(source)

        _, headers, body, timeout = self.transport.calls[0]
        self.assertEqual(source, original)
        self.assertIn("€480".encode("utf-8"), body)
        self.assertNotIn(b"\\u20ac", body)
        self.assertEqual(self.authority.signed[-1], body)
        self.assertEqual(headers["Authorization"], "Bearer host-token")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(headers["X-Localization-Key-Id"], "cms-client-key-1")
        self.assertEqual(timeout, 30.0)

    def test_server_error_is_content_free_and_has_transport_retryability(self):
        with self.assertRaises(CLIENT.CMSClientFailed) as caught:
            self.client.status("missing-event", "site-1", request_id="missing-1")

        self.assertEqual(caught.exception.code, "cms.status.scope_rejected")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("missing-event", str(caught.exception))

        broken_api = API.WebsiteLocalizationAPI(
            self.bridge,
            self.authority,
            clock=lambda: (_ for _ in ()).throw(RuntimeError("private clock")),
            approval_authority=self.authority,
            publication_authority=self.authority,
        )
        client = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, self.authority,
            transport=WSGITransport(broken_api), clock=lambda: 100,
        )
        with self.assertRaises(CLIENT.CMSClientFailed) as caught:
            client.capabilities(request_id="clock-failure-1")
        self.assertEqual(caught.exception.code, "api.internal")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private", str(caught.exception))

    def test_prequeue_recovery_progress_remains_observable(self):
        enqueue_plan = self.queue.enqueue_plan

        def fail_enqueue(*_args, **_kwargs):
            raise CMS._QUEUE.LocalizationQueueBlocked("private queue failure")

        self.queue.enqueue_plan = fail_enqueue
        try:
            with self.assertRaises(CLIENT.CMSClientFailed) as caught:
                self.client.submit_change(event())
        finally:
            self.queue.enqueue_plan = enqueue_plan

        self.assertEqual(caught.exception.code, "cms.queue.rejected")
        self.assertTrue(caught.exception.retryable)
        progress = self.client.status(
            "event-1", "site-1", request_id="prequeue-status-1",
        )
        lifecycle = self.client.lifecycle(
            "event-1", "site-1", request_id="prequeue-lifecycle-1",
        )
        self.assertTrue(progress["queue_recovery_pending"])
        self.assertEqual(sum(progress["counts"].values()), 0)
        self.assertEqual(
            {item["status"] for item in progress["locales"]},
            {"awaiting_queue_resume"},
        )
        self.assertEqual(lifecycle["status"], "queue_recovery")

    def test_response_binding_rejects_another_event(self):
        transport = TransformingTransport(
            self.transport,
            lambda result: replace_json(
                result, lambda value: value.update(event_id="event-other"),
            ),
        )
        client = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, self.authority,
            transport=transport, clock=lambda: 100,
        )

        with self.assertRaises(CLIENT.CMSClientFailed) as caught:
            client.submit_change(event())
        self.assertEqual(caught.exception.code, "response_binding")
        self.assertTrue(caught.exception.retryable)

        malformed = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, self.authority,
            transport=TransformingTransport(
                self.transport,
                lambda result: replace_json(
                    result, lambda value: value.update(required_locales=[[]]),
                ),
            ),
            clock=lambda: 100,
        )
        with self.assertRaises(CLIENT.CMSClientFailed) as malformed_error:
            malformed.lifecycle(
                "event-1", "site-1", request_id="malformed-lifecycle-1",
            )
        self.assertEqual(malformed_error.exception.code, "response_binding")

    def test_capability_hash_and_operation_drift_both_block(self):
        def corrupt_without_rehash(result):
            return replace_json(
                result,
                lambda value: value["api_contract"]["operations"][0].update(
                    path="/wrong",
                ),
            )

        first = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, self.authority,
            transport=TransformingTransport(self.transport, corrupt_without_rehash),
            clock=lambda: 100,
        )
        with self.assertRaises(CLIENT.CMSClientFailed):
            first.capabilities(request_id="capability-hash-drift")

        def corrupt_and_rehash(result):
            def transform(value):
                contract = value["api_contract"]
                contract["operations"][0]["path"] = "/wrong"
                unsigned = dict(contract)
                unsigned.pop("sha256")
                contract["sha256"] = hashlib.sha256(
                    json.dumps(
                        unsigned, ensure_ascii=False, allow_nan=False,
                        sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            return replace_json(result, transform)

        second = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, self.authority,
            transport=TransformingTransport(self.transport, corrupt_and_rehash),
            clock=lambda: 100,
        )
        with self.assertRaises(CLIENT.CMSClientFailed):
            second.capabilities(request_id="capability-contract-drift")

    def test_network_failure_is_one_retryable_attempt(self):
        class FailedTransport:
            def __init__(self):
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                raise RuntimeError("private network detail")

        transport = FailedTransport()
        client = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, self.authority,
            transport=transport, clock=lambda: 100,
        )

        with self.assertRaises(CLIENT.CMSClientFailed) as caught:
            client.capabilities(request_id="network-failure-1")
        self.assertEqual((caught.exception.code, caught.exception.retryable), (
            "network", True,
        ))
        self.assertEqual(transport.calls, 1)
        self.assertNotIn("private", str(caught.exception))

    def test_redirect_and_reserved_authentication_headers_fail_closed(self):
        class RedirectTransport:
            def post(self, *_args, **_kwargs):
                return CLIENT.HTTPResult(302, (), b"")

        redirected = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test", lambda: {}, self.authority,
            transport=RedirectTransport(), clock=lambda: 100,
        )
        with self.assertRaises(CLIENT.CMSClientFailed) as caught:
            redirected.capabilities(request_id="redirect-1")
        self.assertEqual((caught.exception.code, caught.exception.retryable), (
            "redirect", False,
        ))

        reserved = CLIENT.CMSLocalizationHTTPClient(
            "https://localization.example.test",
            lambda: {"X-Localization-Signature": "forged"},
            self.authority,
            transport=self.transport,
            clock=lambda: 100,
        )
        before = len(self.transport.calls)
        with self.assertRaises(CLIENT.CMSClientFailed) as caught:
            reserved.capabilities(request_id="reserved-header-1")
        self.assertEqual(caught.exception.code, "authentication")
        self.assertEqual(len(self.transport.calls), before)

    def test_invalid_origins_are_rejected_before_transport(self):
        for origin in (
            "http://localization.example.test",
            "https://user:secret@localization.example.test",
            "https://localization.example.test/base",
            "https://localization.example.test?debug=1",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                CLIENT.CMSLocalizationHTTPClient(
                    origin, lambda: {}, self.authority, transport=self.transport,
                )


if __name__ == "__main__":
    unittest.main()
