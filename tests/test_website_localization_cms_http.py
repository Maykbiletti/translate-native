from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HTTP = load(
    "blun_test_website_localization_cms_http",
    ROOT / "integrations" / "website_localization_cms_http.py",
)
CMS = HTTP._CMS


class Authority:
    def __init__(self, key=b"cms-http-ack-key"):
        self.key = key

    def sign(self, payload):
        return CMS.CMSMessageSignature(
            "hmac-sha256-test",
            "cms-http-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return (
            signature.algorithm == "hmac-sha256-test"
            and signature.key_id == "cms-http-key-1"
            and hmac.compare_digest(signature.signature, expected)
        )


class Transport:
    def __init__(self):
        self.calls = []
        self.result = None
        self.error = None

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.result


def publication_request(authority=None):
    authority = authority or Authority(b"publication-key")
    target = "Aloita maksutta"
    payload = {
        "schema": CMS.PUBLICATION_SCHEMA,
        "delivery_id": "blun-cms-delivery-" + "a" * 64,
        "event_id": "cms-event-185",
        "site_id": "public-site",
        "website_version": "website-185",
        "plan_id": "blun-l10n-plan-" + "b" * 64,
        "source_id": "homepage.pricing",
        "source_revision": "cms-185",
        "source_sequence": 185,
        "source_sha256": "c" * 64,
        "localizations": [{
            "locale": "fi-FI",
            "target_text": target,
            "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
            "approval_id": "blun-l10n-approval-" + "d" * 64,
            "approval_expires_at": 2000,
        }],
    }
    payload_bytes = HTTP._canonical_json(
        payload,
        code="request_invalid",
        maximum=HTTP.MAX_REQUEST_BYTES,
    )
    return SimpleNamespace(
        delivery_id=payload["delivery_id"],
        payload=payload,
        payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        signature=authority.sign(payload_bytes),
    )


def tombstone_request(authority=None):
    authority = authority or Authority(b"publication-key")
    payload = {
        "schema": CMS.TOMBSTONE_DELIVERY_SCHEMA,
        "delivery_id": "blun-cms-tombstone-" + "a" * 64,
        "tombstone_id": "cms-tombstone-185",
        "event_id": "cms-event-185",
        "site_id": "public-site",
        "website_version": "website-185",
        "plan_id": "blun-l10n-plan-" + "b" * 64,
        "source_id": "homepage.pricing",
        "source_sequence": 185,
        "publication_delivery_id": "blun-cms-delivery-" + "d" * 64,
        "publication_payload_sha256": "e" * 64,
        "locales": ["fi-FI", "mt-MT"],
    }
    payload_bytes = HTTP._canonical_json(
        payload, code="request_invalid", maximum=HTTP.MAX_REQUEST_BYTES,
    )
    return SimpleNamespace(
        delivery_id=payload["delivery_id"], payload=payload,
        payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        signature=authority.sign(payload_bytes),
    )


def response_for(request, authority, **overrides):
    tombstone = request.payload["schema"] == CMS.TOMBSTONE_DELIVERY_SCHEMA
    acknowledgement = {
        "schema": CMS.TOMBSTONE_ACK_SCHEMA if tombstone else CMS.ACK_SCHEMA,
        "delivery_id": request.delivery_id,
        "payload_sha256": request.payload_sha256,
        "status": "deleted" if tombstone else "accepted",
    }
    acknowledgement.update(overrides)
    raw = HTTP._canonical_json(
        acknowledgement,
        code="acknowledgement_invalid",
        maximum=HTTP.MAX_RESPONSE_BYTES,
    )
    signature = authority.sign(raw)
    envelope = {
        "schema": HTTP.TOMBSTONE_RESPONSE_SCHEMA if tombstone else HTTP.RESPONSE_SCHEMA,
        "acknowledgement": acknowledgement,
        "signature": {
            "algorithm": signature.algorithm,
            "key_id": signature.key_id,
            "signature": signature.signature,
        },
    }
    body = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return HTTP.HTTPResult(
        200,
        (("Content-Type", "application/json; charset=utf-8"),
         ("Content-Length", str(len(body)))),
        body,
    )


class HTTPPublisherAdapterTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.ack_authority = Authority()
        self.request = publication_request()
        self.transport.result = response_for(self.request, self.ack_authority)
        self.adapter = HTTP.HTTPPublisherAdapter(
            "https://cms.example.test/localization/publications",
            lambda: {"Authorization": "Bearer deployment-secret"},
            self.ack_authority,
            transport=self.transport,
            timeout=12,
        )

    def failure(self, action):
        with self.assertRaises(HTTP.HTTPPublisherFailed) as caught:
            action()
        self.assertEqual(str(caught.exception), caught.exception.code)
        return caught.exception

    def test_posts_exact_signed_bundle_and_accepts_only_signed_bound_ack(self):
        acknowledgement = self.adapter.publish(self.request)
        self.assertEqual(acknowledgement["status"], "accepted")
        self.assertEqual(len(self.transport.calls), 1)
        url, headers, body, timeout = self.transport.calls[0]
        self.assertEqual(url, "https://cms.example.test/localization/publications")
        self.assertEqual(timeout, 12.0)
        self.assertEqual(headers["Authorization"], "Bearer deployment-secret")
        self.assertEqual(headers["Idempotency-Key"], self.request.delivery_id)
        self.assertEqual(headers["X-Localization-Payload-Sha256"], self.request.payload_sha256)
        self.assertEqual(headers["Accept-Encoding"], "identity")
        envelope = json.loads(body.decode("utf-8"))
        self.assertEqual(envelope["schema"], HTTP.REQUEST_SCHEMA)
        self.assertEqual(envelope["publication"], self.request.payload)
        self.assertEqual(envelope["payload_sha256"], self.request.payload_sha256)
        self.assertEqual(envelope["signature"]["key_id"], "cms-http-key-1")

    def test_posts_signed_tombstone_without_localized_content(self):
        request = tombstone_request()
        self.transport.result = response_for(request, self.ack_authority)

        acknowledgement = self.adapter.publish(request)

        self.assertEqual(acknowledgement["status"], "deleted")
        envelope = json.loads(self.transport.calls[0][2].decode("utf-8"))
        self.assertEqual(envelope["schema"], HTTP.TOMBSTONE_REQUEST_SCHEMA)
        self.assertEqual(envelope["tombstone"], request.payload)
        self.assertNotIn("target_text", json.dumps(envelope))

    def test_request_tampering_is_blocked_before_network(self):
        changed = SimpleNamespace(**vars(self.request))
        changed.payload = dict(self.request.payload, source_sequence=186)
        error = self.failure(lambda: self.adapter.publish(changed))
        self.assertEqual((error.code, error.retryable), ("request_binding", False))
        self.assertEqual(self.transport.calls, [])

        changed = SimpleNamespace(**vars(self.request))
        changed.delivery_id = "different-delivery"
        error = self.failure(lambda: self.adapter.publish(changed))
        self.assertEqual((error.code, error.retryable), ("request_invalid", False))
        self.assertEqual(self.transport.calls, [])

    def test_endpoint_and_authentication_are_strict_and_secret_free(self):
        invalid = (
            "http://cms.example.test/hook",
            "https://user:secret@cms.example.test/hook",
            "https://cms.example.test/hook?token=secret",
            "https://cms.example.test//other",
        )
        for endpoint in invalid:
            with self.assertRaises(ValueError):
                HTTP.HTTPPublisherAdapter(endpoint, lambda: {"Authorization": "x"}, self.ack_authority)
        HTTP.HTTPPublisherAdapter(
            "http://127.0.0.1:8080/hook",
            lambda: {"Authorization": "x"},
            self.ack_authority,
            transport=self.transport,
            allow_loopback_http=True,
        )
        for headers in (
            {},
            {"Content-Type": "text/plain"},
            {"Authorization": "Bearer secret\r\nX-Forged: yes"},
            {"Authorization": "Bearer sécret"},
            {"authorization": "one", "Authorization": "two"},
        ):
            adapter = HTTP.HTTPPublisherAdapter(
                "https://cms.example.test/hook",
                lambda headers=headers: headers,
                self.ack_authority,
                transport=self.transport,
            )
            error = self.failure(lambda adapter=adapter: adapter.publish(self.request))
            self.assertEqual((error.code, error.retryable), ("authentication", False))
            self.assertNotIn("secret", str(error))

    def test_http_statuses_and_network_failures_have_bounded_retry_policy(self):
        for status, retryable in ((302, False), (400, False), (408, True), (429, True), (503, True)):
            self.transport.result = HTTP.HTTPResult(status, (), b"")
            error = self.failure(lambda: self.adapter.publish(self.request))
            expected_code = "redirect" if status == 302 else "http_status"
            self.assertEqual((error.code, error.retryable), (expected_code, retryable))
        self.transport.error = RuntimeError("private transport detail")
        error = self.failure(lambda: self.adapter.publish(self.request))
        self.assertEqual((error.code, error.retryable), ("network", True))
        self.assertNotIn("private", str(error))

    def test_response_parser_rejects_bom_duplicates_size_and_wrong_type(self):
        good = response_for(self.request, self.ack_authority)
        variants = (
            HTTP.HTTPResult(200, (("Content-Type", "text/plain"),), good.body),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"),), b"\xef\xbb\xbf" + good.body),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"),), b'{"schema":1,"schema":2}'),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"),), b"x" * (HTTP.MAX_RESPONSE_BYTES + 1)),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"), ("Content-Length", "1")), good.body),
        )
        expected = (
            "response_content_type", "response_json", "response_json", "response_size", "response_size",
        )
        for result, code in zip(variants, expected):
            self.transport.result = result
            error = self.failure(lambda: self.adapter.publish(self.request))
            self.assertEqual((error.code, error.retryable), (code, True))

    def test_wrong_ack_binding_or_signature_is_terminal(self):
        self.transport.result = response_for(
            self.request,
            self.ack_authority,
            delivery_id="blun-cms-delivery-" + "e" * 64,
        )
        error = self.failure(lambda: self.adapter.publish(self.request))
        self.assertEqual((error.code, error.retryable), ("acknowledgement_binding", False))

        result = response_for(self.request, Authority(b"wrong-key"))
        self.transport.result = result
        error = self.failure(lambda: self.adapter.publish(self.request))
        self.assertEqual((error.code, error.retryable), ("acknowledgement_signature", False))

        envelope = json.loads(response_for(self.request, self.ack_authority).body)
        envelope["signature"]["extra"] = "not-allowed"
        body = json.dumps(envelope, separators=(",", ":")).encode()
        self.transport.result = HTTP.HTTPResult(
            200,
            (("Content-Type", "application/json"),),
            body,
        )
        error = self.failure(lambda: self.adapter.publish(self.request))
        self.assertEqual((error.code, error.retryable), ("acknowledgement_invalid", False))


if __name__ == "__main__":
    unittest.main()
