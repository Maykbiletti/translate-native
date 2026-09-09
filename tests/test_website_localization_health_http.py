from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HTTP = load(
    "blun_test_website_localization_health_http",
    ROOT / "integrations" / "website_localization_health_http.py",
)


def report(*, status="healthy"):
    return {
        "schema": HTTP.HEALTH_SCHEMA,
        "checked_at": 100.0,
        "status": status,
        "components": [
            {
                "component": "storage",
                "status": status,
                "reasons": [] if status == "healthy" else ["monitor.state_unreadable"],
                "counts": {"connections": 5},
            },
            {
                "component": "queue",
                "status": "healthy",
                "reasons": [],
                "counts": {"pending": 1, "succeeded": 0},
            },
        ],
        "providers": [
            {
                "provider_id": "customer-llm",
                "model_id": "configured-model",
                "model_version": "2026-09-09",
                "status": "healthy",
                "reason": None,
            },
        ],
        "website_versions": [
            {
                "event_id": "event-1",
                "site_id": "site-1",
                "website_version": "website-1",
                "plan_id": "plan-1",
                "status": "processing",
                "required_locales": 2,
                "approved_locales": 0,
                "queue_counts": {"pending": 2, "succeeded": 0},
                "blocked_locales": [],
            },
        ],
    }


class Report:
    def __init__(self, payload):
        self.payload = payload

    def as_payload(self):
        return deepcopy(self.payload)


class WebsiteLocalizationHealthHTTPTests(unittest.TestCase):
    @staticmethod
    def principal(**overrides):
        value = {
            "schema": HTTP.PRINCIPAL_SCHEMA,
            "reader_id": "operator-1",
            "credential_id": "health-reader-1",
            "credential_version": "2026-09-09",
            "scope": "service-health",
        }
        value.update(overrides)
        return value

    @staticmethod
    def call(app, *, method="GET", path=None, scheme="https", query="", body=b"", headers=None):
        captured = {}
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": HTTP.HEALTH_PATH if path is None else path,
            "QUERY_STRING": query,
            "wsgi.url_scheme": scheme,
            "CONTENT_LENGTH": str(len(body)) if body else "",
            "wsgi.input": io.BytesIO(body),
            "HTTP_AUTHORIZATION": "Bearer private-health-token",
        }
        environ.update(headers or {})
        response = b"".join(app(
            environ,
            lambda status, response_headers: captured.update(
                status=status, headers=dict(response_headers),
            ),
        ))
        return captured["status"], captured["headers"], json.loads(response), response

    def app(self, health=None, authenticator=None):
        health = Report(report()) if health is None else health
        authenticator = authenticator or (lambda request: self.principal())
        return HTTP.WebsiteLocalizationHealthHTTPApplication(
            lambda *, now: health,
            authenticator,
            clock=lambda: 100,
        )

    def test_authenticated_operator_gets_exact_content_free_health(self):
        requests = []
        app = self.app(authenticator=lambda request: requests.append(request) or self.principal())

        status, headers, payload, encoded = self.call(app)

        self.assertEqual(status, "200 OK")
        self.assertEqual(payload, {
            "schema": HTTP.RESPONSE_SCHEMA,
            "report": report(),
        })
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), len(encoded))
        self.assertEqual(requests[0]["schema"], HTTP.AUTH_REQUEST_SCHEMA)
        self.assertEqual(requests[0]["method"], "GET")
        self.assertEqual(requests[0]["path"], HTTP.HEALTH_PATH)
        self.assertEqual(
            requests[0]["body_sha256"], hashlib.sha256(b"").hexdigest(),
        )
        self.assertIn(
            ["authorization", "Bearer private-health-token"],
            requests[0]["headers"],
        )
        self.assertNotIn("source_text", json.dumps(payload))

    def test_cancelled_website_state_is_valid_content_free_health(self):
        current = report()
        current["providers"] = []
        current["website_versions"][0]["status"] = "cancelled"

        status, _, payload, _ = self.call(self.app(Report(current)))

        self.assertEqual(status, "200 OK")
        self.assertEqual(
            payload["report"]["website_versions"][0]["status"], "cancelled",
        )

    def test_valid_blocked_report_uses_service_unavailable_without_hiding_report(self):
        status, _, payload, _ = self.call(self.app(Report(report(status="blocked"))))

        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["report"]["status"], "blocked")
        self.assertEqual(payload["report"]["components"][0]["status"], "blocked")

        status, _, payload, _ = self.call(
            self.app(Report(report(status="degraded"))),
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["report"]["status"], "degraded")

    def test_authentication_failure_and_outage_call_no_monitor(self):
        calls = []

        def health_provider(*, now):
            calls.append(now)
            return Report(report())

        for authenticator, code, retryable in (
            (lambda request: None, "health.http.authentication_failed", False),
            (
                lambda request: (_ for _ in ()).throw(RuntimeError("private auth outage")),
                "health.http.authentication_unavailable",
                True,
            ),
        ):
            with self.subTest(code=code):
                app = HTTP.WebsiteLocalizationHealthHTTPApplication(
                    health_provider, authenticator, clock=lambda: 100,
                )
                status, _, payload, _ = self.call(app)
                self.assertIn(status, {"401 Unauthorized", "503 Service Unavailable"})
                self.assertEqual(payload["error_code"], code)
                self.assertEqual(payload["retryable"], retryable)
                self.assertEqual(calls, [])
                self.assertNotIn("private auth outage", json.dumps(payload))

    def test_transport_rejects_non_https_queries_bodies_and_wrong_routes_before_auth(self):
        scenarios = (
            ({"scheme": "http"}, "health.http.https_required"),
            ({"query": "details=true"}, "health.http.query_invalid"),
            ({"method": "POST"}, "health.http.route_not_found"),
            ({"path": "/wrong"}, "health.http.route_not_found"),
            ({"body": b"private"}, "health.http.body_not_allowed"),
            (
                {"headers": {"HTTP_TRANSFER_ENCODING": "chunked"}},
                "health.http.transfer_encoding",
            ),
            (
                {"headers": {"HTTP_X_BAD": "line\nbreak"}},
                "health.http.headers_invalid",
            ),
        )
        for options, code in scenarios:
            with self.subTest(code=code):
                auth_calls = []
                status, _, payload, _ = self.call(
                    self.app(authenticator=lambda request: auth_calls.append(request)),
                    **options,
                )
                self.assertTrue(status.startswith(("400 ", "404 ")))
                self.assertEqual(payload["error_code"], code)
                self.assertEqual(auth_calls, [])

    def test_invalid_principal_scope_blocks_before_monitor(self):
        calls = []
        app = HTTP.WebsiteLocalizationHealthHTTPApplication(
            lambda *, now: calls.append(now),
            lambda request: self.principal(scope="site-health"),
            clock=lambda: 100,
        )

        status, _, payload, _ = self.call(app)

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(payload["error_code"], "health.http.authentication_failed")
        self.assertEqual(calls, [])

    def test_malformed_or_prose_bearing_monitor_output_blocks_without_echo(self):
        malformed = report()
        malformed["private_customer_text"] = "secret offer"
        status, _, payload, _ = self.call(self.app(Report(malformed)))
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error_code"], "health.http.response_invalid")
        self.assertNotIn("secret offer", json.dumps(payload))

        malformed = report()
        malformed["components"][0]["reasons"] = ["customer secret sentence"]
        status, _, payload, _ = self.call(self.app(Report(malformed)))
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error_code"], "health.http.response_invalid")
        self.assertNotIn("customer secret sentence", json.dumps(payload))

        stale = report()
        stale["checked_at"] = 99
        status, _, payload, _ = self.call(self.app(Report(stale)))
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error_code"], "health.http.response_invalid")

        inconsistent = report()
        inconsistent["status"] = "blocked"
        status, _, payload, _ = self.call(self.app(Report(inconsistent)))
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error_code"], "health.http.response_invalid")

    def test_response_size_is_bounded(self):
        with self.assertRaisesRegex(
            HTTP.HealthHTTPFailed, "health.http.response_invalid",
        ):
            HTTP._canonical_json({"value": "x" * HTTP.MAX_RESPONSE_BYTES})

    def test_monitor_failure_is_content_free_and_retryable(self):
        def unavailable(*, now):
            raise RuntimeError("private database path")

        app = HTTP.WebsiteLocalizationHealthHTTPApplication(
            unavailable, lambda request: self.principal(), clock=lambda: 100,
        )
        status, _, payload, _ = self.call(app)

        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload["error_code"], "health.http.monitor_unavailable")
        self.assertTrue(payload["retryable"])
        self.assertNotIn("private database path", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
