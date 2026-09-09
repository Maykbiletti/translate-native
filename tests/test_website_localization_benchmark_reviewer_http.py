from __future__ import annotations

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
    "blun_test_benchmark_reviewer_http",
    ROOT / "integrations" / "website_localization_benchmark_reviewer_http.py",
)
BENCHMARK = load(
    "blun_test_benchmark_reviewer_http_external_contract",
    ROOT / "integrations" / "website_localization_benchmark.py",
)


def review_request(*, phase="target_native", suffix="1"):
    digest = hashlib.sha256(f"{phase}:{suffix}".encode()).hexdigest()
    blind_id = "blind-" + digest
    review_input = {
        "blind_id": blind_id,
        "benchmark_version": "native-vs-baseline-1",
        "benchmark_suite": {
            "version": "website-localization-benchmark-suite-v3",
            "sha256": "a" * 64,
            "case_key_sha256": "b" * 64,
        },
        "target": {"language": "mt", "locale": "mt-MT", "script": "Latn"},
        "content_type": "marketing",
        "audience": "small businesses",
        "tone_profile": "clear and contemporary",
        "policy_version": "native-web-2",
        "quality_profile": {"locale": "mt-MT", "version": "mt-quality-1"},
        "variants": [
            {"label": "A", "text": "Agħżel il-pjan li jaqbel għalik."},
            {"label": "B", "text": "Scegli il piano adatto a te."},
        ],
        "response_schema": {
            "schema": BENCHMARK.REVIEW_SCHEMA,
            "phase": phase,
            "target_locale": "mt-MT",
            "blind_id": blind_id,
            "preference": "A, B, or tie",
            "variants": {
                "A": {"blocking_defects": [], "major_defects": []},
                "B": {"blocking_defects": [], "major_defects": []},
            },
        },
    }
    if phase == "source_fidelity":
        review_input["benchmark_suite"].update({
            "case_key": "marketing-health-1",
            "domain": "health",
            "long_form": False,
            "adversarial_tags": ["translationese"],
        })
        review_input.update({
            "source": {"locale": "en-IE", "text": "Choose the plan that suits you."},
            "glossary": [],
            "protected_terms": [],
        })
    else:
        review_input["target_terms"] = []
    system = (
        BENCHMARK._NATIVE_SYSTEM
        if phase == "target_native"
        else BENCHMARK._FIDELITY_SYSTEM
    )
    return BENCHMARK.BenchmarkReviewRequest(
        schema=BENCHMARK.BENCHMARK_SCHEMA,
        review_id="benchmark-review-" + digest,
        phase=phase,
        target_locale="mt-MT",
        system_instruction=system,
        input=review_input,
    )


def review_response(request, *, preference="A", **overrides):
    value = {
        "schema": BENCHMARK.REVIEW_SCHEMA,
        "phase": request.phase,
        "target_locale": request.target_locale,
        "blind_id": request.input["blind_id"],
        "preference": preference,
        "variants": {
            "A": {"blocking_defects": [], "major_defects": []},
            "B": {"blocking_defects": [], "major_defects": []},
        },
    }
    value.update(overrides)
    return value


def http_response(request, **review_overrides):
    request_bytes = json.dumps(
        request.as_payload(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    body = json.dumps({
        "schema": HTTP.RESPONSE_SCHEMA,
        "review_id": request.review_id,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "review": review_response(request, **review_overrides),
    }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return HTTP.HTTPResult(
        200,
        (("Content-Type", "application/json; charset=utf-8"),
         ("Content-Length", str(len(body)))),
        body,
    )


class Transport:
    def __init__(self):
        self.calls = []
        self.results = []
        self.error = None

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.results.pop(0)


class HTTPBenchmarkReviewerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.adapter = HTTP.HTTPBenchmarkReviewerAdapter(
            "https://review.example.test/v1/blind-reviews",
            lambda: {"Authorization": "Bearer reviewer-secret"},
            transport=self.transport,
            timeout=45,
        )

    def failure(self, action):
        with self.assertRaises(HTTP.HTTPBenchmarkReviewerFailed) as caught:
            action()
        self.assertEqual(str(caught.exception), caught.exception.code)
        return caught.exception

    def test_separate_origin_blind_requests_use_exact_phase_contracts(self):
        native = review_request(phase="target_native")
        fidelity = review_request(phase="source_fidelity")
        self.transport.results = [http_response(native), http_response(fidelity)]

        native_result = self.adapter.review(native)
        fidelity_result = self.adapter.review(fidelity)

        self.assertEqual(native_result["phase"], "target_native")
        self.assertEqual(fidelity_result["phase"], "source_fidelity")
        self.assertEqual(len(self.transport.calls), 2)
        first = json.loads(self.transport.calls[0][2])
        second = json.loads(self.transport.calls[1][2])
        self.assertEqual(first["review"]["system_instruction"], BENCHMARK._NATIVE_SYSTEM)
        self.assertEqual(second["review"]["system_instruction"], BENCHMARK._FIDELITY_SYSTEM)
        self.assertNotIn("source", first["review"]["input"])
        self.assertIn("source", second["review"]["input"])
        for envelope in (first, second):
            def keys(value):
                if isinstance(value, dict):
                    return set(value).union(*(keys(item) for item in value.values()))
                if isinstance(value, list):
                    return set().union(*(keys(item) for item in value))
                return set()

            self.assertTrue(
                {"candidate_provider_id", "baseline_id", "origin"}.isdisjoint(keys(envelope))
            )

    def test_headers_bind_authentication_idempotency_and_request_hash(self):
        request = review_request()
        self.transport.results = [http_response(request)]

        self.adapter.review(request)

        url, headers, body, timeout = self.transport.calls[0]
        envelope = json.loads(body)
        expected_hash = hashlib.sha256(json.dumps(
            request.as_payload(), ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(url, "https://review.example.test/v1/blind-reviews")
        self.assertEqual(timeout, 45.0)
        self.assertEqual(headers["Authorization"], "Bearer reviewer-secret")
        self.assertEqual(headers["Idempotency-Key"], request.review_id)
        self.assertEqual(headers["X-Benchmark-Review-Id"], request.review_id)
        self.assertEqual(headers["X-Benchmark-Review-Phase"], request.phase)
        self.assertEqual(headers["X-Benchmark-Review-Request-Sha256"], expected_hash)
        self.assertEqual(envelope["request_sha256"], expected_hash)
        self.assertEqual(envelope["review"], request.as_payload())

    def test_request_contract_blocks_source_leak_and_wrong_system_before_auth(self):
        authentication_calls = []
        adapter = HTTP.HTTPBenchmarkReviewerAdapter(
            "https://review.example.test/v1/blind-reviews",
            lambda: authentication_calls.append(True) or {"Authorization": "secret"},
            transport=self.transport,
        )
        native = review_request()
        native.input["source"] = {"text": "must remain hidden"}
        error = self.failure(lambda: adapter.review(native))
        self.assertEqual((error.code, error.retryable), ("source_blindness", False))

        changed = review_request(suffix="2")
        object.__setattr__(changed, "system_instruction", BENCHMARK._FIDELITY_SYSTEM)
        error = self.failure(lambda: adapter.review(changed))
        self.assertEqual((error.code, error.retryable), ("request_invalid", False))
        self.assertEqual(authentication_calls, [])
        self.assertEqual(self.transport.calls, [])

    def test_separately_loaded_frozen_contract_is_accepted(self):
        request = review_request()
        self.assertIsNot(type(request), HTTP._BENCHMARK.BenchmarkReviewRequest)
        self.transport.results = [http_response(request)]
        response, request_hash, response_hash = HTTP._BENCHMARK._invoke(
            self.adapter, request,
        )
        self.assertEqual(response["preference"], "A")
        self.assertEqual(len(request_hash), 64)
        self.assertEqual(len(response_hash), 64)

        self.transport.results = [HTTP.HTTPResult(503, (), b"")]
        with self.assertRaises(HTTP._BENCHMARK.BenchmarkBlocked) as caught:
            HTTP._BENCHMARK._invoke(self.adapter, request)
        self.assertEqual(caught.exception.code, "reviewer.http_status")
        self.assertTrue(caught.exception.retryable)

    def test_endpoint_and_authentication_are_strict(self):
        invalid_endpoints = (
            "http://review.example.test/v1/reviews",
            "https://user:secret@review.example.test/v1/reviews",
            "https://review.example.test/v1/reviews?token=secret",
            "https://review.example.test//other",
        )
        for endpoint in invalid_endpoints:
            with self.assertRaises(ValueError):
                HTTP.HTTPBenchmarkReviewerAdapter(
                    endpoint, lambda: {"Authorization": "x"},
                )
        HTTP.HTTPBenchmarkReviewerAdapter(
            "http://127.0.0.1:8080/v1/reviews",
            lambda: {"Authorization": "x"},
            transport=self.transport,
            allow_loopback_http=True,
        )
        request = review_request()
        for supplied in (
            {},
            {"Content-Type": "text/plain"},
            {"Authorization": "Bearer secret\r\nX-Forged: yes"},
            {"Authorization": "Bearer sécret"},
            {"authorization": "one", "Authorization": "two"},
        ):
            adapter = HTTP.HTTPBenchmarkReviewerAdapter(
                "https://review.example.test/v1/reviews",
                lambda supplied=supplied: supplied,
                transport=self.transport,
            )
            error = self.failure(lambda adapter=adapter: adapter.review(request))
            self.assertEqual((error.code, error.retryable), ("authentication", False))
            self.assertNotIn("secret", str(error))
        self.assertEqual(self.transport.calls, [])

    def test_statuses_and_network_failures_have_bounded_retry_policy(self):
        request = review_request()
        for status, retryable in ((302, False), (400, False), (408, True), (429, True), (503, True)):
            self.transport.results = [HTTP.HTTPResult(status, (), b"")]
            error = self.failure(lambda: self.adapter.review(request))
            code = "redirect" if status == 302 else "http_status"
            self.assertEqual((error.code, error.retryable), (code, retryable))
        self.transport.error = RuntimeError("private provider detail")
        error = self.failure(lambda: self.adapter.review(request))
        self.assertEqual((error.code, error.retryable), ("network", True))
        self.assertNotIn("private", str(error))

    def test_response_parser_rejects_wrong_type_bom_duplicates_and_size(self):
        request = review_request()
        good = http_response(request)
        variants = (
            HTTP.HTTPResult(200, (("Content-Type", "text/plain"),), good.body),
            HTTP.HTTPResult(
                200, (("Content-Type", "application/json"),),
                b"\xef\xbb\xbf" + good.body,
            ),
            HTTP.HTTPResult(
                200, (("Content-Type", "application/json"),),
                b'{"schema":1,"schema":2}',
            ),
            HTTP.HTTPResult(
                200, (("Content-Type", "application/json"),),
                b"x" * (HTTP.MAX_RESPONSE_BYTES + 1),
            ),
            HTTP.HTTPResult(
                200,
                (("Content-Type", "application/json"), ("Content-Length", "1")),
                good.body,
            ),
        )
        expected = (
            "response_content_type", "response_json", "response_json",
            "response_size", "response_size",
        )
        for result, code in zip(variants, expected):
            self.transport.results = [result]
            error = self.failure(lambda: self.adapter.review(request))
            self.assertEqual((error.code, error.retryable), (code, True))

    def test_response_binding_and_review_schema_fail_terminally(self):
        request = review_request()
        result = http_response(request)
        envelope = json.loads(result.body)
        envelope["request_sha256"] = "0" * 64
        body = json.dumps(envelope, separators=(",", ":")).encode()
        self.transport.results = [HTTP.HTTPResult(
            200, (("Content-Type", "application/json"),), body,
        )]
        error = self.failure(lambda: self.adapter.review(request))
        self.assertEqual((error.code, error.retryable), ("response_binding", False))

        self.transport.results = [http_response(request, phase="source_fidelity")]
        error = self.failure(lambda: self.adapter.review(request))
        self.assertEqual((error.code, error.retryable), ("response_invalid", False))

        invalid = review_response(request)
        invalid["variants"]["A"]["major_defects"] = [{
            "class": "translationese",
            "excerpt": "Agħżel il-pjan",
            "reason": "The preferred variant contains a major defect.",
        }]
        result = http_response(request)
        envelope = json.loads(result.body)
        envelope["review"] = invalid
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
        self.transport.results = [HTTP.HTTPResult(
            200, (("Content-Type", "application/json"),), body,
        )]
        error = self.failure(lambda: self.adapter.review(request))
        self.assertEqual((error.code, error.retryable), ("response_invalid", False))

    def test_request_mutation_during_transport_blocks_response(self):
        request = review_request()
        response = http_response(request)

        class MutatingTransport:
            def post(self, url, headers, body, *, timeout):
                request.input["blind_id"] = "blind-" + "f" * 64
                return response

        adapter = HTTP.HTTPBenchmarkReviewerAdapter(
            "https://review.example.test/v1/reviews",
            lambda: {"Authorization": "secret"},
            transport=MutatingTransport(),
        )
        error = self.failure(lambda: adapter.review(request))
        self.assertEqual((error.code, error.retryable), ("request_mutated", False))


if __name__ == "__main__":
    unittest.main()
