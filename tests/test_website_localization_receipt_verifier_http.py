from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HTTP = load(
    "blun_test_website_localization_receipt_verifier_http",
    ROOT / "integrations" / "website_localization_receipt_verifier_http.py",
)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def binding(*, kind="quality"):
    source = "Build a calmer workday."
    target = "Rakenna rauhallisempi työpäivä."
    return {
        "schema": HTTP.RECEIPT_BINDING_SCHEMA,
        "review_kind": kind,
        "job_id": "job-finnish-1",
        "result_sha256": "1" * 64,
        "source_text": source,
        "target_text": target,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
        "source_locale": "en-IE",
        "target_locale": "fi-FI",
        "content_type": "marketing",
        "glossary_version": "public-glossary-3",
        "policy_version": "native-web-1",
        "primary_provider": {
            "id": "customer-model",
            "model_id": "configured-model",
            "model_version": "2026-09-09",
        },
        "review_provider": None,
        "software_version": "6.43.0-dev",
        "review_confidence": {
            "target_native": "high",
            "source_fidelity": "high",
        },
        "quality_profile": {
            "locale": "fi-FI",
            "version": "eu-fi-FI-quality-v1",
            "sha256": "2" * 64,
        },
        "commercial_profile": None,
        "commercial_review": None,
        "human_review_required": False,
        "independent_review_required": False,
    }


class Transport:
    def __init__(self, responder=None, error=None):
        self.responder = responder
        self.error = error
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.responder(self.calls[-1])


def response_for(call, *, verified=True, **changes):
    _, _, body, _ = call
    request = json.loads(body)
    payload = {
        "schema": HTTP.RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "binding_sha256": request["binding_sha256"],
        "receipt_sha256": request["receipt_sha256"],
        "verified": verified,
    }
    payload.update(changes)
    body_out = canonical(payload)
    return HTTP.HTTPResult(
        200,
        (("Content-Type", "application/json; charset=utf-8"),
         ("Content-Length", str(len(body_out)))),
        body_out,
    )


class HTTPReceiptVerifierTests(unittest.TestCase):
    def adapter(self, transport):
        return HTTP.HTTPReceiptVerifierAdapter(
            "https://quality.example/v1/receipts/verify",
            lambda: {"Authorization": "Bearer host-secret"},
            transport=transport,
            timeout=12,
        )

    def test_sends_one_exact_bound_native_unicode_request(self):
        transport = Transport(lambda call: response_for(call))
        verifier = self.adapter(transport)
        self.assertTrue(verifier.verify(binding=binding(), receipt="signed-receipt"))
        self.assertEqual(len(transport.calls), 1)
        url, headers, body, timeout = transport.calls[0]
        request = json.loads(body)
        self.assertEqual(url, "https://quality.example/v1/receipts/verify")
        self.assertEqual(timeout, 12.0)
        self.assertEqual(request["schema"], HTTP.REQUEST_SCHEMA)
        self.assertEqual(request["binding"]["target_text"], "Rakenna rauhallisempi työpäivä.")
        self.assertEqual(
            request["binding_sha256"],
            hashlib.sha256(canonical(request["binding"])).hexdigest(),
        )
        self.assertEqual(
            request["receipt_sha256"],
            hashlib.sha256(b"signed-receipt").hexdigest(),
        )
        self.assertEqual(headers["Idempotency-Key"], request["request_id"])
        self.assertEqual(
            headers["X-Localization-Receipt-Request-Sha256"],
            hashlib.sha256(body).hexdigest(),
        )
        self.assertEqual(headers["Authorization"], "Bearer host-secret")

    def test_negative_verdict_is_false_not_transport_failure(self):
        transport = Transport(lambda call: response_for(call, verified=False))
        self.assertFalse(self.adapter(transport).verify(
            binding=binding(), receipt="rejected-receipt",
        ))
        self.assertEqual(len(transport.calls), 1)

    def test_changed_binding_or_receipt_has_a_distinct_idempotency_key(self):
        transport = Transport(lambda call: response_for(call))
        verifier = self.adapter(transport)
        verifier.verify(binding=binding(), receipt="receipt-one")
        changed = binding()
        changed["policy_version"] = "native-web-2"
        verifier.verify(binding=changed, receipt="receipt-one")
        verifier.verify(binding=changed, receipt="receipt-two")
        ids = [call[1]["Idempotency-Key"] for call in transport.calls]
        self.assertEqual(len(set(ids)), 3)

    def test_malformed_binding_blocks_before_authentication_or_transport(self):
        auth_calls = []
        transport = Transport(lambda call: response_for(call))
        verifier = HTTP.HTTPReceiptVerifierAdapter(
            "https://quality.example/v1/receipts/verify",
            lambda: auth_calls.append(True) or {"Authorization": "secret"},
            transport=transport,
        )
        mutations = []
        changed_text = binding()
        changed_text["target_text"] += " Muutos."
        mutations.append(changed_text)
        missing = binding()
        del missing["policy_version"]
        mutations.append(missing)
        wrong_profile = binding()
        wrong_profile["quality_profile"]["locale"] = "sv-SE"
        mutations.append(wrong_profile)
        same_provider = binding(kind="independent_model")
        same_provider["review_provider"] = copy.deepcopy(same_provider["primary_provider"])
        mutations.append(same_provider)
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(HTTP.HTTPReceiptVerifierFailed) as caught:
                    verifier.verify(binding=value, receipt="signed-receipt")
                self.assertEqual(caught.exception.code, "binding_invalid")
                self.assertFalse(caught.exception.retryable)
        self.assertEqual(auth_calls, [])
        self.assertEqual(transport.calls, [])

    def test_endpoint_and_authentication_are_fail_closed(self):
        for endpoint in (
            "http://quality.example/v1/verify",
            "https://user:secret@quality.example/v1/verify",
            "https://quality.example/v1/verify?next=other",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    HTTP.HTTPReceiptVerifierAdapter(endpoint, lambda: {"X-Key": "x"})
        transport = Transport(lambda call: response_for(call))
        verifier = HTTP.HTTPReceiptVerifierAdapter(
            "https://quality.example/v1/verify",
            lambda: {"Content-Type": "text/plain"},
            transport=transport,
        )
        with self.assertRaises(HTTP.HTTPReceiptVerifierFailed) as caught:
            verifier.verify(binding=binding(), receipt="signed-receipt")
        self.assertEqual(caught.exception.code, "authentication")
        self.assertEqual(transport.calls, [])

    def test_network_and_retryable_http_failures_make_one_attempt(self):
        cases = (
            (Transport(error=OSError("private network detail")), "network"),
            (Transport(lambda call: HTTP.HTTPResult(503, (), b"private")), "http_status"),
            (Transport(lambda call: HTTP.HTTPResult(429, (), b"private")), "http_status"),
        )
        for transport, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(HTTP.HTTPReceiptVerifierFailed) as caught:
                    self.adapter(transport).verify(
                        binding=binding(), receipt="signed-receipt",
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(transport.calls), 1)

    def test_redirect_and_terminal_http_statuses_do_not_retry(self):
        for status in (302, 400, 401, 403, 404):
            transport = Transport(lambda call, status=status: HTTP.HTTPResult(
                status, (("Location", "https://evil.example"),), b"private",
            ))
            with self.subTest(status=status):
                with self.assertRaises(HTTP.HTTPReceiptVerifierFailed) as caught:
                    self.adapter(transport).verify(
                        binding=binding(), receipt="signed-receipt",
                    )
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(transport.calls), 1)

    def test_wrong_response_binding_and_ambiguous_json_fail_closed(self):
        responders = (
            lambda call: response_for(call, request_id="blun-l10n-receipt-" + "0" * 64),
            lambda call: response_for(call, binding_sha256="0" * 64),
            lambda call: response_for(call, receipt_sha256="0" * 64),
            lambda call: HTTP.HTTPResult(
                200,
                (("Content-Type", "application/json"),),
                b'{"schema":"x","schema":"y"}',
            ),
            lambda call: HTTP.HTTPResult(
                200,
                (("Content-Type", "application/json"),),
                b"\xef\xbb\xbf{}",
            ),
        )
        for index, responder in enumerate(responders):
            with self.subTest(index=index):
                transport = Transport(responder)
                with self.assertRaises(HTTP.HTTPReceiptVerifierFailed):
                    self.adapter(transport).verify(
                        binding=binding(), receipt="signed-receipt",
                    )
                self.assertEqual(len(transport.calls), 1)

    def test_commercial_review_scope_is_bound_before_transport(self):
        value = binding()
        value["content_type"] = "commercial"
        value["commercial_profile"] = "translate-native.commercial.v3"
        value["quality_profile"]["commercial"] = {
            "profile": value["commercial_profile"],
            "version": "commercial-eu-fi-FI-2026-09-1",
            "sha256": "3" * 64,
        }
        value["commercial_review"] = {
            "schema": HTTP.COMMERCIAL_REVIEW_SUMMARY_SCHEMA,
            "profile": value["commercial_profile"],
            "status": "review_required",
            "review_required_dimensions": ["cancellation"],
            "evidence_sha256": "d" * 64,
        }
        value["independent_review_required"] = True
        transport = Transport(response_for)
        self.adapter(transport).verify(binding=value, receipt="signed-receipt")
        sent = json.loads(transport.calls[0][2])["binding"]
        self.assertEqual(
            sent["commercial_review"]["review_required_dimensions"],
            ["cancellation"],
        )
        self.assertEqual(
            sent["quality_profile"]["commercial"],
            value["quality_profile"]["commercial"],
        )

        value["commercial_review"]["review_required_dimensions"] = [
            "private cancellation text",
        ]
        invalid_transport = Transport(response_for)
        with self.assertRaises(HTTP.HTTPReceiptVerifierFailed) as caught:
            self.adapter(invalid_transport).verify(
                binding=value, receipt="signed-receipt",
            )
        self.assertEqual(caught.exception.code, "binding_invalid")
        self.assertEqual(invalid_transport.calls, [])

        for mutate in (
            lambda payload: payload["quality_profile"].pop("commercial"),
            lambda payload: payload["quality_profile"]["commercial"].update(
                profile="another-commercial-profile"
            ),
            lambda payload: payload.update(content_type="marketing"),
        ):
            changed = json.loads(json.dumps(value))
            changed["commercial_review"]["review_required_dimensions"] = [
                "cancellation"
            ]
            mutate(changed)
            invalid_transport = Transport(response_for)
            with self.subTest(mutate=mutate), self.assertRaises(
                HTTP.HTTPReceiptVerifierFailed,
            ) as caught:
                self.adapter(invalid_transport).verify(
                    binding=changed, receipt="signed-receipt",
                )
            self.assertEqual(caught.exception.code, "binding_invalid")
            self.assertEqual(invalid_transport.calls, [])

        noncommercial = binding()
        noncommercial["quality_profile"]["commercial"] = value[
            "quality_profile"
        ]["commercial"]
        invalid_transport = Transport(response_for)
        with self.assertRaises(HTTP.HTTPReceiptVerifierFailed):
            self.adapter(invalid_transport).verify(
                binding=noncommercial, receipt="signed-receipt",
            )
        self.assertEqual(invalid_transport.calls, [])


if __name__ == "__main__":
    unittest.main()
