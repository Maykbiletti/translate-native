from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CLIENT = load(
    "blun_test_website_localization_benchmark_client",
    ROOT / "integrations" / "website_localization_benchmark_client.py",
)
HTTP = CLIENT._HTTP

CAMPAIGN_ID = "benchmark-campaign-" + "a" * 64
POLICY_SHA256 = "b" * 64
SUITE_SHA256 = "c" * 64


def campaign_status(
    *, complete=True, blocked=False, finalization="succeeded",
    campaign_id=CAMPAIGN_ID, policy_sha256=POLICY_SHA256,
    suite_sha256=SUITE_SHA256, valid_until=1_800_000_000,
):
    counts = {
        "pending": 0, "leased": 0, "retry_wait": 0,
        "succeeded": 1 if complete and not blocked else 0,
        "failed": 1 if blocked else 0,
    }
    if not complete and not blocked:
        counts["pending"] = 1
    failed = finalization == "failed"
    return {
        "campaign_id": campaign_id,
        "policy_sha256": policy_sha256,
        "suite_sha256": suite_sha256,
        "valid_until": valid_until,
        "work_count": 1,
        "counts": counts,
        "error_counts": {"provider.failed": 1} if blocked else {},
        "complete": complete and not blocked,
        "blocked": blocked or failed,
        "report_finalization": {
            "status": finalization,
            "attempt": 0 if finalization == "pending" else 1,
            "max_attempts": 3,
            "next_attempt_at": 100,
            "error_code": (
                "benchmark.report.failed"
                if finalization in {"retry_wait", "failed"} else None
            ),
        },
    }


def benchmark_report(*, status="BLOCK", suite_sha256=SUITE_SHA256):
    return {
        "schema": HTTP.BENCHMARK_REPORT_SCHEMA,
        "valid_until": 1_800_000_000,
        "suite": {
            "version": "website-localization-suite-3",
            "sha256": suite_sha256,
        },
        "status": status,
        "superiority_claim_allowed": status == "PASS",
        "claim_block_reasons": (
            [] if status == "PASS" else ["configured_locale_evaluation_failed"]
        ),
        "attestation": {"algorithm": "host-signature", "signature": "signed"},
    }


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def response(value, *, status=200, headers=()):
    body = encoded(value)
    standard = (
        ("Content-Type", "application/json; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Length", str(len(body))),
    )
    return CLIENT.HTTPResult(status, standard + tuple(headers), body)


def status_response(status=None):
    return response({
        "schema": HTTP.STATUS_RESPONSE_SCHEMA,
        "campaign": campaign_status() if status is None else status,
    })


def report_response(report=None, *, campaign_id=CAMPAIGN_ID, digest=None):
    report = benchmark_report() if report is None else report
    digest = hashlib.sha256(encoded(report)).hexdigest() if digest is None else digest
    return response({
        "schema": HTTP.REPORT_RESPONSE_SCHEMA,
        "campaign_id": campaign_id,
        "report_sha256": digest,
        "report": report,
    })


def openapi_response(*, document=None, contract_digest=None, document_digest=None):
    contract = HTTP._openapi_contract()
    expected = HTTP._OPENAPI.build_document(contract)
    document = expected if document is None else document
    return response({
        "schema": HTTP.OPENAPI_RESPONSE_SCHEMA,
        "contract_sha256": (
            HTTP._OPENAPI.document_sha256(contract)
            if contract_digest is None else contract_digest
        ),
        "openapi_sha256": (
            HTTP._OPENAPI.document_sha256(document)
            if document_digest is None else document_digest
        ),
        "openapi": document,
    })


class Transport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class BenchmarkClientTests(unittest.TestCase):
    @staticmethod
    def client(transport, *, clock=lambda: 100, credentials=None, **kwargs):
        return CLIENT.WebsiteLocalizationBenchmarkClient(
            "https://benchmark.example",
            credentials or (lambda: {"Authorization": "Bearer private-token"}),
            expected_campaign_id=CAMPAIGN_ID,
            expected_policy_sha256=POLICY_SHA256,
            expected_suite_sha256=SUITE_SHA256,
            transport=transport,
            clock=clock,
            **kwargs,
        )

    def assert_failure(self, code, call, *, retryable=None):
        with self.assertRaises(CLIENT.BenchmarkClientFailed) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if retryable is not None:
            self.assertEqual(caught.exception.retryable, retryable)

    def test_reads_bound_status_then_preserves_valid_block_report(self):
        transport = Transport(status_response(), report_response())
        snapshot = self.client(transport).report()
        self.assertEqual(snapshot.report["status"], "BLOCK")
        self.assertFalse(snapshot.report["superiority_claim_allowed"])
        self.assertEqual(
            snapshot.report["claim_block_reasons"],
            ["configured_locale_evaluation_failed"],
        )
        self.assertEqual(snapshot.campaign["policy_sha256"], POLICY_SHA256)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(transport.calls[0][1].endswith(HTTP.STATUS_PATH))
        self.assertTrue(transport.calls[1][1].endswith(HTTP.REPORT_PATH))
        for call in transport.calls:
            self.assertEqual(call[0], "GET")
            self.assertEqual(call[2]["Authorization"], "Bearer private-token")
            self.assertIsNone(call[3])

    def test_pass_report_is_returned_without_rewriting_claim(self):
        report = benchmark_report(status="PASS")
        snapshot = self.client(
            Transport(status_response(), report_response(report)),
        ).report()
        self.assertEqual(snapshot.report["status"], "PASS")
        self.assertTrue(snapshot.report["superiority_claim_allowed"])

    def test_reads_status_then_returns_exact_origin_free_openapi(self):
        transport = Transport(status_response(), openapi_response())
        snapshot = self.client(transport).openapi()
        self.assertEqual(snapshot.openapi["openapi"], "3.1.0")
        self.assertNotIn("servers", snapshot.openapi)
        self.assertEqual(set(snapshot.openapi["paths"]), {
            HTTP.STATUS_PATH, HTTP.REPORT_PATH, HTTP.OPENAPI_PATH,
        })
        self.assertTrue(transport.calls[0][1].endswith(HTTP.STATUS_PATH))
        self.assertTrue(transport.calls[1][1].endswith(HTTP.OPENAPI_PATH))

    def test_self_rehashed_or_contract_stale_openapi_fails_closed(self):
        changed = deepcopy(HTTP._OPENAPI.build_document(HTTP._openapi_contract()))
        changed["info"]["title"] = "Substituted reader contract"
        scenarios = (
            (openapi_response(document=changed), "benchmark_client.openapi_mismatch"),
            (openapi_response(contract_digest="d" * 64),
             "benchmark_client.contract_mismatch"),
            (openapi_response(document_digest="d" * 64),
             "benchmark_client.openapi_mismatch"),
        )
        for result, code in scenarios:
            with self.subTest(code=code):
                self.assert_failure(
                    code,
                    self.client(Transport(status_response(), result)).openapi,
                )

    def test_foreign_campaign_policy_or_suite_blocks_before_report(self):
        scenarios = (
            (
                campaign_status(campaign_id="benchmark-campaign-" + "d" * 64),
                "benchmark_client.campaign_mismatch",
            ),
            (campaign_status(policy_sha256="d" * 64), "benchmark_client.policy_mismatch"),
            (campaign_status(suite_sha256="d" * 64), "benchmark_client.suite_mismatch"),
        )
        for status, code in scenarios:
            with self.subTest(code=code):
                transport = Transport(status_response(status))
                self.assert_failure(code, self.client(transport).report)
                self.assertEqual(len(transport.calls), 1)

    def test_unready_campaign_never_fetches_report(self):
        scenarios = (
            (campaign_status(complete=False, finalization="pending"),
             "benchmark_client.campaign_incomplete", True),
            (campaign_status(blocked=True, finalization="failed"),
             "benchmark_client.campaign_blocked", False),
            (campaign_status(finalization="retry_wait"),
             "benchmark_client.report_unavailable", True),
        )
        for status, code, retryable in scenarios:
            with self.subTest(code=code):
                transport = Transport(status_response(status))
                self.assert_failure(
                    code, self.client(transport).report, retryable=retryable,
                )
                self.assertEqual(len(transport.calls), 1)

    def test_report_digest_campaign_and_suite_drift_fail_closed(self):
        scenarios = (
            (report_response(digest="d" * 64), "benchmark_client.report_digest_mismatch"),
            (report_response(campaign_id="benchmark-campaign-" + "d" * 64),
             "benchmark_client.response_invalid"),
            (report_response(benchmark_report(suite_sha256="d" * 64)),
             "benchmark_client.response_invalid"),
        )
        for report_result, code in scenarios:
            with self.subTest(code=code):
                self.assert_failure(
                    code,
                    self.client(Transport(status_response(), report_result)).report,
                )

    def test_expiry_before_status_or_between_requests_blocks(self):
        transport = Transport(status_response(campaign_status(valid_until=100)))
        self.assert_failure(
            "benchmark_client.campaign_expired",
            self.client(transport, clock=lambda: 100).report,
        )
        self.assertEqual(len(transport.calls), 1)

        times = iter((100, 1_800_000_000))
        transport = Transport(status_response(), report_response())
        self.assert_failure(
            "benchmark_client.campaign_expired",
            self.client(transport, clock=lambda: next(times)).report,
        )
        self.assertEqual(len(transport.calls), 2)

    def test_remote_errors_preserve_retry_decision(self):
        for status, retryable in ((409, False), (503, True)):
            with self.subTest(status=status):
                result = response({
                    "schema": HTTP.ERROR_RESPONSE_SCHEMA,
                    "error_code": "benchmark.http.report_unavailable",
                    "retryable": retryable,
                }, status=status)
                self.assert_failure(
                    "benchmark.http.report_unavailable",
                    self.client(Transport(result)).status,
                    retryable=retryable,
                )

    def test_authentication_invalid_precedes_network(self):
        transport = Transport(RuntimeError("must not run"))
        self.assert_failure(
            "benchmark_client.authentication_invalid",
            self.client(
                transport,
                credentials=lambda: {"Host": "attacker.example"},
            ).status,
        )
        self.assertEqual(transport.calls, [])

    def test_transport_and_ambiguous_response_fail_content_free(self):
        malformed = response({"schema": HTTP.STATUS_RESPONSE_SCHEMA, "campaign": {}})
        malformed = CLIENT.HTTPResult(
            malformed.status,
            malformed.headers + (("Content-Length", str(len(malformed.body))),),
            malformed.body,
        )
        for result, code in (
            (RuntimeError("private network failure"), "benchmark_client.network"),
            (malformed, "benchmark_client.response_invalid"),
            (CLIENT.HTTPResult(302, (), b"private redirect"),
             "benchmark_client.response_invalid"),
        ):
            with self.subTest(code=code):
                try:
                    self.client(Transport(result)).status()
                except CLIENT.BenchmarkClientFailed as error:
                    self.assertEqual(error.code, code)
                    self.assertNotIn("private", str(error))
                else:
                    self.fail("unsafe response was accepted")

    def test_unsafe_origins_and_dependencies_are_rejected(self):
        arguments = dict(
            credential_headers=lambda: {},
            expected_campaign_id=CAMPAIGN_ID,
            expected_policy_sha256=POLICY_SHA256,
            expected_suite_sha256=SUITE_SHA256,
            clock=lambda: 0,
        )
        for origin in (
            "http://benchmark.example", "https://user@example.com",
            "https://benchmark.example/path", "https://benchmark.example?query=1",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                CLIENT.WebsiteLocalizationBenchmarkClient(origin, **arguments)
        with self.assertRaises(ValueError):
            CLIENT.WebsiteLocalizationBenchmarkClient(
                "https://benchmark.example", transport=object(), **arguments,
            )


if __name__ == "__main__":
    unittest.main()
