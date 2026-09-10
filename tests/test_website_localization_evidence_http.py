from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path

from test_website_localization_release_coordinator import (
    ApprovalAuthority,
    CMSAuthority,
    COORDINATOR,
    CMS,
    PLANNER,
    QUEUE,
    RELEASE,
    change_event,
    completed_result,
    receipt,
)


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HTTP = load(
    "blun_test_website_localization_evidence_http",
    ROOT / "integrations" / "website_localization_evidence_http.py",
)
RECEIPT_HTTP = load(
    "blun_test_website_localization_receipt_verifier_http_integration",
    ROOT / "integrations" / "website_localization_receipt_verifier_http.py",
)


class FakeTransport:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if isinstance(self.responder, Exception):
            raise self.responder
        return self.responder(body)


def evidence_request():
    source_text = "Build your business with BLUN."
    target_text = "Kasvata yritystäsi BLUN-palvelun avulla."
    return COORDINATOR.QualityEvidenceRequest(
        schema=COORDINATOR.EVIDENCE_REQUEST_SCHEMA,
        request_id="blun-l10n-evidence-" + "a" * 64,
        evidence_revision="native-evidence-1",
        event_id="cms-event-1",
        plan_id="blun-l10n-plan-1",
        job_id="blun-l10n-job-1",
        result_sha256="b" * 64,
        source_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        target_sha256=hashlib.sha256(target_text.encode("utf-8")).hexdigest(),
        source_locale="en-IE",
        target_locale="fi-FI",
        content_type="headline",
        glossary_version="public-glossary-3",
        policy_version="native-web-1",
        provider={
            "id": "customer-llm",
            "model_id": "configured-model",
            "model_version": "2026-09-09",
        },
        software_version="6.43.0-dev",
        source_text=source_text,
        target_text=target_text,
        review_confidence={"target_native": "high", "source_fidelity": "high"},
        quality_profile={"locale": "fi-FI", "version": "fi-native-1", "sha256": "c" * 64},
        commercial_profile=None,
        commercial_review=None,
        human_review_required=False,
        independent_review_required=False,
    )


def response_for(body: bytes, *, mutate=None, evidence_mutate=None):
    request_envelope = json.loads(body.decode("utf-8"))
    request = request_envelope["request"]
    evidence = {
        "schema": COORDINATOR.EVIDENCE_RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "result_sha256": request["result_sha256"],
        "quality_receipt": receipt(
            "quality", request["source_text"], request["target_text"],
            request["target_locale"], request["request_id"],
        ),
        "human_review_receipt": None,
        "independent_model_review": None,
    }
    if evidence_mutate is not None:
        evidence_mutate(evidence)
    envelope = {
        "schema": HTTP.RESPONSE_SCHEMA,
        "request_id": request_envelope["request_id"],
        "request_sha256": request_envelope["request_sha256"],
        "result_sha256": request["result_sha256"],
        "evidence": evidence,
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
    return HTTP.HTTPEvidenceProviderAdapter(
        "https://quality.example.test/v1/evidence",
        authentication_headers or (lambda: {"Authorization": "Bearer private-token"}),
        transport=transport,
        **overrides,
    )


def receipt_response_for(body: bytes):
    request = json.loads(body.decode("utf-8"))
    payload = {
        "schema": RECEIPT_HTTP.RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "binding_sha256": request["binding_sha256"],
        "receipt_sha256": request["receipt_sha256"],
        "verified": True,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return RECEIPT_HTTP.HTTPResult(
        200,
        (("Content-Type", "application/json"), ("Content-Length", str(len(raw)))),
        raw,
    )


class AcceptingVerifier:
    def verify(self, **values):
        return isinstance(values.get("receipt"), str) and values["receipt"].startswith("quality:")


class WebsiteLocalizationEvidenceHTTPTests(unittest.TestCase):
    def test_sends_one_exact_native_unicode_request_with_stable_idempotency(self):
        transport = FakeTransport(response_for)
        provider = adapter(transport)
        request = evidence_request()

        first = provider.obtain(request)
        second = provider.obtain(request)

        self.assertEqual(first, second)
        self.assertEqual(len(transport.calls), 2)
        first_call = transport.calls[0]
        envelope = json.loads(first_call[2].decode("utf-8"))
        expected_hash = hashlib.sha256(HTTP._canonical_json(request.as_payload(), code="x", maximum=HTTP.MAX_REQUEST_BYTES)).hexdigest()
        self.assertEqual(envelope["schema"], HTTP.REQUEST_SCHEMA)
        self.assertEqual(envelope["request"], request.as_payload())
        self.assertEqual(envelope["request_sha256"], expected_hash)
        self.assertEqual(first_call[1]["Idempotency-Key"], request.request_id)
        self.assertEqual(first_call[1]["X-Localization-Evidence-Request-Sha256"], expected_hash)
        self.assertEqual(first_call[3], 60.0)
        self.assertIn("yritystäsi".encode("utf-8"), first_call[2])
        self.assertNotIn(b"\\u00e4", first_call[2])
        self.assertEqual(transport.calls[0][2], transport.calls[1][2])

    def test_http_statuses_have_queue_owned_retry_semantics_and_one_attempt(self):
        for status, expected_code, retryable in (
            (302, "redirect", False),
            (401, "http_status", False),
            (408, "http_status", True),
            (429, "http_status", True),
            (503, "http_status", True),
        ):
            with self.subTest(status=status):
                transport = FakeTransport(lambda _body, status=status: HTTP.HTTPResult(status, (), b""))
                with self.assertRaises(HTTP.HTTPEvidenceProviderFailed) as caught:
                    adapter(transport).obtain(evidence_request())
                self.assertEqual((caught.exception.code, caught.exception.retryable), (expected_code, retryable))
                self.assertEqual(len(transport.calls), 1)

    def test_network_failure_is_content_free_and_retryable(self):
        secret = "do-not-log-this-token"
        transport = FakeTransport(RuntimeError("network included " + secret))
        with self.assertRaises(HTTP.HTTPEvidenceProviderFailed) as caught:
            adapter(transport, lambda: {"Authorization": "Bearer " + secret}).obtain(evidence_request())
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("network", True))
        self.assertNotIn(secret, str(caught.exception))

    def test_authentication_and_endpoint_are_fail_closed_before_transport(self):
        for supplied in (
            {},
            {"Content-Type": "text/plain"},
            {"Authorization": "one", "authorization": "two"},
            {"Authorization": "Bearer token\nX-Evil: yes"},
            {"Bad Header": "value"},
        ):
            with self.subTest(supplied=supplied):
                transport = FakeTransport(response_for)
                with self.assertRaises(HTTP.HTTPEvidenceProviderFailed) as caught:
                    adapter(transport, lambda supplied=supplied: supplied).obtain(evidence_request())
                self.assertEqual(caught.exception.code, "authentication")
                self.assertEqual(transport.calls, [])

        for endpoint in (
            "http://quality.example.test/v1/evidence",
            "https://user:secret@quality.example.test/v1/evidence",
            "https://quality.example.test/v1/evidence?secret=x",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                HTTP.HTTPEvidenceProviderAdapter(endpoint, lambda: {"X-Key": "x"})
        local = HTTP.HTTPEvidenceProviderAdapter(
            "http://127.0.0.1:8042/v1/evidence",
            lambda: {"X-Key": "x"},
            allow_loopback_http=True,
        )
        self.assertEqual(local.endpoint, "http://127.0.0.1:8042/v1/evidence")

    def test_request_mutation_and_wrong_hash_block_before_transport(self):
        for field, value in (
            ("target_text", "Muokattu teksti."),
            ("target_sha256", "0" * 64),
            ("schema", "wrong.schema"),
            ("human_review_required", 1),
        ):
            class BadRequest:
                request_id = evidence_request().request_id

                def as_payload(self):
                    payload = evidence_request().as_payload()
                    payload[field] = value
                    return payload

            with self.subTest(field=field):
                transport = FakeTransport(response_for)
                with self.assertRaises(HTTP.HTTPEvidenceProviderFailed) as caught:
                    adapter(transport).obtain(BadRequest())
                self.assertEqual((caught.exception.code, caught.exception.retryable), ("request_invalid", False))
                self.assertEqual(transport.calls, [])

    def test_commercial_review_scope_is_exact_and_content_free(self):
        base = evidence_request().as_payload()
        base["commercial_profile"] = "translate-native.commercial.v2"
        base["commercial_review"] = {
            "schema": HTTP.COMMERCIAL_REVIEW_SUMMARY_SCHEMA,
            "profile": base["commercial_profile"],
            "status": "review_required",
            "review_required_dimensions": ["tax_status"],
            "evidence_sha256": "d" * 64,
        }
        base["independent_review_required"] = True

        class CommercialRequest:
            request_id = base["request_id"]

            def __init__(self, payload):
                self.payload = payload

            def as_payload(self):
                return self.payload

        transport = FakeTransport(response_for)
        adapter(transport).obtain(CommercialRequest(base))
        sent = json.loads(transport.calls[0][2])["request"]["commercial_review"]
        self.assertEqual(sent["review_required_dimensions"], ["tax_status"])
        self.assertNotIn("VAT", json.dumps(sent))

        for mutation in (
            {"review_required_dimensions": ["private VAT 480"]},
            {"status": "verified"},
            {"schema": "wrong.schema"},
            {"evidence_sha256": "not-a-digest"},
        ):
            payload = json.loads(json.dumps(base))
            payload["commercial_review"].update(mutation)
            invalid_transport = FakeTransport(response_for)
            with self.subTest(mutation=mutation), self.assertRaises(
                HTTP.HTTPEvidenceProviderFailed,
            ) as caught:
                adapter(invalid_transport).obtain(CommercialRequest(payload))
            self.assertEqual(caught.exception.code, "request_invalid")
            self.assertEqual(invalid_transport.calls, [])

    def test_response_must_bind_outer_and_inner_evidence(self):
        cases = (
            lambda body: response_for(body, mutate=lambda value: value.__setitem__("request_sha256", "0" * 64)),
            lambda body: response_for(body, mutate=lambda value: value.__setitem__("result_sha256", "0" * 64)),
            lambda body: response_for(body, evidence_mutate=lambda value: value.__setitem__("request_id", "blun-l10n-evidence-" + "d" * 64)),
            lambda body: response_for(body, evidence_mutate=lambda value: value.__setitem__("extra", True)),
        )
        for responder in cases:
            with self.subTest(responder=responder):
                with self.assertRaises(HTTP.HTTPEvidenceProviderFailed) as caught:
                    adapter(FakeTransport(responder)).obtain(evidence_request())
                self.assertEqual((caught.exception.code, caught.exception.retryable), ("response_binding", False))

    def test_response_parser_rejects_ambiguous_or_unbounded_data(self):
        bodies = (
            b"\xef\xbb\xbf{}",
            b'{"schema":"x","schema":"y"}',
            b'{"value":NaN}',
            b"{}",
        )
        for body in bodies:
            with self.subTest(body=body):
                transport = FakeTransport(lambda _request, body=body: HTTP.HTTPResult(
                    200, (("Content-Type", "application/json"),), body,
                ))
                with self.assertRaises(HTTP.HTTPEvidenceProviderFailed) as caught:
                    adapter(transport).obtain(evidence_request())
                self.assertIn(caught.exception.code, {"response_json", "response_binding"})

        valid = response_for(HTTP._canonical_json({
            "schema": HTTP.REQUEST_SCHEMA,
            "request_id": evidence_request().request_id,
            "request_sha256": hashlib.sha256(HTTP._canonical_json(evidence_request().as_payload(), code="x", maximum=HTTP.MAX_REQUEST_BYTES)).hexdigest(),
            "request": evidence_request().as_payload(),
        }, code="x", maximum=HTTP.MAX_REQUEST_BYTES))
        for result in (
            HTTP.HTTPResult(200, (("Content-Type", "text/html"),), valid.body),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"), ("Content-Length", "1")), valid.body),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"), ("content-type", "application/json")), valid.body),
            HTTP.HTTPResult(200, (("Content-Type", "application/json"),), b"x" * (HTTP.MAX_RESPONSE_BYTES + 1)),
        ):
            with self.subTest(headers=result.headers, size=len(result.body)):
                with self.assertRaises(HTTP.HTTPEvidenceProviderFailed):
                    adapter(FakeTransport(lambda _body, result=result: result)).obtain(evidence_request())

    def test_durable_coordinator_owns_retry_and_then_releases_one_locale(self):
        connections = [sqlite3.connect(":memory:") for _ in range(4)]
        try:
            queue = QUEUE.LocalizationQueue(connections[0])
            store = RELEASE.LocalizationReleaseStore(connections[1], queue)
            bridge = CMS.WebsiteLocalizationCMSBridge(connections[2], queue, store)
            evidence_state = COORDINATOR.QualityEvidenceStateStore(connections[3])
            event_authority = CMSAuthority(b"event-key")
            approval_authority = ApprovalAuthority()
            publication_authority = CMSAuthority(b"publication-key")
            event = change_event(targets=("fi-FI",))
            plan = PLANNER.plan_from_mapping(event["localization"])
            signature = event_authority.sign(CMS._canonical_json(event).encode("utf-8"))
            bridge.ingest_change(event, signature, event_authority, now=100)
            claim = queue.claim("worker", now=110, lease_seconds=30)
            self.assertIsNotNone(claim)
            queue.complete(
                claim,
                completed_result(plan.jobs[0], "Kasvata yritystäsi BLUN-palvelun avulla."),
                now=111,
            )

            failure_transport = FakeTransport(lambda _body: HTTP.HTTPResult(503, (), b""))
            failure_provider = adapter(failure_transport)
            receipt_transport = FakeTransport(receipt_response_for)
            receipt_verifier = RECEIPT_HTTP.HTTPReceiptVerifierAdapter(
                "https://quality.example.test/v1/receipts/verify",
                lambda: {"Authorization": "Bearer verifier-token"},
                transport=receipt_transport,
            )
            arguments = dict(
                evidence_revision="native-evidence-http-1",
                approval_ttl_seconds=1000,
                evidence_state=evidence_state,
                quality_verifier=receipt_verifier,
                approval_authority=approval_authority,
                publication_authority=publication_authority,
                evidence_worker_id="quality-http-worker",
            )
            with self.assertRaises(COORDINATOR.LocalizationReleaseCoordinatorBlocked) as caught:
                COORDINATOR.run_next_release(
                    bridge, event["event_id"], event_authority, failure_provider,
                    now=200, **arguments,
                )
            self.assertEqual((caught.exception.code, caught.exception.retryable), ("evidence.http_status", True))
            self.assertEqual(len(failure_transport.calls), 1)
            self.assertEqual(receipt_transport.calls, [])
            state = evidence_state.statuses(event["event_id"])[0]
            self.assertEqual((state.status, state.attempts, state.next_attempt_at), ("retry_wait", 1, 205.0))

            waiting = COORDINATOR.run_next_release(
                bridge, event["event_id"], event_authority, failure_provider,
                now=204, **arguments,
            )
            self.assertEqual(waiting.status, "waiting")
            self.assertEqual(len(failure_transport.calls), 1)

            success_transport = FakeTransport(response_for)
            outcome = COORDINATOR.run_next_release(
                bridge, event["event_id"], event_authority, adapter(success_transport),
                now=205, **arguments,
            )
            self.assertEqual((outcome.status, outcome.target_locale), ("delivery_ready", "fi-FI"))
            self.assertEqual(len(success_transport.calls), 1)
            self.assertEqual(len(receipt_transport.calls), 1)
            receipt_request = json.loads(receipt_transport.calls[0][2])
            self.assertEqual(receipt_request["binding"]["target_locale"], "fi-FI")
            self.assertEqual(receipt_request["binding"]["review_kind"], "quality")
            state = evidence_state.statuses(event["event_id"])[0]
            self.assertEqual((state.status, state.attempts, state.last_error_code), ("succeeded", 2, None))
        finally:
            for connection in reversed(connections):
                connection.close()


if __name__ == "__main__":
    unittest.main()
