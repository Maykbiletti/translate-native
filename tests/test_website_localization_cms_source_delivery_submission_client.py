from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from tests import test_website_localization_cms_client as cms_support
from tests import (
    test_website_localization_cms_source_delivery_submission_runtime
    as runtime_support,
)


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLIENT = load(
    "blun_test_website_localization_submission_client",
    ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_client.py",
)
HTTP = CLIENT._HTTP


class WSGITransport:
    def __init__(self, application):
        self.application = application
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        parsed = urlsplit(url)
        raw = b"" if body is None else body
        environ = {
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": method,
            "wsgi.url_scheme": parsed.scheme,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        for name, value in headers.items():
            normalized = name.upper().replace("-", "_")
            if normalized == "CONTENT_TYPE":
                environ[normalized] = value
            elif normalized != "CONTENT_LENGTH":
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

    def request(self, method, url, headers, body, *, timeout):
        result = self.transport.request(
            method, url, headers, body, timeout=timeout,
        )
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.transform(len(self.calls), result)


class StaticTransport:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.result


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


class SourceDeliverySubmissionClientTests(unittest.TestCase):
    def setUp(self):
        self.support = runtime_support.SourceDeliverySubmissionRuntimeTests(
            methodName="runTest",
        )
        self.support.setUp()
        self.runtime = self.support.open(
            hosted=True,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
        )
        self.server_authentication = []

        def authenticate(request):
            self.server_authentication.append(copy.deepcopy(request))
            path = request["path"]
            base = {
                "principal_id": "website-backend",
                "credential_id": "public-client",
                "credential_version": "1",
                "scope": (
                    HTTP._SUBMISSION.CAPABILITIES_HTTP_SCOPE
                    if path == HTTP.CAPABILITIES_PATH
                    else HTTP.MONITOR_SCOPES[path]
                    if path in HTTP.MONITOR_PATHS
                    else HTTP.SCOPES[path]
                ),
            }
            if path == HTTP.CAPABILITIES_PATH:
                return {
                    "schema": HTTP._SUBMISSION.CAPABILITIES_HTTP_PRINCIPAL_SCHEMA,
                    **base,
                }
            if path in HTTP.MONITOR_PATHS:
                return {"schema": HTTP.OPERATOR_PRINCIPAL_SCHEMA, **base}
            return {
                "schema": (
                    HTTP.READ_PRINCIPAL_SCHEMA
                    if path in HTTP.READ_PATHS
                    else HTTP.PRINCIPAL_SCHEMA
                ),
                **base,
                "site_id": "site-1",
            }

        self.application = HTTP.build_submission_http(
            self.runtime, authenticate,
        )
        self.transport = WSGITransport(self.application)
        self.client_authentication = []
        self.digest = self.runtime.submission_capabilities().as_payload()[
            "sha256"
        ]
        self.client = self.make_client()

    def tearDown(self):
        self.support.tearDown()

    def make_client(self, *, transport=None, digest=None, headers=None):
        def authentication(context):
            self.client_authentication.append(copy.deepcopy(context))
            if headers is not None:
                return headers
            return {"Authorization": "Bearer public-client-test"}

        return CLIENT.CMSSourceDeliverySubmissionHTTPClient(
            "https://website.example",
            self.digest if digest is None else digest,
            authentication,
            transport=self.transport if transport is None else transport,
        )

    @staticmethod
    def payload_hash(payload):
        return hashlib.sha256(CLIENT._canonical(payload)).hexdigest()

    def test_capabilities_are_fresh_exact_and_pinned(self):
        response = self.client.capabilities()

        capabilities = response["capabilities"]
        self.assertEqual(capabilities["sha256"], self.digest)
        self.assertEqual(set(capabilities["operations"]), {
            "capabilities_http", "enqueue_change", "enqueue_removal",
            "submission_health", "submission_lifecycle",
            "submission_pipeline_health", "submission_pipeline_readiness",
            "submission_readiness", "submission_status",
        })
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.client_authentication[0], {
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "GET",
            "origin": "https://website.example",
            "path": HTTP.CAPABILITIES_PATH,
            "scope": HTTP._SUBMISSION.CAPABILITIES_HTTP_SCOPE,
            "body_sha256": hashlib.sha256(b"").hexdigest(),
        })

    def test_changes_removals_status_and_lifecycle_cross_the_public_edge(self):
        change = cms_support.event()
        cancellation = cms_support.cancellation(change)
        tombstone = cms_support.tombstone(change)

        accepted = self.client.submit_change(
            change, source_max_attempts=3, delivery_max_attempts=4,
        )
        replayed = self.client.submit_change(
            change, source_max_attempts=3, delivery_max_attempts=4,
        )
        cancelled = self.client.submit_removal(cancellation)
        deleted = self.client.submit_removal(tombstone)
        identity = (
            accepted["operation"], accepted["request_id"],
            accepted["event_id"], accepted["site_id"],
            accepted["payload_sha256"],
        )
        status = self.client.submission_status(*identity)
        lifecycle = self.client.submission_lifecycle(*identity)

        self.assertEqual(accepted, replayed)
        self.assertEqual(cancelled["operation"], "cancellation")
        self.assertEqual(deleted["operation"], "tombstone")
        self.assertEqual(status["result"]["request_id"], change["event_id"])
        self.assertEqual(
            lifecycle["result"]["submission"]["payload_sha256"],
            self.payload_hash(change),
        )
        writes = [call for call in self.transport.calls if call[0] == "POST"]
        self.assertEqual(len(writes), 6)
        first_write = writes[0]
        self.assertEqual(first_write[2]["Idempotency-Key"], change["event_id"])
        self.assertEqual(
            first_write[2]["X-Localization-Source-Payload-Sha256"],
            self.payload_hash(change),
        )
        rendered = json.dumps((accepted, status, lifecycle), sort_keys=True)
        self.assertNotIn(change["localization"]["source_text"], rendered)
        self.assertNotIn("target_text", rendered)

    def test_operator_health_and_readiness_are_separate_and_content_free(self):
        health = self.client.pipeline_health()
        readiness = self.client.pipeline_readiness()

        self.assertEqual(health["result"]["status"], "ok")
        self.assertEqual(readiness["result"]["status"], "ready")
        self.assertTrue(health["content_free"])
        self.assertFalse(readiness["publication_authority"])
        operational = self.client_authentication[-1]
        self.assertEqual(
            operational["scope"], HTTP.PIPELINE_READINESS_SCOPE,
        )
        self.assertNotIn("site_id", operational)
        self.assertEqual(operational["body_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertNotIn("source_text", json.dumps((health, readiness)))

    def test_rehashed_capability_substitution_blocks_before_operation(self):
        def transform(number, result):
            if number != 1:
                return result
            return replace_json(result, lambda value: (
                value["capabilities"]["semantics"].update({
                    "accepted_implies_publication": True,
                }),
                value["capabilities"].update({
                    "sha256": hashlib.sha256(CLIENT._canonical({
                        key: content
                        for key, content in value["capabilities"].items()
                        if key != "sha256"
                    })).hexdigest(),
                }),
            ))

        transport = TransformingTransport(self.transport, transform)
        client = self.make_client(transport=transport)

        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            client.submit_change(cms_support.event())

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.capabilities_binding",
        )
        self.assertEqual(len(transport.calls), 1)

        stale = self.make_client(digest="e" * 64)
        before = len(self.transport.calls)
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            stale.capabilities()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.capabilities_binding",
        )
        self.assertEqual(len(self.transport.calls), before + 1)

    def test_cross_tenant_and_private_response_tampering_fail_closed(self):
        change = cms_support.event()

        def change_site(number, result):
            if number == 2:
                return replace_json(
                    result, lambda value: value.update({"site_id": "site-2"}),
                )
            return result

        client = self.make_client(
            transport=TransformingTransport(self.transport, change_site),
        )
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            client.submit_change(change)
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.submission_binding",
        )

        def add_private(number, result):
            if number == 2:
                return replace_json(
                    result,
                    lambda value: value["result"].update({
                        "source_text": "private",
                    }),
                )
            return result

        client = self.make_client(
            transport=TransformingTransport(self.transport, add_private),
        )
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            client.pipeline_health()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.health_binding",
        )

    def test_invalid_payload_and_reserved_authentication_block_pre_network(self):
        before = len(self.transport.calls)
        invalid = cms_support.event()
        invalid.pop("site_id")

        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            self.client.submit_change(invalid)
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.request_invalid",
        )
        self.assertEqual(len(self.transport.calls), before)

        client = self.make_client(headers={"Idempotency-Key": "injected"})
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            client.capabilities()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.authentication",
        )
        self.assertEqual(len(self.transport.calls), before)

    def test_redirects_and_network_failures_are_not_retried(self):
        redirect = StaticTransport(CLIENT.HTTPResult(
            307,
            (("Content-Type", "application/json"),),
            b"{}",
        ))
        client = self.make_client(transport=redirect)
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            client.capabilities()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.redirect",
        )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(redirect.calls), 1)

        unavailable = StaticTransport(error=TimeoutError("late"))
        client = self.make_client(transport=unavailable)
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionClientBlocked,
        ) as caught:
            client.pipeline_readiness()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_client.network",
        )
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(unavailable.calls), 1)

    def test_configuration_rejects_unsafe_origins_and_invalid_pins(self):
        with self.assertRaises(ValueError):
            CLIENT.CMSSourceDeliverySubmissionHTTPClient(
                "http://website.example", self.digest, lambda _value: {},
            )
        with self.assertRaises(ValueError):
            CLIENT.CMSSourceDeliverySubmissionHTTPClient(
                "https://website.example", "bad", lambda _value: {},
            )
        self.assertNotIn("website.example", repr(self.client))


if __name__ == "__main__":
    unittest.main()
