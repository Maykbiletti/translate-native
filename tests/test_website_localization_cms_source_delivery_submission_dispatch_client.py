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
    test_website_localization_cms_source_delivery_submission_dispatch_http
    as http_support,
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
    "blun_test_website_localization_submission_dispatch_client",
    ROOT / "integrations" / "website_localization_cms_source_delivery_submission_dispatch_client.py",
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
            "PATH_INFO": parsed.path, "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": method, "wsgi.url_scheme": parsed.scheme,
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
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
            captured["headers"], response_body,
        )


class TransformingTransport:
    def __init__(self, transport, transform):
        self.transport = transport
        self.transform = transform
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        result = self.transport.request(method, url, headers, body, timeout=timeout)
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


class StaleCapabilityTransport:
    def __init__(self, transport):
        self.transport = transport
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        changed = dict(headers)
        if urlsplit(url).path != HTTP.CAPABILITIES_PATH:
            changed[HTTP.CAPABILITIES_PRECONDITION_HEADER] = "0" * 64
        self.calls.append((method, url, changed, body, timeout))
        return self.transport.request(
            method, url, changed, body, timeout=timeout,
        )


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


class SubmissionDispatchClientTests(unittest.TestCase):
    def setUp(self):
        self.support = http_support.SubmissionDispatchHTTPTests(methodName="runTest")
        self.support.setUp()
        self.runtime = self.support.open()
        self.transport = WSGITransport(self.runtime.http)
        self.auth_calls = []
        self.public_digest = self.runtime.expected_capabilities_sha256
        self.digest = HTTP._capabilities_payload(self.public_digest)["sha256"]
        self.client = self.make_client()

    def tearDown(self):
        self.support.tearDown()

    def make_client(self, *, transport=None, digest=None, public_digest=None, headers=None):
        def authenticate(context):
            self.auth_calls.append(copy.deepcopy(context))
            return headers or {"Authorization": "Bearer cms-sidecar-client-test"}

        return CLIENT.CMSSourceDeliverySubmissionDispatchHTTPClient(
            "https://cms-sidecar.example",
            self.digest if digest is None else digest,
            self.public_digest if public_digest is None else public_digest,
            authenticate,
            transport=self.transport if transport is None else transport,
        )

    def test_capabilities_are_fresh_exact_and_double_pinned(self):
        response = self.client.capabilities()

        self.assertEqual(response["capabilities"]["sha256"], self.digest)
        self.assertEqual(
            response["capabilities"]["public_submission_capabilities_sha256"],
            self.public_digest,
        )
        self.assertEqual(set(response["capabilities"]["operations"]), {
            "capabilities", "commercial_profile", "enqueue", "health",
            "lifecycle", "openapi", "readiness", "status",
        })
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.auth_calls[0], {
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "GET", "origin": "https://cms-sidecar.example",
            "path": HTTP.CAPABILITIES_PATH,
            "scope": HTTP.SCOPES[HTTP.CAPABILITIES_PATH],
            "body_sha256": hashlib.sha256(b"").hexdigest(),
        })

    def test_changes_cancellations_tombstones_and_status_cross_sidecar(self):
        change = cms_support.event()
        cancellation = cms_support.cancellation(change)
        tombstone = cms_support.tombstone(change)

        accepted = self.client.enqueue(
            change, source_max_attempts=3, delivery_max_attempts=4,
            client_max_attempts=2,
        )
        replay = self.client.enqueue(
            change, source_max_attempts=3, delivery_max_attempts=4,
            client_max_attempts=2,
        )
        removed = self.client.enqueue(cancellation)
        deleted = self.client.enqueue(tombstone)
        status_payload = accepted["status"]
        identity = tuple(status_payload[name] for name in (
            "operation", "request_id", "event_id", "site_id", "payload_sha256",
        ))
        status = self.client.status(*identity)

        self.assertEqual(accepted, replay)
        self.assertEqual(removed["status"]["operation"], "cancellation")
        self.assertEqual(deleted["status"]["operation"], "tombstone")
        self.assertEqual(status["status"]["request_id"], change["event_id"])
        self.assertEqual(
            status["status"]["commercial_contract_binding"],
            self.client._expected_capabilities["commercial_contract_binding"],
        )
        self.assertIsNone(
            removed["status"]["commercial_contract_binding"]
        )
        self.assertIsNone(
            deleted["status"]["commercial_contract_binding"]
        )
        self.assertIsNone(
            status["status"]["remote_website_capability_binding"]
        )
        rendered = json.dumps((accepted, status), sort_keys=True)
        self.assertNotIn(change["localization"]["source_text"], rendered)
        self.assertNotIn("target_text", rendered)

    def test_verified_lifecycle_crosses_reference_client_after_acceptance(self):
        change = cms_support.event()
        queued = self.client.enqueue(change)["status"]
        identity = tuple(queued[name] for name in (
            "operation", "request_id", "event_id", "site_id",
            "payload_sha256",
        ))

        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked,
        ) as pending:
            self.client.lifecycle(*identity)
        self.assertEqual(pending.exception.http_status, 409)
        self.assertEqual(
            pending.exception.remote_error_code,
            "submission_dispatch_http.lifecycle_not_accepted",
        )
        self.assertFalse(pending.exception.retryable)

        with self.runtime._lock:
            self.runtime._dispatcher.run_once(
                self.runtime._client, "manual-client-lifecycle", now=100,
            )
        response = self.client.lifecycle(*identity)
        lifecycle = response["lifecycle"]
        self.assertEqual(lifecycle["dispatch_status"]["status"], "accepted")
        self.assertEqual(
            lifecycle["source_lifecycle"]["result"]["submission"]["event_id"],
            change["event_id"],
        )
        self.assertFalse(response["accepted_implies_publication"])
        rendered = json.dumps(response, sort_keys=True)
        self.assertNotIn(change["localization"]["source_text"], rendered)
        self.assertNotIn("target_text", rendered)

    def test_status_rejects_a_self_consistent_cross_registry_acceptance(self):
        change = cms_support.event()
        queued = self.client.enqueue(change)["status"]
        identity = tuple(queued[name] for name in (
            "operation", "request_id", "event_id", "site_id", "payload_sha256",
        ))
        with self.runtime._lock:
            self.runtime._dispatcher.run_once(
                self.runtime._client, "manual-cross-registry", now=100,
            )

        def transform(number, result):
            if number != 2:
                return result

            def substitute(value):
                status = value["status"]
                binding = dict(status["remote_website_capability_binding"])
                binding["commercial_rendering_registry_sha256"] = "0" * 64
                binding["binding_sha256"] = hashlib.sha256("\x00".join((
                    binding["schema"], binding["database_role"],
                    binding["delivery_capabilities_sha256"],
                    binding["runtime_capabilities_sha256"],
                    binding["commercial_rendering_registry_sha256"],
                    binding["terminal_receiver_capabilities_sha256"],
                )).encode("utf-8")).hexdigest()
                status["remote_website_capability_binding"] = binding
                status["remote_binding_sha256"] = hashlib.sha256(
                    json.dumps(
                        binding, ensure_ascii=False, allow_nan=False,
                        sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()

            return replace_json(result, substitute)

        client = self.make_client(transport=TransformingTransport(
            WSGITransport(self.runtime.http), transform,
        ))
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked,
        ) as blocked:
            client.status(*identity)
        self.assertEqual(
            blocked.exception.code,
            "source_delivery_submission_dispatch_client.status_binding",
        )

    def test_substituted_nested_lifecycle_binding_is_rejected(self):
        change = cms_support.event()
        queued = self.client.enqueue(change)["status"]
        identity = tuple(queued[name] for name in (
            "operation", "request_id", "event_id", "site_id",
            "payload_sha256",
        ))
        with self.runtime._lock:
            self.runtime._dispatcher.run_once(
                self.runtime._client, "manual-tamper-lifecycle", now=100,
            )

        def transform(number, result):
            if number != 2:
                return result
            return replace_json(result, lambda value: value["lifecycle"]
                ["source_lifecycle"]["result"]["website_capability_binding"]
                .update({"runtime_capabilities_sha256": "0" * 64}))

        client = self.make_client(transport=TransformingTransport(
            WSGITransport(self.runtime.http), transform,
        ))
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked,
        ) as caught:
            client.lifecycle(*identity)
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_client.lifecycle_binding",
        )

    def test_enqueue_binds_auth_body_headers_identity_and_three_budgets(self):
        change = cms_support.event()
        response = self.client.enqueue(
            change, source_max_attempts=2, delivery_max_attempts=3,
            client_max_attempts=4,
        )

        calls = self.transport.calls
        self.assertEqual(len(calls), 2)
        request = json.loads(calls[1][3])
        _copy, identity, canonical = HTTP._DISPATCH._payload(change)
        self.assertEqual(request["source_max_attempts"], 2)
        self.assertEqual(request["delivery_max_attempts"], 3)
        self.assertEqual(request["client_max_attempts"], 4)
        self.assertEqual(calls[1][2]["Idempotency-Key"], identity["request_id"])
        self.assertEqual(
            calls[1][2][HTTP.CAPABILITIES_PRECONDITION_HEADER], self.digest,
        )
        self.assertEqual(
            calls[1][2]["X-Localization-Source-Payload-Sha256"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(self.auth_calls[1]["body_sha256"], hashlib.sha256(calls[1][3]).hexdigest())
        self.assertEqual(self.auth_calls[1]["capabilities_sha256"], self.digest)
        self.assertEqual(self.auth_calls[1]["site_id"], "site-1")
        self.assertEqual(response["status"]["client_max_attempts"], 4)

    def test_enqueue_binds_commercial_profile_and_rejects_cross_scope_injection(self):
        commercial = cms_support.event()
        commercial["localization"]["content_type"] = "commercial"
        self.client.enqueue(commercial)
        commercial_request = json.loads(self.transport.calls[1][3])
        self.assertEqual(
            commercial_request["commercial_contract_binding"],
            self.client._expected_capabilities["commercial_contract_binding"],
        )

        ordinary = cms_support.event()
        ordinary["event_id"] = "ordinary-binding-null"
        ordinary["localization"]["content_type"] = "marketing"
        self.client.enqueue(ordinary)
        ordinary_request = json.loads(self.transport.calls[3][3])
        self.assertIsNone(ordinary_request["commercial_contract_binding"])

    def test_health_and_readiness_are_separate_content_free_and_bound(self):
        health = self.client.health()
        readiness = self.client.readiness()

        self.assertEqual(health["health"]["expected_capabilities_sha256"], self.public_digest)
        self.assertEqual(readiness["readiness"]["capabilities_sha256"], self.public_digest)
        self.assertEqual(readiness["readiness"]["status"], "ready")
        self.assertNotIn("payload", json.dumps((health, readiness)))

    def test_commercial_profile_is_exact_live_bound_and_tamper_evident(self):
        response = self.client.commercial_profile()

        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(
            response["commercial_profile"],
            self.client._expected_capabilities["commercial_profile"],
        )
        self.assertEqual(
            response["website_capability_binding"]
            ["commercial_rendering_registry_sha256"],
            response["commercial_rendering_registry"]["sha256"],
        )
        self.assertTrue(response["content_free"])
        self.assertFalse(response["publication_authority"])

        transport = TransformingTransport(
            WSGITransport(self.runtime.http),
            lambda call, result: replace_json(
                result,
                lambda value: value["commercial_profile"].update(
                    {"protected_terms": "embedded-product-list"}
                ),
            ) if call == 2 else result,
        )
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked,
        ) as blocked:
            self.make_client(transport=transport).commercial_profile()
        self.assertEqual(
            blocked.exception.code,
            "source_delivery_submission_dispatch_client.commercial_profile_binding",
        )

    def test_openapi_is_fresh_exact_and_uses_its_own_scope(self):
        response = self.client.openapi()

        expected = HTTP._OPENAPI.build_document(self.client._expected_capabilities)
        self.assertEqual(response["openapi"], expected)
        self.assertEqual(
            response["openapi_sha256"], HTTP._OPENAPI.document_sha256(expected)
        )
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.auth_calls[1]["path"], HTTP.OPENAPI_PATH)
        self.assertEqual(self.auth_calls[1]["scope"], HTTP.SCOPES[HTTP.OPENAPI_PATH])
        self.assertEqual(self.auth_calls[1]["body_sha256"], hashlib.sha256(b"").hexdigest())

    def test_self_rehashed_openapi_substitution_is_rejected(self):
        def mutate(number, result):
            if number == 2:
                def replace(value):
                    value["openapi"]["info"]["description"] = "Altered contract"
                    value["openapi_sha256"] = HTTP._OPENAPI.document_sha256(
                        value["openapi"]
                    )
                return replace_json(result, replace)
            return result

        transport = TransformingTransport(self.transport, mutate)
        client = self.make_client(transport=transport)
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as caught:
            client.openapi()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_client.openapi_binding",
        )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(transport.calls), 2)

    def test_inconsistent_deployment_pins_are_rejected_without_transport(self):
        with self.assertRaises(ValueError):
            self.make_client(digest="0" * 64)
        with self.assertRaises(ValueError):
            self.make_client(public_digest="1" * 64)
        self.assertEqual(self.transport.calls, [])

    def test_self_rehashed_stale_capabilities_are_rejected_before_write(self):
        def mutate(number, result):
            if number == 1:
                def replace(value):
                    capabilities = value["capabilities"]
                    capabilities["semantics"]["accepted_implies_publication"] = True
                    unsigned = {
                        name: content for name, content in capabilities.items()
                        if name != "sha256"
                    }
                    capabilities["sha256"] = hashlib.sha256(
                        CLIENT._canonical(unsigned)
                    ).hexdigest()
                return replace_json(result, replace)
            return result

        transport = TransformingTransport(self.transport, mutate)
        client = self.make_client(transport=transport)
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as caught:
            client.enqueue(cms_support.event())
        self.assertEqual(caught.exception.code, "source_delivery_submission_dispatch_client.capabilities_binding")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_server_generation_change_blocks_client_write_before_persistence(self):
        transport = StaleCapabilityTransport(self.transport)
        client = self.make_client(transport=transport)
        change = cms_support.event()

        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked
        ) as caught:
            client.enqueue(change)

        self.assertEqual(
            caught.exception.code,
            "source_delivery_submission_dispatch_client.http_status",
        )
        self.assertEqual(caught.exception.http_status, 412)
        self.assertEqual(
            caught.exception.remote_error_code,
            "submission_dispatch_http.capabilities_precondition_failed",
        )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(transport.calls), 2)
        with self.assertRaises(
            http_support.RUNTIME.CMSSourceDeliverySubmissionDispatchRuntimeBlocked
        ):
            self.runtime.status("change", change["event_id"])

    def test_cross_tenant_status_and_response_substitution_fail_closed(self):
        accepted = self.client.enqueue(cms_support.event())["status"]
        identity = tuple(accepted[name] for name in (
            "operation", "request_id", "event_id", "site_id", "payload_sha256",
        ))
        self.support.authenticator.site_id = "site-2"
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as tenant:
            self.client.status(*identity)
        self.assertEqual(tenant.exception.code, "source_delivery_submission_dispatch_client.http_status")
        self.assertFalse(tenant.exception.retryable)
        self.assertEqual(tenant.exception.http_status, 404)
        self.assertEqual(
            tenant.exception.remote_error_code,
            "submission_dispatch_http.submission_not_found",
        )

        self.support.authenticator.site_id = "site-1"
        def mutate(number, result):
            if number == 2:
                return replace_json(result, lambda value: value["status"].update({"site_id": "site-2"}))
            return result
        client = self.make_client(transport=TransformingTransport(self.transport, mutate))
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as substituted:
            client.status(*identity)
        self.assertEqual(substituted.exception.code, "source_delivery_submission_dispatch_client.status_binding")

    def test_modified_accepted_website_binding_is_rejected(self):
        queued = self.client.enqueue(cms_support.event())["status"]
        with self.runtime._lock:
            self.runtime._dispatcher.run_once(
                self.runtime._client, "manual-client-test-worker",
                now=self.support.now,
            )
        identity = tuple(queued[name] for name in (
            "operation", "request_id", "event_id", "site_id",
            "payload_sha256",
        ))

        def mutate(number, result):
            if number == 2:
                def change_binding(value):
                    value["status"]["remote_website_capability_binding"][
                        "terminal_receiver_capabilities_sha256"
                    ] = "0" * 64
                return replace_json(result, change_binding)
            return result

        client = self.make_client(
            transport=TransformingTransport(self.transport, mutate),
        )
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked,
        ) as blocked:
            client.status(*identity)
        self.assertEqual(
            blocked.exception.code,
            "source_delivery_submission_dispatch_client.status_binding",
        )

    def test_reserved_authentication_headers_and_invalid_payload_block_locally(self):
        client = self.make_client(headers={"Idempotency-Key": "attacker"})
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as auth:
            client.capabilities()
        self.assertEqual(auth.exception.code, "source_delivery_submission_dispatch_client.authentication")
        calls = len(self.transport.calls)
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as payload:
            self.client.enqueue({"schema": "not-a-source-event"})
        self.assertEqual(payload.exception.code, "source_delivery_submission_dispatch_client.request_invalid")
        self.assertEqual(len(self.transport.calls), calls)

        client = self.make_client(headers={
            HTTP.CAPABILITIES_PRECONDITION_HEADER: "0" * 64,
        })
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked
        ) as precondition:
            client.capabilities()
        self.assertEqual(
            precondition.exception.code,
            "source_delivery_submission_dispatch_client.authentication",
        )

    def test_redirects_are_terminal_and_network_failures_are_retryable_once(self):
        redirect = StaticTransport(CLIENT.HTTPResult(
            307, (("Content-Type", "application/json"),), b"{}",
        ))
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as moved:
            self.make_client(transport=redirect).capabilities()
        self.assertEqual(moved.exception.code, "source_delivery_submission_dispatch_client.redirect")
        self.assertFalse(moved.exception.retryable)
        self.assertEqual(len(redirect.calls), 1)

        failed = StaticTransport(error=OSError("private detail"))
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as network:
            self.make_client(transport=failed).capabilities()
        self.assertEqual(network.exception.code, "source_delivery_submission_dispatch_client.network")
        self.assertTrue(network.exception.retryable)
        self.assertEqual(len(failed.calls), 1)
        self.assertNotIn("private detail", str(network.exception))

    def test_private_fields_and_wrong_generation_are_never_accepted(self):
        mutations = (
            lambda value: value.update({"private": "secret"}),
            lambda value: value.update({"capabilities_sha256": "0" * 64}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                def transform(number, result, mutation=mutation):
                    return replace_json(result, mutation) if number == 2 else result
                client = self.make_client(
                    transport=TransformingTransport(self.transport, transform)
                )
                with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked):
                    client.enqueue(cms_support.event())

    def test_exact_remote_errors_are_visible_without_exposing_private_details(self):
        change = cms_support.event()
        self.client.enqueue(change)
        with self.assertRaises(
            CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked
        ) as conflict:
            self.client.enqueue(change, client_max_attempts=4)

        self.assertEqual(
            conflict.exception.code,
            "source_delivery_submission_dispatch_client.http_status",
        )
        self.assertEqual(conflict.exception.http_status, 409)
        self.assertEqual(
            conflict.exception.remote_error_code,
            "submission_dispatch_http.idempotency_collision",
        )
        self.assertFalse(conflict.exception.retryable)
        self.assertNotIn("site-1", str(conflict.exception))

    def test_unadvertised_or_open_remote_error_envelopes_fail_closed(self):
        self.runtime.stop_worker()
        mutations = (
            lambda value: value.update({
                "error_code": "submission_dispatch_http.forged",
            }),
            lambda value: value.update({"private": "secret"}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                def transform(number, result, mutation=mutation):
                    return replace_json(result, mutation) if number == 2 else result

                client = self.make_client(
                    transport=TransformingTransport(self.transport, transform)
                )
                with self.assertRaises(
                    CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked
                ) as caught:
                    client.enqueue(cms_support.event())
                self.assertEqual(
                    caught.exception.code,
                    "source_delivery_submission_dispatch_client.error_response",
                )
                self.assertTrue(caught.exception.retryable)
                self.assertIsNone(caught.exception.http_status)
                self.assertIsNone(caught.exception.remote_error_code)
                self.assertNotIn("secret", str(caught.exception))

    def test_sidecar_monitor_outage_is_retryable_but_blocked_health_is_valid(self):
        self.runtime.stop_worker()
        readiness = self.client.readiness()
        self.assertEqual(readiness["readiness"]["status"], "not_ready")

        self.support.authenticator.failure = RuntimeError("private outage")
        with self.assertRaises(CLIENT.CMSSourceDeliverySubmissionDispatchClientBlocked) as caught:
            self.client.health()
        self.assertEqual(caught.exception.code, "source_delivery_submission_dispatch_client.http_status")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(
            caught.exception.remote_error_code,
            "submission_dispatch_http.authentication_unavailable",
        )
        self.assertNotIn("private outage", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
