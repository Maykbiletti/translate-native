from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from test_website_localization_worker import WORKER, assets, candidate, job, review


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HTTP = load(
    "blun_test_website_localization_http_provider",
    ROOT / "integrations" / "website_localization_http_provider.py",
)
REQUEST_ID_VALUE = "blun-l10n-call-" + "a" * 64


class FakeTransport:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if isinstance(self.responder, Exception):
            raise self.responder
        return self.responder(body)


def response_for(body: bytes, *, mutate=None, response=None):
    request_envelope = json.loads(body.decode("utf-8"))
    phase = request_envelope["request"]["phase"]
    if response is None:
        response = candidate() if phase == "transcreation" else review(phase)
    envelope = {
        "schema": HTTP.RESPONSE_SCHEMA,
        "request_id": request_envelope["request_id"],
        "request_sha256": request_envelope["request_sha256"],
        "response": response,
    }
    if mutate is not None:
        mutate(envelope)
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return HTTP.HTTPResult(
        200,
        (("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(raw)))),
        raw,
    )


def adapter(transport, authentication_headers=None, **overrides):
    return HTTP.HTTPProviderAdapter(
        "https://models.example.test/v1/localize",
        authentication_headers or (lambda: {"Authorization": "Bearer private-token"}),
        transport=transport,
        **overrides,
    )


def provider_request():
    return WORKER.ProviderRequest(
        schema=WORKER.WORKER_SCHEMA,
        request_id=REQUEST_ID_VALUE,
        phase="transcreation",
        provider_id="customer-llm",
        model_id="king",
        model_version="2026-08-29",
        system_instruction="Return only the response object.",
        input={"source": {"text": "Build your business with BLUN."}, "target": {"locale": "sv-SE"}},
    )


class WebsiteLocalizationHTTPProviderTests(unittest.TestCase):
    def test_runs_the_real_three_phase_worker_over_separate_bound_requests(self):
        transport = FakeTransport(response_for)
        result = WORKER.run_localization_job(job(), assets(), adapter(transport))

        self.assertEqual(result["candidate"], "Bygg ditt företag med BLUN.")
        self.assertEqual(len(transport.calls), 3)
        envelopes = [json.loads(call[2].decode("utf-8")) for call in transport.calls]
        self.assertEqual(
            [item["request"]["phase"] for item in envelopes],
            ["transcreation", "target_native", "source_fidelity"],
        )
        for call, envelope in zip(transport.calls, envelopes):
            _, headers, raw, timeout = call
            expected_hash = hashlib.sha256(HTTP._canonical_json(envelope["request"])).hexdigest()
            self.assertEqual(envelope["schema"], HTTP.REQUEST_SCHEMA)
            self.assertEqual(envelope["request_sha256"], expected_hash)
            self.assertEqual(headers["Idempotency-Key"], envelope["request_id"])
            self.assertEqual(headers["X-Localization-Request-Sha256"], expected_hash)
            self.assertEqual(timeout, 60.0)
            self.assertNotIn(b"\\u00f6", raw)

        target_only = json.dumps(envelopes[1]["request"]["input"], ensure_ascii=False)
        self.assertNotIn("Build your business", target_only)
        self.assertNotIn('"source"', target_only)
        fidelity = envelopes[2]["request"]["input"]
        self.assertEqual(fidelity["source"]["text"], "Build your business with BLUN.")

    def test_response_must_bind_to_the_exact_request(self):
        for field, value in (
            ("schema", "wrong.schema"),
            ("request_id", "b" * 64),
            ("request_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                transport = FakeTransport(
                    lambda body, field=field, value=value: response_for(
                        body, mutate=lambda envelope: envelope.__setitem__(field, value)
                    )
                )
                with self.assertRaises(WORKER.LocalizationWorkerBlocked) as caught:
                    WORKER.run_localization_job(job(), assets(), adapter(transport))
                self.assertEqual(caught.exception.code, "provider.response_binding")
                self.assertFalse(caught.exception.retryable)

    def test_http_statuses_have_bounded_retry_semantics_and_no_internal_retry(self):
        for status, expected_code, retryable in (
            (302, "redirect", False),
            (401, "http_status", False),
            (408, "http_status", True),
            (429, "http_status", True),
            (503, "http_status", True),
        ):
            with self.subTest(status=status):
                transport = FakeTransport(lambda _body, status=status: HTTP.HTTPResult(status, (), b""))
                with self.assertRaises(HTTP.HTTPProviderFailed) as caught:
                    adapter(transport).invoke(provider_request())
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(len(transport.calls), 1)

    def test_network_failure_is_content_free_and_retryable(self):
        secret = "do-not-log-this-token"
        transport = FakeTransport(RuntimeError("network included " + secret))
        with self.assertRaises(HTTP.HTTPProviderFailed) as caught:
            adapter(transport, lambda: {"Authorization": "Bearer " + secret}).invoke(provider_request())
        self.assertEqual(caught.exception.code, "network")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn(secret, str(caught.exception))

    def test_authentication_is_host_injected_and_reserved_or_unsafe_headers_block(self):
        cases = (
            {},
            {"Content-Type": "text/plain"},
            {"Authorization": "one", "authorization": "two"},
            {"Authorization": "Bearer token\nX-Evil: yes"},
            {"Authorization": "Bearer token\tcontinued"},
            {"Bad Header": "value"},
        )
        for supplied in cases:
            with self.subTest(supplied=supplied):
                transport = FakeTransport(response_for)
                with self.assertRaises(HTTP.HTTPProviderFailed) as caught:
                    adapter(transport, lambda supplied=supplied: supplied).invoke(provider_request())
                self.assertEqual(caught.exception.code, "authentication")
                self.assertEqual(transport.calls, [])

    def test_endpoint_requires_https_unless_loopback_is_explicit(self):
        with self.assertRaises(ValueError):
            HTTP.HTTPProviderAdapter("http://models.example.test/v1", lambda: {"X-Key": "x"})
        with self.assertRaises(ValueError):
            HTTP.HTTPProviderAdapter("https://user:secret@models.example.test/v1", lambda: {"X-Key": "x"})
        with self.assertRaises(ValueError):
            HTTP.HTTPProviderAdapter("https://models.example.test/v1?secret=x", lambda: {"X-Key": "x"})
        local = HTTP.HTTPProviderAdapter(
            "http://127.0.0.1:8042/v1/localize",
            lambda: {"X-Key": "x"},
            allow_loopback_http=True,
        )
        self.assertEqual(local.endpoint, "http://127.0.0.1:8042/v1/localize")

    def test_untrusted_request_shape_and_header_unsafe_id_block_before_transport(self):
        class BadRequest:
            request_id = "blun-l10n-call-" + "a" * 63 + "\n"

            def as_payload(self):
                return provider_request().as_payload()

        class MismatchedRequest:
            request_id = REQUEST_ID_VALUE

            def as_payload(self):
                value = provider_request().as_payload()
                value["request_id"] = "blun-l10n-call-" + "b" * 64
                return value

        for request in (BadRequest(), MismatchedRequest()):
            with self.subTest(request=type(request).__name__):
                transport = FakeTransport(response_for)
                with self.assertRaises(HTTP.HTTPProviderFailed) as caught:
                    adapter(transport).invoke(request)
                self.assertEqual(caught.exception.code, "request_invalid")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(transport.calls, [])

    def test_invalid_transport_status_blocks_as_retryable_transport_failure(self):
        for status in (0, 99, 600):
            with self.subTest(status=status):
                transport = FakeTransport(lambda _body, status=status: HTTP.HTTPResult(status, (), b""))
                with self.assertRaises(HTTP.HTTPProviderFailed) as caught:
                    adapter(transport).invoke(provider_request())
                self.assertEqual(caught.exception.code, "transport_invalid")
                self.assertTrue(caught.exception.retryable)

    def test_response_requires_json_content_type_and_exact_declared_length(self):
        valid = response_for(HTTP._canonical_json({
            "schema": HTTP.REQUEST_SCHEMA,
            "request_id": REQUEST_ID_VALUE,
            "request_sha256": hashlib.sha256(HTTP._canonical_json(provider_request().as_payload())).hexdigest(),
            "request": provider_request().as_payload(),
        }))
        cases = (
            HTTP.HTTPResult(200, (("Content-Type", "text/html"),), valid.body),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"), ("Content-Length", "1")), valid.body),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"), ("content-type", "application/json")), valid.body),
        )
        for result in cases:
            with self.subTest(headers=result.headers):
                transport = FakeTransport(lambda _body, result=result: result)
                with self.assertRaises(HTTP.HTTPProviderFailed):
                    adapter(transport).invoke(provider_request())

    def test_response_parser_rejects_bom_duplicate_keys_nonfinite_and_extra_fields(self):
        bodies = (
            b'\xef\xbb\xbf{}',
            b'{"schema":"x","schema":"y"}',
            b'{"value":NaN}',
            b'{}',
        )
        for body in bodies:
            with self.subTest(body=body):
                transport = FakeTransport(
                    lambda _request, body=body: HTTP.HTTPResult(
                        200, (("Content-Type", "application/json"),), body
                    )
                )
                with self.assertRaises(HTTP.HTTPProviderFailed) as caught:
                    adapter(transport).invoke(provider_request())
                self.assertIn(caught.exception.code, {"response_json", "response_binding"})

    def test_request_id_and_hash_are_stable_across_identical_replays(self):
        first = FakeTransport(response_for)
        second = FakeTransport(response_for)
        adapter(first).invoke(provider_request())
        adapter(second).invoke(provider_request())
        self.assertEqual(first.calls[0][2], second.calls[0][2])
        self.assertEqual(first.calls[0][1]["Idempotency-Key"], second.calls[0][1]["Idempotency-Key"])


if __name__ == "__main__":
    unittest.main()
