from __future__ import annotations

import hashlib
import importlib.util
import io
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
    "blun_test_website_localization_benchmark_http",
    ROOT / "integrations" / "website_localization_benchmark_http.py",
)


CAMPAIGN_ID = "benchmark-campaign-" + "a" * 64
OTHER_CAMPAIGN_ID = "benchmark-campaign-" + "b" * 64
SUITE_SHA256 = "c" * 64
POLICY_SHA256 = "d" * 64


def campaign_status(*, complete=True, blocked=False):
    counts = {
        "pending": 0 if complete else 1,
        "leased": 0,
        "retry_wait": 0,
        "succeeded": 1 if complete else 0,
        "failed": 0,
    }
    return {
        "campaign_id": CAMPAIGN_ID,
        "policy_sha256": POLICY_SHA256,
        "suite_sha256": SUITE_SHA256,
        "valid_until": 1_800_000_000,
        "work_count": 1,
        "counts": counts,
        "error_counts": {},
        "complete": complete,
        "blocked": blocked,
        "report_finalization": {
            "status": "succeeded" if complete else "pending",
            "attempt": 1 if complete else 0,
            "max_attempts": 3,
            "next_attempt_at": 100,
            "error_code": None,
        },
    }


def benchmark_report():
    return {
        "schema": HTTP.BENCHMARK_REPORT_SCHEMA,
        "valid_until": 1_800_000_000,
        "suite": {"version": "website-localization-suite-3", "sha256": SUITE_SHA256},
        "status": "BLOCK",
        "superiority_claim_allowed": False,
        "claim_block_reasons": ["configured_locale_evaluation_failed"],
        "attestation": {"algorithm": "host-signature", "signature": "signed"},
    }


class Runtime:
    def __init__(self, *, status=None, report=None):
        self.status_value = campaign_status() if status is None else status
        self.report_value = benchmark_report() if report is None else report
        self.status_calls = 0
        self.report_calls = 0

    def benchmark_campaign_status(self):
        self.status_calls += 1
        if isinstance(self.status_value, Exception):
            raise self.status_value
        return self.status_value

    def load_benchmark_report(self):
        self.report_calls += 1
        if isinstance(self.report_value, Exception):
            raise self.report_value
        return self.report_value


class BoundaryFailure(RuntimeError):
    def __init__(self, code, *, retryable=False, private=""):
        super().__init__(private)
        self.code = code
        self.retryable = retryable


class BenchmarkHTTPTests(unittest.TestCase):
    @staticmethod
    def principal(campaign_id=CAMPAIGN_ID):
        return {
            "schema": HTTP.PRINCIPAL_SCHEMA,
            "reader_id": "benchmark-auditor-1",
            "campaign_id": campaign_id,
            "credential_id": "audit-credential-1",
            "credential_version": "2026-09-09",
        }

    @staticmethod
    def call(app, path, *, method="GET", scheme="https", query="", body=b"", headers=None):
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = status
            captured["headers"] = dict(response_headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "wsgi.url_scheme": scheme,
            "CONTENT_LENGTH": str(len(body)) if body else "",
            "wsgi.input": io.BytesIO(body),
            "HTTP_AUTHORIZATION": "Bearer private-reader-token",
        }
        environ.update(headers or {})
        response = b"".join(app(environ, start_response))
        payload = json.loads(response) if response else None
        return captured["status"], captured["headers"], payload, response

    def test_authenticated_reader_gets_status_and_exact_verified_report(self):
        runtime = Runtime()
        authenticated = []

        def authenticate(request):
            authenticated.append(request)
            return self.principal()

        app = HTTP.BenchmarkReportHTTPApplication(runtime, authenticate)
        status, headers, payload, _ = self.call(app, HTTP.STATUS_PATH)
        self.assertTrue(status.startswith("200 "))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(payload, {
            "schema": HTTP.STATUS_RESPONSE_SCHEMA,
            "campaign": campaign_status(),
        })

        status, headers, payload, encoded = self.call(app, HTTP.REPORT_PATH)
        self.assertTrue(status.startswith("200 "))
        self.assertEqual(headers["Cache-Control"], "no-store")
        expected_report = benchmark_report()
        expected_hash = hashlib.sha256(
            json.dumps(
                expected_report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(payload, {
            "schema": HTTP.REPORT_RESPONSE_SCHEMA,
            "campaign_id": CAMPAIGN_ID,
            "report_sha256": expected_hash,
            "report": expected_report,
        })
        self.assertEqual(int(headers["Content-Length"]), len(encoded))
        self.assertEqual(runtime.status_calls, 2)
        self.assertEqual(runtime.report_calls, 1)
        self.assertEqual(len(authenticated), 2)
        for request, path in zip(
            authenticated, (HTTP.STATUS_PATH, HTTP.REPORT_PATH), strict=True,
        ):
            self.assertEqual(request["schema"], HTTP.AUTH_REQUEST_SCHEMA)
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["path"], path)
            self.assertEqual(
                request["body_sha256"], hashlib.sha256(b"").hexdigest(),
            )
            self.assertIn(
                ["authorization", "Bearer private-reader-token"],
                request["headers"],
            )

    def test_campaign_scope_and_incomplete_report_block_before_loading(self):
        runtime = Runtime()
        app = HTTP.BenchmarkReportHTTPApplication(
            runtime, lambda _: self.principal(OTHER_CAMPAIGN_ID),
        )
        status, _, payload, _ = self.call(app, HTTP.REPORT_PATH)
        self.assertTrue(status.startswith("403 "))
        self.assertEqual(payload["error_code"], "benchmark.http.campaign_forbidden")
        self.assertEqual(runtime.report_calls, 0)

        runtime = Runtime(status=campaign_status(complete=False))
        app = HTTP.BenchmarkReportHTTPApplication(
            runtime, lambda _: self.principal(),
        )
        status, _, payload, _ = self.call(app, HTTP.REPORT_PATH)
        self.assertTrue(status.startswith("409 "))
        self.assertEqual(payload["error_code"], "benchmark.http.report_unavailable")
        self.assertEqual(runtime.report_calls, 0)

    def test_transport_and_authentication_fail_before_runtime_access(self):
        scenarios = (
            ({"scheme": "http"}, "400 ", "benchmark.http.https_required"),
            ({"query": "campaign=other"}, "400 ", "benchmark.http.query_invalid"),
            ({"method": "POST"}, "404 ", "benchmark.http.route_not_found"),
            ({"body": b"x"}, "400 ", "benchmark.http.body_not_allowed"),
            (
                {"headers": {"HTTP_X_BAD": "line\nbreak"}},
                "400 ",
                "benchmark.http.headers_invalid",
            ),
        )
        for options, expected_status, expected_code in scenarios:
            with self.subTest(expected_code=expected_code):
                runtime = Runtime()
                auth_calls = []
                app = HTTP.BenchmarkReportHTTPApplication(
                    runtime, lambda request: auth_calls.append(request) or self.principal(),
                )
                status, _, payload, _ = self.call(
                    app, HTTP.STATUS_PATH, **options,
                )
                self.assertTrue(status.startswith(expected_status))
                self.assertEqual(payload["error_code"], expected_code)
                self.assertEqual(auth_calls, [])
                self.assertEqual(runtime.status_calls, 0)

        for authenticator, expected_status, expected_code, retryable in (
            (
                lambda _: {"schema": "wrong"},
                "401 ",
                "benchmark.http.authentication_failed",
                False,
            ),
            (
                lambda _: (_ for _ in ()).throw(RuntimeError("private auth failure")),
                "503 ",
                "benchmark.http.authentication_unavailable",
                True,
            ),
        ):
            runtime = Runtime()
            app = HTTP.BenchmarkReportHTTPApplication(runtime, authenticator)
            status, _, payload, encoded = self.call(app, HTTP.STATUS_PATH)
            self.assertTrue(status.startswith(expected_status))
            self.assertEqual(payload["error_code"], expected_code)
            self.assertEqual(payload["retryable"], retryable)
            self.assertNotIn(b"private auth failure", encoded)
            self.assertEqual(runtime.status_calls, 0)

    def test_runtime_and_response_failures_are_content_free(self):
        runtime = Runtime(status=RuntimeError("private campaign state"))
        app = HTTP.BenchmarkReportHTTPApplication(
            runtime, lambda _: self.principal(),
        )
        status, _, payload, encoded = self.call(app, HTTP.STATUS_PATH)
        self.assertTrue(status.startswith("503 "))
        self.assertEqual(payload["error_code"], "benchmark.http.runtime_unavailable")
        self.assertNotIn(b"private campaign state", encoded)

        runtime = Runtime(report=BoundaryFailure(
            "benchmark.campaign.report_missing", private="private report state",
        ))
        app = HTTP.BenchmarkReportHTTPApplication(
            runtime, lambda _: self.principal(),
        )
        status, _, payload, encoded = self.call(app, HTTP.REPORT_PATH)
        self.assertTrue(status.startswith("409 "))
        self.assertEqual(payload["error_code"], "benchmark.campaign.report_missing")
        self.assertNotIn(b"private report state", encoded)

        for runtime in (
            Runtime(status={"campaign_id": CAMPAIGN_ID}),
            Runtime(report={"schema": HTTP.BENCHMARK_REPORT_SCHEMA}),
        ):
            app = HTTP.BenchmarkReportHTTPApplication(
                runtime, lambda _: self.principal(),
            )
            path = (
                HTTP.STATUS_PATH
                if runtime.status_value == {"campaign_id": CAMPAIGN_ID}
                else HTTP.REPORT_PATH
            )
            status, _, payload, _ = self.call(app, path)
            self.assertTrue(status.startswith("503 "))
            self.assertEqual(payload["error_code"], "benchmark.http.response_invalid")

    def test_constructor_requires_exact_runtime_boundary(self):
        with self.assertRaises(ValueError):
            HTTP.BenchmarkReportHTTPApplication(object(), lambda _: self.principal())
        with self.assertRaises(ValueError):
            HTTP.BenchmarkReportHTTPApplication(Runtime(), None)


if __name__ == "__main__":
    unittest.main()
