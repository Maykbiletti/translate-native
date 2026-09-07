from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integrations"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


API = load("blun_test_website_localization_api", ROOT / "integrations" / "website_localization_api.py")
CMS = sys.modules["website_localization_cms"]
QUEUE = CMS._QUEUE
RELEASE = CMS._RELEASE


class Authority:
    def __init__(self, key=b"test-event-key"):
        self.key = key

    def sign(self, event):
        payload = CMS._canonical_json(event).encode("utf-8")
        return CMS.CMSMessageSignature(
            "hmac-sha256-test", "event-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return (
            signature.algorithm == "hmac-sha256-test"
            and signature.key_id == "event-key-1"
            and hmac.compare_digest(signature.signature, expected)
        )


def event(**overrides):
    localization = {
        "source_id": "pricing.hero", "source_revision": "cms-1",
        "source_text": "Save up to €480 a year. All prices exclude VAT.",
        "source_locale": "en-IE", "content_type": "commercial",
        "glossary_version": "public-1", "policy_version": "commercial-1",
        "provider_id": "customer-llm", "model_id": "model",
        "model_version": "1", "software_version": "6.42.18",
        "target_locales": ["mt-MT", "fi-FI"],
    }
    value = {"schema": CMS.CHANGE_SCHEMA, "event_id": "event-1", "site_id": "site-1",
             "website_version": "web-1", "localization": localization}
    for key, item in overrides.items():
        (localization if key in localization else value)[key] = item
    return value


class WebsiteLocalizationAPITests(unittest.TestCase):
    def setUp(self):
        self.queue_connection = sqlite3.connect(":memory:")
        self.queue = QUEUE.LocalizationQueue(self.queue_connection)
        self.release_connection = sqlite3.connect(":memory:")
        self.release = RELEASE.LocalizationReleaseStore(self.release_connection, self.queue)
        self.cms_connection = sqlite3.connect(":memory:")
        self.bridge = CMS.WebsiteLocalizationCMSBridge(self.cms_connection, self.queue, self.release)
        self.authority = Authority()
        self.api = API.WebsiteLocalizationAPI(self.bridge, self.authority, clock=lambda: 100)

    def tearDown(self):
        self.cms_connection.close()
        self.release_connection.close()
        self.queue_connection.close()

    def request(self, value=None, *, raw=None, signature=None, **environment):
        value = event() if value is None else value
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8") if raw is None else raw
        signature = signature or self.authority.sign(value)
        environ = {
            "PATH_INFO": API.CHANGE_PATH, "REQUEST_METHOD": "POST", "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json; charset=utf-8", "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
            "HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM": signature.algorithm,
            "HTTP_X_LOCALIZATION_KEY_ID": signature.key_id,
            "HTTP_X_LOCALIZATION_SIGNATURE": signature.signature,
        }
        environ.update(environment)
        captured = {}
        result = b"".join(self.api(environ, lambda status, headers: captured.update(status=status, headers=headers)))
        return captured["status"], dict(captured["headers"]), json.loads(result)

    def test_signed_change_enqueues_one_job_per_locale_and_replay_is_idempotent(self):
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

    def test_default_policy_routes_all_remaining_eu_locales(self):
        value = event()
        del value["localization"]["target_locales"]
        status, _, payload = self.request(value)
        self.assertEqual(status, "202 Accepted")
        self.assertEqual(payload["job_count"], 23)
        self.assertEqual(payload["inserted_jobs"], 23)

    def test_signature_failure_and_idempotency_collision_do_not_enqueue_new_content(self):
        value = event()
        forged = self.authority.sign(value)
        forged = CMS.CMSMessageSignature(forged.algorithm, forged.key_id, "0" * 64)
        status, _, payload = self.request(value, signature=forged)
        self.assertEqual((status, payload["error"]), ("401 Unauthorized", "cms.event.signature_rejected"))
        self.assertEqual(self.queue.connection.execute("SELECT COUNT(*) FROM localization_jobs").fetchone()[0], 0)
        self.request(value)
        changed = event(source_revision="cms-2")
        status, _, payload = self.request(changed)
        self.assertEqual((status, payload["error"]), ("409 Conflict", "cms.event.idempotency_collision"))
        self.assertEqual(self.queue.connection.execute("SELECT COUNT(*) FROM localization_jobs").fetchone()[0], 2)

    def test_transport_rejects_non_https_wrong_routes_methods_types_and_framing(self):
        cases = [
            ({"wsgi.url_scheme": "http"}, "400 Bad Request", "api.https.required"),
            ({"PATH_INFO": "/wrong"}, "404 Not Found", "api.path.not_found"),
            ({"REQUEST_METHOD": "GET"}, "405 Method Not Allowed", "api.method.not_allowed"),
            ({"CONTENT_TYPE": "text/plain"}, "415 Unsupported Media Type", "api.content_type.invalid"),
            ({"CONTENT_LENGTH": ""}, "411 Length Required", "api.content_length.required"),
            ({"CONTENT_LENGTH": "+10"}, "411 Length Required", "api.content_length.required"),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, "400 Bad Request", "api.transfer_encoding.rejected"),
        ]
        for env, expected_status, code in cases:
            with self.subTest(code=code):
                status, _, payload = self.request(**env)
                self.assertEqual((status, payload["error"]), (expected_status, code))

    def test_malformed_ambiguous_and_oversized_json_fail_closed_without_echo(self):
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
        raw = b"x" * (CMS.MAX_MESSAGE_BYTES + 1)
        status, _, payload = self.request(raw=raw)
        self.assertEqual((status, payload["error"]), ("413 Content Too Large", "api.body.too_large"))

    def test_truncated_body_is_rejected_before_signature_check(self):
        raw = json.dumps(event()).encode()
        status, _, payload = self.request(raw=raw, CONTENT_LENGTH=str(len(raw) + 1))
        self.assertEqual((status, payload["error"]), ("400 Bad Request", "api.body.invalid"))

    def test_invalid_event_returns_only_stable_code(self):
        value = event(source_text="private-customer-text")
        value["localization"]["provider_id"] = ""
        status, _, payload = self.request(value)
        self.assertEqual((status, payload["error"]), ("400 Bad Request", "cms.localization.invalid"))
        self.assertNotIn("private-customer-text", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
