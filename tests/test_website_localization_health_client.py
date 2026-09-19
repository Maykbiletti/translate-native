from __future__ import annotations

import importlib.util
import json
import sys
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


CLIENT = load(
    "blun_test_website_localization_health_client",
    ROOT / "integrations" / "website_localization_health_client.py",
)
HTTP = CLIENT._HTTP


def report(*, status="healthy", reason=None):
    component_status = status
    return {
        "schema": HTTP.HEALTH_SCHEMA,
        "checked_at": 100.0,
        "status": status,
        "components": [{
            "component": "release",
            "status": component_status,
            "reasons": [] if reason is None else [reason],
            "counts": {"website_versions": 1},
        }],
        "providers": [],
        "website_versions": [{
            "event_id": "event-1",
            "site_id": "site-1",
            "website_version": "version-1",
            "plan_id": "plan-1",
            "status": (
                "ready" if status == "healthy" else "awaiting_approval"
            ),
            "required_locales": 1,
            "approved_locales": 1 if status == "healthy" else 0,
            "queue_counts": {"succeeded": 1},
            "blocked_locales": (
                [] if status == "healthy"
                else [["fi-FI", "publication.evidence.policy_unavailable"]]
            ),
        }],
    }


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def response(value, status):
    body = encoded(value)
    return CLIENT.HTTPResult(status, (
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    ), body)


class Transport:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.result


class WebsiteLocalizationHealthClientTests(unittest.TestCase):
    @staticmethod
    def client(transport, headers=lambda: {"Authorization": "Bearer private"}, **options):
        return CLIENT.WebsiteLocalizationHealthClient(
            "https://localization.example",
            headers,
            transport=transport,
            clock=lambda: 100,
            **options,
        )

    def test_reads_one_fresh_health_report_with_bounded_credentials(self):
        payload = {
            "schema": HTTP.RESPONSE_SCHEMA,
            "report": report(),
        }
        transport = Transport(response(payload, 200))

        snapshot = self.client(transport).read()

        self.assertEqual(snapshot.http_status, 200)
        self.assertEqual(snapshot.as_payload(), report())
        self.assertEqual(len(transport.calls), 1)
        method, url, headers, body, timeout = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://localization.example/v2/localization/health")
        self.assertEqual(body, None)
        self.assertEqual(timeout, 10.0)
        self.assertEqual(headers["Authorization"], "Bearer private")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Connection"], "close")

    def test_preserves_every_policy_block_as_a_valid_503_report(self):
        for reason in (
            "release.policy_unavailable",
            "release.policy_stale",
            "release.integrity_failed",
        ):
            with self.subTest(reason=reason):
                current = report(status="blocked", reason=reason)
                transport = Transport(response({
                    "schema": HTTP.RESPONSE_SCHEMA,
                    "report": current,
                }, 503))

                snapshot = self.client(transport).read()

                self.assertEqual(snapshot.http_status, 503)
                self.assertEqual(
                    snapshot.report["components"][0]["reasons"], [reason],
                )
                self.assertEqual(
                    snapshot.report["website_versions"][0]["blocked_locales"],
                    [["fi-FI", "publication.evidence.policy_unavailable"]],
                )

    def test_remote_contract_errors_keep_exact_retry_decision(self):
        for status, code, retryable in (
            (401, "health.http.authentication_failed", False),
            (503, "health.http.authentication_unavailable", True),
            (503, "health.http.response_invalid", False),
        ):
            with self.subTest(code=code):
                transport = Transport(response({
                    "schema": HTTP.ERROR_SCHEMA,
                    "error_code": code,
                    "retryable": retryable,
                }, status))

                with self.assertRaises(CLIENT.HealthClientFailed) as caught:
                    self.client(transport).read()

                self.assertEqual(caught.exception.code, code)
                self.assertIs(caught.exception.retryable, retryable)

    def test_stale_and_future_reports_are_retryable_failures(self):
        for checked_at, options in (
            (39.0, {}),
            (106.0, {}),
            (100.0, {"max_report_age_seconds": True}),
        ):
            with self.subTest(checked_at=checked_at, options=options):
                current = report()
                current["checked_at"] = checked_at
                transport = Transport(response({
                    "schema": HTTP.RESPONSE_SCHEMA,
                    "report": current,
                }, 200))
                if options:
                    with self.assertRaises(ValueError):
                        self.client(transport, **options)
                    continue

                with self.assertRaises(CLIENT.HealthClientFailed) as caught:
                    self.client(transport).read()

                self.assertEqual(
                    caught.exception.code, "health_client.report_stale",
                )
                self.assertTrue(caught.exception.retryable)

    def test_invalid_report_status_and_http_status_never_pass(self):
        invalid = report(status="blocked", reason="release.policy_stale")
        invalid["status"] = "healthy"
        scenarios = (
            response({"schema": HTTP.RESPONSE_SCHEMA, "report": invalid}, 200),
            response({"schema": HTTP.RESPONSE_SCHEMA, "report": report()}, 503),
        )
        for item in scenarios:
            with self.subTest(status=item.status):
                with self.assertRaises(CLIENT.HealthClientFailed) as caught:
                    self.client(Transport(item)).read()
                self.assertEqual(
                    caught.exception.code, "health_client.response_invalid",
                )

    def test_duplicate_json_and_response_header_ambiguity_block(self):
        duplicate = (
            b'{"schema":"' + HTTP.RESPONSE_SCHEMA.encode("ascii")
            + b'","schema":"' + HTTP.RESPONSE_SCHEMA.encode("ascii")
            + b'","report":{}}'
        )
        bad_json = CLIENT.HTTPResult(200, (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(duplicate))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ), duplicate)
        good = response({"schema": HTTP.RESPONSE_SCHEMA, "report": report()}, 200)
        duplicate_header = CLIENT.HTTPResult(
            good.status,
            good.headers + (("cache-control", "no-store"),),
            good.body,
        )
        for item in (bad_json, duplicate_header):
            with self.subTest(headers=len(item.headers)):
                with self.assertRaises(CLIENT.HealthClientFailed) as caught:
                    self.client(Transport(item)).read()
                self.assertEqual(
                    caught.exception.code, "health_client.response_invalid",
                )

    def test_authentication_failure_precedes_network_and_reveals_no_detail(self):
        scenarios = (
            (
                lambda: {"Host": "attacker.example"},
                "health_client.authentication_invalid",
                False,
            ),
            (
                lambda: (_ for _ in ()).throw(
                    RuntimeError("private credential diagnostic")
                ),
                "health_client.authentication_unavailable",
                True,
            ),
        )
        for provider, code, retryable in scenarios:
            with self.subTest(code=code):
                transport = Transport(error=AssertionError("must not call network"))
                with self.assertRaises(CLIENT.HealthClientFailed) as caught:
                    self.client(transport, headers=provider).read()
                self.assertEqual(caught.exception.code, code)
                self.assertIs(caught.exception.retryable, retryable)
                self.assertEqual(transport.calls, [])
                self.assertNotIn("private", str(caught.exception))

    def test_transport_exception_is_content_free_and_retryable(self):
        transport = Transport(error=RuntimeError("private TLS diagnostic"))

        with self.assertRaises(CLIENT.HealthClientFailed) as caught:
            self.client(transport).read()

        self.assertEqual(caught.exception.code, "health_client.network")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private TLS diagnostic", str(caught.exception))

    def test_unsafe_origins_and_invalid_dependencies_fail_before_use(self):
        options = (
            "http://localization.example",
            "https://user:secret@localization.example",
            "https://localization.example/path",
            "https://localization.example?query=1",
        )
        for origin in options:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                CLIENT.WebsiteLocalizationHealthClient(
                    origin, lambda: {}, transport=Transport(), clock=lambda: 100,
                )

        loopback = CLIENT.WebsiteLocalizationHealthClient(
            "http://127.0.0.1:8080", lambda: {}, transport=Transport(),
            clock=lambda: 100, allow_loopback_http=True,
        )
        self.assertEqual(loopback.origin, "http://127.0.0.1:8080")

    def test_response_security_headers_length_and_size_are_mandatory(self):
        good = response({"schema": HTTP.RESPONSE_SCHEMA, "report": report()}, 200)
        malformed = (
            CLIENT.HTTPResult(good.status, tuple(
                (name, "public") if name.lower() == "cache-control" else (name, value)
                for name, value in good.headers
            ), good.body),
            CLIENT.HTTPResult(good.status, tuple(
                (name, "1") if name.lower() == "content-length" else (name, value)
                for name, value in good.headers
            ), good.body),
            CLIENT.HTTPResult(
                503,
                tuple(
                    (name, str(CLIENT.MAX_RESPONSE_BYTES + 1))
                    if name.lower() == "content-length" else (name, value)
                    for name, value in good.headers
                ),
                b"x" * (CLIENT.MAX_RESPONSE_BYTES + 1),
            ),
        )
        for item in malformed:
            with self.subTest(length=len(item.body)):
                with self.assertRaises(CLIENT.HealthClientFailed) as caught:
                    self.client(Transport(item)).read()
                self.assertEqual(
                    caught.exception.code, "health_client.response_invalid",
                )


if __name__ == "__main__":
    unittest.main()
