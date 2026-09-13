from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import io
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RECEIVER = load(
    "blun_test_website_localization_cms_receiver",
    ROOT / "integrations" / "website_localization_cms_receiver.py",
)
HTTP = load(
    "blun_test_website_localization_cms_receiver_http",
    ROOT / "integrations" / "website_localization_cms_http.py",
)
CMS = RECEIVER._CMS


class Authority:
    def __init__(self, key: bytes, key_id: str):
        self.key = key
        self.key_id = key_id

    def sign(self, payload):
        return SimpleNamespace(
            algorithm="hmac-sha256-test",
            key_id=self.key_id,
            signature=hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        return (
            signature.algorithm == "hmac-sha256-test"
            and signature.key_id == self.key_id
            and hmac.compare_digest(
                signature.signature,
                hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
            )
        )


def release_evidence(
    target: str,
    *,
    locale="fi-FI",
    commercial=True,
    review_required=False,
    resolution_method="independent_model",
):
    target_sha256 = hashlib.sha256(target.encode("utf-8")).hexdigest()
    review = None
    profile = None
    quality_profile = None
    resolution = None
    content_type = "cta"
    if commercial:
        content_type = "commercial"
        profile = CMS._PLANNER.COMMERCIAL_PROFILE
        canonical = CMS._PLANNER.commercial_quality_profile_for(locale)
        quality_profile = {
            "profile": canonical["commercial_profile"],
            "version": canonical["version"],
            "sha256": canonical["sha256"],
        }
        review = {
            "schema": CMS._RELEASE._WORKER._COMMERCIAL.REVIEW_SUMMARY_SCHEMA,
            "profile": profile,
            "status": "review_required" if review_required else "verified",
            "review_required_dimensions": (
                ["tax_status", "cancellation"] if review_required else []
            ),
            "evidence_sha256": "5" * 64,
        }
        if review_required:
            resolution = {
                "schema": CMS._RELEASE.COMMERCIAL_REVIEW_RESOLUTION_SCHEMA,
                "status": "resolved",
                "reviewed_dimensions": ["tax_status", "cancellation"],
                "method": resolution_method,
                "receipt_sha256": "7" * 64,
                "provider": (
                    {
                        "id": "independent-reviewer",
                        "model_id": "review-model",
                        "model_version": "2026-09-12",
                    }
                    if resolution_method == "independent_model" else None
                ),
            }
    return {
        "schema": CMS._RELEASE.PUBLICATION_EVIDENCE_SCHEMA,
        "job_id": "blun-l10n-job-" + "1" * 64,
        "target_locale": locale,
        "target_sha256": target_sha256,
        "approval_id": "blun-l10n-approval-" + "2" * 64,
        "content_type": content_type,
        "result_sha256": "3" * 64,
        "approval_sha256": "4" * 64,
        "quality_receipt_sha256": "6" * 64,
        "commercial_profile": profile,
        "commercial_quality_profile": quality_profile,
        "commercial_review": review,
        "commercial_review_resolution": resolution,
    }


def publication_payload(*, expires_at=2000):
    target = "Aloita maksutta – hinta 480 € vuodessa."
    evidence = release_evidence(target)
    unsigned = {
        "schema": CMS.PUBLICATION_SCHEMA,
        "event_id": "cms-event-201",
        "site_id": "public-site",
        "website_version": "website-201",
        "plan_id": "blun-l10n-plan-" + "7" * 64,
        "source_id": "homepage.pricing",
        "source_revision": "cms-201",
        "source_sequence": 201,
        "source_sha256": "8" * 64,
        "localizations": [{
            "locale": "fi-FI",
            "target_text": target,
            "target_sha256": evidence["target_sha256"],
            "approval_id": evidence["approval_id"],
            "approval_expires_at": expires_at,
            "release_evidence": evidence,
        }],
    }
    delivery_id = "blun-cms-delivery-" + hashlib.sha256(
        RECEIVER._canonical_json(unsigned, maximum=RECEIVER.MAX_REQUEST_BYTES)
    ).hexdigest()
    return {**unsigned, "delivery_id": delivery_id}


def rebind_publication(payload):
    unsigned = {key: value for key, value in payload.items() if key != "delivery_id"}
    payload["delivery_id"] = "blun-cms-delivery-" + hashlib.sha256(
        RECEIVER._canonical_json(unsigned, maximum=RECEIVER.MAX_REQUEST_BYTES)
    ).hexdigest()
    return payload


def expectation(
    payload,
    *,
    required_locales=None,
    content_type="commercial",
    commercial_profile=CMS._PLANNER.COMMERCIAL_PROFILE,
):
    return RECEIVER.PublicationExpectation(
        event_id=payload["event_id"],
        site_id=payload["site_id"],
        website_version=payload["website_version"],
        plan_id=payload["plan_id"],
        source_id=payload["source_id"],
        source_revision=payload["source_revision"],
        source_sequence=payload["source_sequence"],
        source_sha256=payload["source_sha256"],
        required_locales=(
            tuple(item["locale"] for item in payload["localizations"])
            if required_locales is None
            else tuple(required_locales)
        ),
        content_type=content_type,
        commercial_profile=commercial_profile,
    )


def tombstone_payload():
    unsigned = {
        "schema": CMS.TOMBSTONE_DELIVERY_SCHEMA,
        "tombstone_id": "cms-tombstone-201",
        "event_id": "cms-event-201",
        "site_id": "public-site",
        "website_version": "website-201",
        "plan_id": "blun-l10n-plan-" + "7" * 64,
        "source_id": "homepage.pricing",
        "source_sequence": 201,
        "publication_delivery_id": "blun-cms-delivery-" + "8" * 64,
        "publication_payload_sha256": "9" * 64,
        "locales": ["fi-FI", "mt-MT"],
    }
    delivery_id = "blun-cms-tombstone-" + hashlib.sha256(
        RECEIVER._canonical_json(unsigned, maximum=RECEIVER.MAX_REQUEST_BYTES)
    ).hexdigest()
    return {**unsigned, "delivery_id": delivery_id}


def rebind_tombstone(payload):
    unsigned = {key: value for key, value in payload.items() if key != "delivery_id"}
    payload["delivery_id"] = "blun-cms-tombstone-" + hashlib.sha256(
        RECEIVER._canonical_json(unsigned, maximum=RECEIVER.MAX_REQUEST_BYTES)
    ).hexdigest()
    return payload


def tombstone_expectation(payload, **overrides):
    values = {
        "tombstone_id": payload["tombstone_id"],
        "event_id": payload["event_id"],
        "site_id": payload["site_id"],
        "website_version": payload["website_version"],
        "plan_id": payload["plan_id"],
        "source_id": payload["source_id"],
        "source_sequence": payload["source_sequence"],
        "publication_delivery_id": payload["publication_delivery_id"],
        "publication_payload_sha256": payload["publication_payload_sha256"],
        "locales": tuple(payload["locales"]),
    }
    values.update(overrides)
    return RECEIVER.TombstoneExpectation(**values)


def wire(payload, authority):
    payload_bytes = RECEIVER._canonical_json(
        payload, maximum=RECEIVER.MAX_REQUEST_BYTES,
    )
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    signature = authority.sign(payload_bytes)
    envelope = {
        "schema": CMS.PUBLICATION_HTTP_REQUEST_SCHEMA,
        "payload_sha256": payload_sha256,
        "publication": payload,
        "signature": {
            "algorithm": signature.algorithm,
            "key_id": signature.key_id,
            "signature": signature.signature,
        },
    }
    body = RECEIVER._canonical_json(envelope, maximum=RECEIVER.MAX_REQUEST_BYTES)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Idempotency-Key": payload["delivery_id"],
        "X-Localization-Delivery-Id": payload["delivery_id"],
        "X-Localization-Payload-Sha256": payload_sha256,
    }
    return body, headers, payload_sha256, signature


def wire_tombstone(payload, authority):
    payload_bytes = RECEIVER._canonical_json(
        payload, maximum=RECEIVER.MAX_REQUEST_BYTES,
    )
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    signature = authority.sign(payload_bytes)
    envelope = {
        "schema": CMS.TOMBSTONE_HTTP_REQUEST_SCHEMA,
        "payload_sha256": payload_sha256,
        "tombstone": payload,
        "signature": {
            "algorithm": signature.algorithm,
            "key_id": signature.key_id,
            "signature": signature.signature,
        },
    }
    body = RECEIVER._canonical_json(envelope, maximum=RECEIVER.MAX_REQUEST_BYTES)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Idempotency-Key": payload["delivery_id"],
        "X-Localization-Delivery-Id": payload["delivery_id"],
        "X-Localization-Payload-Sha256": payload_sha256,
    }
    return body, headers, payload_sha256, signature


def wire_health(probe_id, contract_sha256, *, authorization="Bearer secret"):
    body = RECEIVER._canonical_json({
        "schema": CMS.PUBLICATION_HEALTH_HTTP_REQUEST_SCHEMA,
        "probe": {
            "schema": CMS.PUBLICATION_HEALTH_SCHEMA,
            "probe_id": probe_id,
            "contract_sha256": contract_sha256,
        },
    }, maximum=RECEIVER.MAX_REQUEST_BYTES)
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Idempotency-Key": probe_id,
        "X-Localization-Probe-Id": probe_id,
        "X-Localization-Contract-Sha256": contract_sha256,
    }
    return body, headers


def request(payload, authority):
    _, _, payload_sha256, signature = wire(payload, authority)
    return SimpleNamespace(
        delivery_id=payload["delivery_id"],
        payload=payload,
        payload_sha256=payload_sha256,
        signature=signature,
    )


def tombstone_request(payload, authority):
    _, _, payload_sha256, signature = wire_tombstone(payload, authority)
    return SimpleNamespace(
        delivery_id=payload["delivery_id"],
        payload=payload,
        payload_sha256=payload_sha256,
        signature=signature,
    )


class ReceiverTransport:
    def __init__(
        self, publication_authority, acknowledgement_authority, expectation, commit,
    ):
        self.publication_authority = publication_authority
        self.acknowledgement_authority = acknowledgement_authority
        self.expectation = expectation
        self.commit = commit
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        try:
            response = RECEIVER.receive_publication(
                body,
                tuple(headers.items()),
                self.publication_authority,
                self.acknowledgement_authority,
                self.expectation,
                self.commit,
                now=1000,
            )
            return HTTP.HTTPResult(response.status, response.headers, response.body)
        except RECEIVER.CMSReceiverBlocked as error:
            return HTTP.HTTPResult(
                error.http_status,
                (("Content-Type", "application/json"),),
                b"{}",
            )


class TombstoneReceiverTransport:
    def __init__(
        self, publication_authority, acknowledgement_authority, expectation, delete,
    ):
        self.publication_authority = publication_authority
        self.acknowledgement_authority = acknowledgement_authority
        self.expectation = expectation
        self.delete = delete
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        try:
            response = RECEIVER.receive_tombstone(
                body,
                tuple(headers.items()),
                self.publication_authority,
                self.acknowledgement_authority,
                self.expectation,
                self.delete,
            )
            return HTTP.HTTPResult(response.status, response.headers, response.body)
        except RECEIVER.CMSReceiverBlocked as error:
            return HTTP.HTTPResult(
                error.http_status,
                (("Content-Type", "application/json"),),
                b"{}",
            )


class HealthReceiverTransport:
    def __init__(self, acknowledgement_authority, contract_sha256, check):
        self.acknowledgement_authority = acknowledgement_authority
        self.contract_sha256 = contract_sha256
        self.check = check
        self.calls = []

    @staticmethod
    def authenticate(headers):
        return headers.get("authorization") == "Bearer secret"

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        try:
            response = RECEIVER.receive_health(
                body,
                tuple(headers.items()),
                self.authenticate,
                self.acknowledgement_authority,
                self.check,
                contract_sha256=self.contract_sha256,
            )
            return HTTP.HTTPResult(response.status, response.headers, response.body)
        except RECEIVER.CMSReceiverBlocked as error:
            return HTTP.HTTPResult(
                error.http_status,
                (("Content-Type", "application/json"),),
                b"{}",
            )


def wsgi_environ(
    body,
    headers,
    *,
    path=RECEIVER.RECEIVER_PATH,
    method="POST",
    scheme="https",
    query="",
):
    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": method,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(body),
    }
    for name, value in headers.items():
        normalized = name.upper().replace("-", "_")
        if normalized == "CONTENT_TYPE":
            environ["CONTENT_TYPE"] = value
        elif normalized == "CONTENT_LENGTH":
            environ["CONTENT_LENGTH"] = value
        else:
            environ["HTTP_" + normalized] = value
    return environ


def call_wsgi(application, environ):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = tuple(headers)

    chunks = application(environ, start_response)
    return captured["status"], dict(captured["headers"]), b"".join(chunks)


class WSGIReceiverTransport:
    def __init__(self, application):
        self.application = application
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        parsed = urlsplit(url)
        environ = wsgi_environ(
            body, dict(headers), path=parsed.path, scheme=parsed.scheme,
            query=parsed.query,
        )
        status, response_headers, response_body = call_wsgi(
            self.application, environ,
        )
        return HTTP.HTTPResult(
            int(status.split(" ", 1)[0]),
            tuple(response_headers.items()),
            response_body,
        )


class CMSPublicationReceiverTests(unittest.TestCase):
    def setUp(self):
        self.publication_authority = Authority(b"publication-key", "publication-key-1")
        self.acknowledgement_authority = Authority(b"ack-key", "ack-key-1")
        self.commits = []

    def commit(self, publication):
        self.commits.append(publication)
        return {
            "delivery_id": publication.delivery_id,
            "payload_sha256": publication.payload_sha256,
            "status": "committed",
        }

    def test_sender_to_receiver_round_trip_commits_then_accepts(self):
        payload = publication_payload()
        transport = ReceiverTransport(
            self.publication_authority,
            self.acknowledgement_authority,
            expectation(payload),
            self.commit,
        )
        publisher = HTTP.HTTPPublisherAdapter(
            "https://cms.example.test/localizations",
            lambda: {"Authorization": "Bearer secret"},
            self.acknowledgement_authority,
            transport=transport,
        )

        acknowledgement = publisher.publish(request(payload, self.publication_authority))

        self.assertEqual(acknowledgement["status"], "accepted")
        self.assertEqual(len(self.commits), 1)
        committed = self.commits[0]
        self.assertEqual(committed.payload, payload)
        evidence = committed.payload["localizations"][0]["release_evidence"]
        self.assertEqual(evidence["commercial_profile"], CMS._PLANNER.COMMERCIAL_PROFILE)
        canonical = CMS._PLANNER.commercial_quality_profile_for("fi-FI")
        self.assertEqual(evidence["commercial_quality_profile"], {
            "profile": canonical["commercial_profile"],
            "version": canonical["version"],
            "sha256": canonical["sha256"],
        })
        self.assertEqual(evidence["commercial_review"]["status"], "verified")
        self.assertIsNone(evidence["commercial_review_resolution"])

    def test_targeted_commercial_resolution_reaches_commit_content_free(self):
        for method in ("independent_model", "qualified_human"):
            payload = publication_payload()
            item = payload["localizations"][0]
            item["release_evidence"] = release_evidence(
                item["target_text"], review_required=True,
                resolution_method=method,
            )
            item["target_sha256"] = item["release_evidence"]["target_sha256"]
            item["approval_id"] = item["release_evidence"]["approval_id"]
            rebind_publication(payload)
            body, headers, _, _ = wire(payload, self.publication_authority)

            response = RECEIVER.receive_publication(
                body, headers, self.publication_authority,
                self.acknowledgement_authority, expectation(payload),
                self.commit, now=1000,
            )

            with self.subTest(method=method):
                self.assertEqual(
                    json.loads(response.body)["acknowledgement"]["status"],
                    "accepted",
                )
                resolution = self.commits[-1].payload["localizations"][0][
                    "release_evidence"
                ]["commercial_review_resolution"]
                self.assertEqual(resolution["method"], method)
                self.assertEqual(
                    resolution["reviewed_dimensions"],
                    ["tax_status", "cancellation"],
                )
                self.assertNotIn("receipt", resolution)

    def test_invalid_targeted_commercial_resolution_never_reaches_commit(self):
        mutations = (
            lambda value: value["localizations"][0]["release_evidence"].update(
                commercial_review_resolution=None,
            ),
            lambda value: value["localizations"][0]["release_evidence"]
            ["commercial_review_resolution"].update(
                reviewed_dimensions=["cancellation"],
            ),
            lambda value: value["localizations"][0]["release_evidence"]
            ["commercial_review_resolution"].update(
                method="qualified_human",
            ),
            lambda value: value["localizations"][0]["release_evidence"]
            ["commercial_review_resolution"].update(
                receipt_sha256="8" * 63,
            ),
        )
        for mutation in mutations:
            payload = publication_payload()
            item = payload["localizations"][0]
            item["release_evidence"] = release_evidence(
                item["target_text"], review_required=True,
            )
            expected = expectation(payload)
            mutation(payload)
            rebind_publication(payload)
            body, headers, _, _ = wire(payload, self.publication_authority)
            before = len(self.commits)
            with self.subTest(mutation=mutation), self.assertRaises(
                RECEIVER.CMSReceiverBlocked,
            ):
                RECEIVER.receive_publication(
                    body, headers, self.publication_authority,
                    self.acknowledgement_authority, expected,
                    self.commit, now=1000,
                )
            self.assertEqual(len(self.commits), before)

    def test_every_eu_locale_uses_its_exact_commercial_quality_binding(self):
        for locale in sorted(
            profile.locale for profile in CMS._PLANNER.EU_OFFICIAL_LOCALES
        ):
            payload = publication_payload()
            item = payload["localizations"][0]
            item["locale"] = locale
            item["release_evidence"] = release_evidence(
                item["target_text"], locale=locale,
            )
            item["target_sha256"] = item["release_evidence"]["target_sha256"]
            item["approval_id"] = item["release_evidence"]["approval_id"]
            rebind_publication(payload)
            body, headers, _, _ = wire(payload, self.publication_authority)

            response = RECEIVER.receive_publication(
                body, headers, self.publication_authority,
                self.acknowledgement_authority, expectation(payload),
                self.commit, now=1000,
            )

            with self.subTest(locale=locale):
                self.assertEqual(
                    json.loads(response.body)["acknowledgement"]["status"],
                    "accepted",
                )

    def test_exact_replay_uses_the_same_host_idempotency_binding(self):
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)

        first = RECEIVER.receive_publication(
            body, headers, self.publication_authority,
            self.acknowledgement_authority, expectation(payload), self.commit,
            now=1000,
        )
        second = RECEIVER.receive_publication(
            body, headers, self.publication_authority,
            self.acknowledgement_authority, expectation(payload), self.commit,
            now=1000,
        )

        self.assertEqual(first.body, second.body)
        self.assertEqual(
            [(item.delivery_id, item.payload_sha256) for item in self.commits],
            [(payload["delivery_id"], self.commits[0].payload_sha256)] * 2,
        )

    def test_invalid_publication_never_reaches_commit(self):
        mutations = (
            lambda value: value["localizations"][0]["release_evidence"].pop(
                "quality_receipt_sha256"
            ),
            lambda value: value["localizations"][0]["release_evidence"].update(
                target_locale="mt-MT"
            ),
            lambda value: value["localizations"][0].update(target_sha256="9" * 64),
            lambda value: value["localizations"].append(
                copy.deepcopy(value["localizations"][0])
            ),
            lambda value: value["localizations"][0].update(approval_expires_at=999),
        )
        for mutation in mutations:
            payload = publication_payload()
            expected = expectation(payload)
            mutation(payload)
            rebind_publication(payload)
            body, headers, _, _ = wire(payload, self.publication_authority)
            with self.subTest(mutation=mutation), self.assertRaises(
                RECEIVER.CMSReceiverBlocked,
            ):
                RECEIVER.receive_publication(
                    body, headers, self.publication_authority,
                    self.acknowledgement_authority, expected,
                    self.commit, now=1000,
                )
        self.assertEqual(self.commits, [])

    def test_expected_locale_bundle_source_and_release_scope_are_exact(self):
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        base = expectation(payload)
        cases = (
            (
                replace(base, required_locales=("fi-FI", "mt-MT")),
                "receiver.bundle_incomplete",
            ),
            (
                replace(base, source_revision="cms-expected-202"),
                "receiver.source_binding",
            ),
            (
                replace(base, commercial_profile="commercial-offer-v2"),
                "receiver.release_scope",
            ),
        )

        for expected, code in cases:
            with self.subTest(code=code), self.assertRaises(
                RECEIVER.CMSReceiverBlocked,
            ) as caught:
                RECEIVER.receive_publication(
                    body, headers, self.publication_authority,
                    self.acknowledgement_authority, expected, self.commit,
                    now=1000,
                )
            self.assertEqual(caught.exception.code, code)

    def test_locale_commercial_quality_profile_drift_blocks_before_commit(self):
        mutations = (
            (
                lambda value: value["localizations"][0]["release_evidence"]
                ["commercial_quality_profile"].update(
                    version="commercial-eu-fi-FI-old"
                ),
                "receiver.release_scope",
            ),
            (
                lambda value: value["localizations"][0]["release_evidence"]
                ["commercial_quality_profile"].update(sha256="9" * 64),
                "receiver.release_scope",
            ),
            (
                lambda value: value["localizations"][0]["release_evidence"]
                ["commercial_quality_profile"].update(
                    profile="commercial-offer-v2"
                ),
                "receiver.localization_invalid",
            ),
        )
        for mutation, code in mutations:
            payload = publication_payload()
            expected = expectation(payload)
            mutation(payload)
            rebind_publication(payload)
            body, headers, _, _ = wire(payload, self.publication_authority)
            with self.subTest(mutation=mutation), self.assertRaises(
                RECEIVER.CMSReceiverBlocked,
            ) as caught:
                RECEIVER.receive_publication(
                    body, headers, self.publication_authority,
                    self.acknowledgement_authority, expected, self.commit,
                    now=1000,
                )
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.commits, [])

    def test_invalid_host_expectation_never_reaches_commit(self):
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        invalid = replace(expectation(payload), required_locales=("fi-FI", "fi-FI"))

        with self.assertRaises(ValueError):
            RECEIVER.receive_publication(
                body, headers, self.publication_authority,
                self.acknowledgement_authority, invalid, self.commit, now=1000,
            )
        self.assertEqual(self.commits, [])

    def test_delivery_id_must_bind_the_complete_sorted_bundle(self):
        payload = publication_payload()
        payload["source_revision"] = "cms-202"
        body, headers, _, _ = wire(payload, self.publication_authority)

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.verify_publication_request(
                body, headers, self.publication_authority, expectation(payload),
                now=1000,
            )
        self.assertEqual(caught.exception.code, "receiver.delivery_binding")

    def test_header_signature_and_payload_bindings_fail_closed(self):
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        cases = []
        bad_header = dict(headers)
        bad_header["X-Localization-Payload-Sha256"] = "0" * 64
        cases.append((body, bad_header))
        duplicate_headers = tuple(headers.items()) + (
            ("x-localization-delivery-id", payload["delivery_id"]),
        )
        cases.append((body, duplicate_headers))
        bad_signature = json.loads(body)
        bad_signature["signature"]["signature"] = "0" * 64
        cases.append((
            RECEIVER._canonical_json(
                bad_signature, maximum=RECEIVER.MAX_REQUEST_BYTES,
            ),
            headers,
        ))
        bad_payload_hash = json.loads(body)
        bad_payload_hash["payload_sha256"] = "0" * 64
        cases.append((
            RECEIVER._canonical_json(
                bad_payload_hash, maximum=RECEIVER.MAX_REQUEST_BYTES,
            ),
            headers,
        ))

        for candidate_body, candidate_headers in cases:
            with self.subTest(), self.assertRaises(RECEIVER.CMSReceiverBlocked):
                RECEIVER.verify_publication_request(
                    candidate_body,
                    candidate_headers,
                    self.publication_authority,
                    expectation(payload),
                    now=1000,
                )
        self.assertEqual(self.commits, [])

    def test_parser_rejects_noncanonical_duplicate_bom_and_nonfinite_json(self):
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        candidates = (
            b" " + body,
            b"\xef\xbb\xbf" + body,
            b'{"schema":"x","schema":"y"}',
            b'{"value":NaN}',
        )
        for candidate in candidates:
            adjusted = dict(headers, **{"Content-Length": str(len(candidate))})
            with self.subTest(candidate=candidate[:20]), self.assertRaises(
                RECEIVER.CMSReceiverBlocked,
            ):
                RECEIVER.verify_publication_request(
                    candidate, adjusted, self.publication_authority,
                    expectation(payload), now=1000,
                )

    def test_commit_must_confirm_exact_binding_before_acknowledgement(self):
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        calls = []

        def wrong(publication):
            calls.append(publication)
            return {
                "delivery_id": publication.delivery_id,
                "payload_sha256": "0" * 64,
                "status": "committed",
            }

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_publication(
                body, headers, self.publication_authority,
                self.acknowledgement_authority, expectation(payload), wrong,
                now=1000,
            )
        self.assertEqual(caught.exception.code, "receiver.commit_unconfirmed")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(len(calls), 1)

    def test_private_commit_failure_is_reduced_to_stable_retryable_error(self):
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)

        def failed(_):
            raise RuntimeError("private CMS database detail")

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_publication(
                body, headers, self.publication_authority,
                self.acknowledgement_authority, expectation(payload), failed,
                now=1000,
            )
        self.assertEqual(caught.exception.code, "receiver.commit_failed")
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(caught.exception.retryable)

    def test_acknowledgement_signing_failure_occurs_after_commit(self):
        class BrokenAuthority:
            def sign(self, _):
                raise RuntimeError("private signing detail")

            def verify(self, *_):
                return False

        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_publication(
                body, headers, self.publication_authority,
                BrokenAuthority(), expectation(payload), self.commit, now=1000,
            )
        self.assertEqual(caught.exception.code, "receiver.acknowledgement_signing")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(len(self.commits), 1)

    def test_malformed_acknowledgement_signature_is_retryable_after_commit(self):
        class MalformedAuthority:
            def sign(self, _):
                return SimpleNamespace(
                    algorithm="hmac-sha256-test",
                    key_id="ack-key-1",
                    signature="contains whitespace",
                )

            def verify(self, *_):
                return False

        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_publication(
                body, headers, self.publication_authority,
                MalformedAuthority(), expectation(payload), self.commit, now=1000,
            )
        self.assertEqual(caught.exception.code, "receiver.acknowledgement_signing")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(len(self.commits), 1)


class CMSTombstoneReceiverTests(unittest.TestCase):
    def setUp(self):
        self.publication_authority = Authority(b"publication-key", "publication-key-1")
        self.acknowledgement_authority = Authority(b"ack-key", "ack-key-1")
        self.deletions = []

    def delete(self, tombstone):
        self.deletions.append(tombstone)
        return {
            "delivery_id": tombstone.delivery_id,
            "payload_sha256": tombstone.payload_sha256,
            "status": "deleted",
        }

    def test_sender_to_receiver_deletes_then_returns_signed_acknowledgement(self):
        payload = tombstone_payload()
        transport = TombstoneReceiverTransport(
            self.publication_authority,
            self.acknowledgement_authority,
            tombstone_expectation(payload),
            self.delete,
        )
        publisher = HTTP.HTTPPublisherAdapter(
            "https://cms.example.test/localizations",
            lambda: {"Authorization": "Bearer secret"},
            self.acknowledgement_authority,
            transport=transport,
        )

        acknowledgement = publisher.publish(
            tombstone_request(payload, self.publication_authority),
        )

        self.assertEqual(acknowledgement, {
            "schema": CMS.TOMBSTONE_ACK_SCHEMA,
            "delivery_id": payload["delivery_id"],
            "payload_sha256": self.deletions[0].payload_sha256,
            "status": "deleted",
        })
        self.assertEqual(len(self.deletions), 1)
        self.assertEqual(self.deletions[0].payload, payload)

    def test_exact_replay_reuses_the_same_delete_binding(self):
        payload = tombstone_payload()
        body, headers, _, _ = wire_tombstone(payload, self.publication_authority)
        expected = tombstone_expectation(payload)

        first = RECEIVER.receive_tombstone(
            body, headers, self.publication_authority,
            self.acknowledgement_authority, expected, self.delete,
        )
        second = RECEIVER.receive_tombstone(
            body, headers, self.publication_authority,
            self.acknowledgement_authority, expected, self.delete,
        )

        self.assertEqual(first.body, second.body)
        self.assertEqual(
            [(item.delivery_id, item.payload_sha256) for item in self.deletions],
            [(payload["delivery_id"], self.deletions[0].payload_sha256)] * 2,
        )

    def test_expected_publication_source_and_complete_locale_set_are_exact(self):
        original = tombstone_payload()
        expected = tombstone_expectation(original)
        mutations = (
            lambda value: value.update(locales=["fi-FI"]),
            lambda value: value.update(source_sequence=202),
            lambda value: value.update(publication_payload_sha256="0" * 64),
            lambda value: value.update(
                publication_delivery_id="blun-cms-delivery-" + "1" * 64,
            ),
        )

        for mutation in mutations:
            payload = copy.deepcopy(original)
            mutation(payload)
            rebind_tombstone(payload)
            body, headers, _, _ = wire_tombstone(
                payload, self.publication_authority,
            )
            with self.subTest(mutation=mutation), self.assertRaises(
                RECEIVER.CMSReceiverBlocked,
            ) as caught:
                RECEIVER.receive_tombstone(
                    body, headers, self.publication_authority,
                    self.acknowledgement_authority, expected, self.delete,
                )
            self.assertEqual(caught.exception.code, "receiver.tombstone_binding")
        self.assertEqual(self.deletions, [])

    def test_malformed_tombstone_headers_and_signature_never_delete(self):
        payload = tombstone_payload()
        expected = tombstone_expectation(payload)
        body, headers, _, _ = wire_tombstone(payload, self.publication_authority)
        duplicate_locale = copy.deepcopy(payload)
        duplicate_locale["locales"].append("mt-MT")
        duplicate_body, duplicate_headers, _, _ = wire_tombstone(
            duplicate_locale, self.publication_authority,
        )
        bad_header = dict(headers)
        bad_header["X-Localization-Payload-Sha256"] = "0" * 64
        bad_signature = json.loads(body)
        bad_signature["signature"]["signature"] = "0" * 64
        cases = (
            (duplicate_body, duplicate_headers),
            (body, bad_header),
            (
                RECEIVER._canonical_json(
                    bad_signature, maximum=RECEIVER.MAX_REQUEST_BYTES,
                ),
                headers,
            ),
        )

        for candidate_body, candidate_headers in cases:
            with self.subTest(), self.assertRaises(RECEIVER.CMSReceiverBlocked):
                RECEIVER.receive_tombstone(
                    candidate_body, candidate_headers, self.publication_authority,
                    self.acknowledgement_authority, expected, self.delete,
                )
        self.assertEqual(self.deletions, [])

    def test_delete_must_confirm_exact_binding_before_acknowledgement(self):
        payload = tombstone_payload()
        body, headers, _, _ = wire_tombstone(payload, self.publication_authority)
        calls = []

        def wrong(tombstone):
            calls.append(tombstone)
            return {
                "delivery_id": tombstone.delivery_id,
                "payload_sha256": "0" * 64,
                "status": "deleted",
            }

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_tombstone(
                body, headers, self.publication_authority,
                self.acknowledgement_authority, tombstone_expectation(payload),
                wrong,
            )
        self.assertEqual(caught.exception.code, "receiver.delete_unconfirmed")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(len(calls), 1)

    def test_private_delete_failure_is_content_free_and_retryable(self):
        payload = tombstone_payload()
        body, headers, _, _ = wire_tombstone(payload, self.publication_authority)

        def failed(_):
            raise RuntimeError("private CMS deletion detail")

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_tombstone(
                body, headers, self.publication_authority,
                self.acknowledgement_authority, tombstone_expectation(payload),
                failed,
            )
        self.assertEqual(caught.exception.code, "receiver.delete_failed")
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.deletions, [])


class CMSHealthReceiverTests(unittest.TestCase):
    def setUp(self):
        self.acknowledgement_authority = Authority(b"ack-key", "ack-key-1")
        self.contract_sha256 = (
            CMS.WebsiteLocalizationCMSBridge._publication_http_capabilities()[
                "sha256"
            ]
        )
        self.checks = []

    def check(self, probe):
        self.checks.append(probe)
        return {
            "probe_id": probe.probe_id,
            "contract_sha256": probe.contract_sha256,
            "status": "healthy",
        }

    @staticmethod
    def authenticate(headers):
        return headers.get("authorization") == "Bearer secret"

    def test_sender_to_receiver_health_round_trip_is_content_free(self):
        transport = HealthReceiverTransport(
            self.acknowledgement_authority,
            self.contract_sha256,
            self.check,
        )
        publisher = HTTP.HTTPPublisherAdapter(
            "https://cms.example.test/localizations",
            lambda: {"Authorization": "Bearer secret"},
            self.acknowledgement_authority,
            transport=transport,
            probe_id_factory=lambda: "publisher-health-probe-201",
        )

        acknowledgement = publisher.check(contract_sha256=self.contract_sha256)

        self.assertEqual(acknowledgement, {
            "schema": CMS.PUBLICATION_HEALTH_ACK_SCHEMA,
            "probe_id": "publisher-health-probe-201",
            "contract_sha256": self.contract_sha256,
            "status": "healthy",
        })
        self.assertEqual(len(self.checks), 1)
        self.assertEqual(self.checks[0], RECEIVER.VerifiedHealthProbe(
            probe_id="publisher-health-probe-201",
            contract_sha256=self.contract_sha256,
        ))
        sent = json.loads(transport.calls[0][2])
        self.assertEqual(set(sent), {"schema", "probe"})
        self.assertEqual(
            set(sent["probe"]), {"schema", "probe_id", "contract_sha256"},
        )

    def test_authentication_precedes_contract_check_and_host_health(self):
        body, headers = wire_health(
            "publisher-health-probe-201", "0" * 64,
        )
        authentications = []

        def authenticate(parsed_headers):
            authentications.append(parsed_headers.get("authorization"))
            return True

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_health(
                body, headers, authenticate, self.acknowledgement_authority,
                self.check, contract_sha256=self.contract_sha256,
            )
        self.assertEqual(caught.exception.code, "receiver.health_contract_binding")
        self.assertEqual(authentications, ["Bearer secret"])
        self.assertEqual(self.checks, [])

    def test_bad_or_unavailable_authentication_never_checks_host(self):
        body, headers = wire_health(
            "publisher-health-probe-201", self.contract_sha256,
        )

        def unavailable(_):
            raise RuntimeError("private identity-provider detail")

        cases = (
            (lambda _: False, "receiver.authentication_invalid", False, 401),
            (unavailable, "receiver.authentication_failed", True, 503),
        )
        for authenticate, code, retryable, status in cases:
            with self.subTest(code=code), self.assertRaises(
                RECEIVER.CMSReceiverBlocked,
            ) as caught:
                RECEIVER.receive_health(
                    body, headers, authenticate, self.acknowledgement_authority,
                    self.check, contract_sha256=self.contract_sha256,
                )
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertEqual(caught.exception.http_status, status)
            self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.checks, [])

    def test_malformed_probe_or_binding_never_authenticates_or_checks(self):
        body, headers = wire_health(
            "publisher-health-probe-201", self.contract_sha256,
        )
        malformed = json.loads(body)
        malformed["probe"]["site_id"] = "must-not-be-sent"
        malformed_body = RECEIVER._canonical_json(
            malformed, maximum=RECEIVER.MAX_REQUEST_BYTES,
        )
        malformed_headers = dict(headers)
        malformed_headers["Content-Length"] = str(len(malformed_body))
        wrong_binding = dict(headers)
        wrong_binding["X-Localization-Probe-Id"] = "publisher-health-probe-999"
        authentications = []

        for candidate_body, candidate_headers in (
            (malformed_body, malformed_headers),
            (body, wrong_binding),
        ):
            with self.subTest(), self.assertRaises(RECEIVER.CMSReceiverBlocked):
                RECEIVER.receive_health(
                    candidate_body,
                    candidate_headers,
                    lambda _: authentications.append(True) or True,
                    self.acknowledgement_authority,
                    self.check,
                    contract_sha256=self.contract_sha256,
                )
        self.assertEqual(authentications, [])
        self.assertEqual(self.checks, [])

    def test_host_health_must_confirm_the_exact_probe_binding(self):
        body, headers = wire_health(
            "publisher-health-probe-201", self.contract_sha256,
        )

        def wrong(probe):
            self.checks.append(probe)
            return {
                "probe_id": "publisher-health-probe-999",
                "contract_sha256": probe.contract_sha256,
                "status": "healthy",
            }

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_health(
                body, headers, self.authenticate, self.acknowledgement_authority,
                wrong, contract_sha256=self.contract_sha256,
            )
        self.assertEqual(caught.exception.code, "receiver.health_unconfirmed")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(len(self.checks), 1)

    def test_private_host_failure_is_content_free_and_retryable(self):
        body, headers = wire_health(
            "publisher-health-probe-201", self.contract_sha256,
        )

        def failed(_):
            raise RuntimeError("private CMS health detail")

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_health(
                body, headers, self.authenticate, self.acknowledgement_authority,
                failed, contract_sha256=self.contract_sha256,
            )
        self.assertEqual(caught.exception.code, "receiver.health_check_failed")
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(caught.exception.retryable)

    def test_acknowledgement_is_signed_only_after_confirmed_health(self):
        class BrokenAuthority:
            def sign(self, _):
                raise RuntimeError("private signing detail")

            def verify(self, *_):
                return False

        body, headers = wire_health(
            "publisher-health-probe-201", self.contract_sha256,
        )

        with self.assertRaises(RECEIVER.CMSReceiverBlocked) as caught:
            RECEIVER.receive_health(
                body, headers, self.authenticate, BrokenAuthority(), self.check,
                contract_sha256=self.contract_sha256,
            )
        self.assertEqual(caught.exception.code, "receiver.acknowledgement_signing")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(self.checks), 1)


class CMSReceiverApplicationTests(unittest.TestCase):
    def setUp(self):
        self.publication_authority = Authority(b"publication-key", "publication-key-1")
        self.acknowledgement_authority = Authority(b"ack-key", "ack-key-1")
        self.contract_sha256 = (
            CMS.WebsiteLocalizationCMSBridge._publication_http_capabilities()[
                "sha256"
            ]
        )
        self.authentications = []
        self.publication_resolutions = []
        self.tombstone_resolutions = []
        self.commits = []
        self.deletions = []
        self.checks = []

    def authenticate(self, headers):
        self.authentications.append(headers.get("authorization"))
        return headers.get("authorization") == "Bearer secret"

    def resolve_publication(self, publication):
        self.publication_resolutions.append(publication)
        return expectation(publication.payload)

    def commit(self, publication):
        self.commits.append(publication)
        return {
            "delivery_id": publication.delivery_id,
            "payload_sha256": publication.payload_sha256,
            "status": "committed",
        }

    def resolve_tombstone(self, tombstone):
        self.tombstone_resolutions.append(tombstone)
        return tombstone_expectation(tombstone.payload)

    def delete(self, tombstone):
        self.deletions.append(tombstone)
        return {
            "delivery_id": tombstone.delivery_id,
            "payload_sha256": tombstone.payload_sha256,
            "status": "deleted",
        }

    def check(self, probe):
        self.checks.append(probe)
        return {
            "probe_id": probe.probe_id,
            "contract_sha256": probe.contract_sha256,
            "status": "healthy",
        }

    def application(self, **overrides):
        values = {
            "publication_authority": self.publication_authority,
            "acknowledgement_authority": self.acknowledgement_authority,
            "authenticate": self.authenticate,
            "resolve_publication_expectation": self.resolve_publication,
            "commit": self.commit,
            "resolve_tombstone_expectation": self.resolve_tombstone,
            "delete": self.delete,
            "check": self.check,
            "contract_sha256": self.contract_sha256,
            "clock": lambda: 1000,
        }
        values.update(overrides)
        return RECEIVER.CMSReceiverApplication(**values)

    def test_one_https_endpoint_completes_all_three_callback_operations(self):
        application = self.application()
        transport = WSGIReceiverTransport(application)
        publisher = HTTP.HTTPPublisherAdapter(
            "https://cms.example.test" + RECEIVER.RECEIVER_PATH,
            lambda: {"Authorization": "Bearer secret"},
            self.acknowledgement_authority,
            transport=transport,
            probe_id_factory=lambda: "publisher-health-probe-301",
        )
        publication = publication_payload()
        tombstone = tombstone_payload()

        accepted = publisher.publish(
            request(publication, self.publication_authority),
        )
        deleted = publisher.publish(
            tombstone_request(tombstone, self.publication_authority),
        )
        healthy = publisher.check(contract_sha256=self.contract_sha256)

        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(self.authentications, ["Bearer secret"] * 3)
        self.assertEqual(len(self.publication_resolutions), 1)
        self.assertEqual(len(self.tombstone_resolutions), 1)
        self.assertEqual(len(self.commits), 1)
        self.assertEqual(len(self.deletions), 1)
        self.assertEqual(len(self.checks), 1)

    def test_authentication_failure_precedes_json_and_host_callbacks(self):
        application = self.application(authenticate=lambda _: False)
        body = b"not-json"
        headers = {
            "Authorization": "Bearer wrong",
            "Content-Type": "application/json; charset=utf-8",
        }

        status, _, response = call_wsgi(
            application, wsgi_environ(body, headers),
        )

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(json.loads(response)["error"], "receiver.authentication_invalid")
        self.assertNotIn(b"not-json", response)
        self.assertEqual(self.publication_resolutions, [])
        self.assertEqual(self.tombstone_resolutions, [])
        self.assertEqual(self.commits, [])

    def test_signature_verification_precedes_expectation_resolution(self):
        application = self.application()
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        envelope = json.loads(body)
        envelope["signature"]["signature"] = "0" * 64
        body = RECEIVER._canonical_json(
            envelope, maximum=RECEIVER.MAX_REQUEST_BYTES,
        )
        headers["Content-Length"] = str(len(body))
        headers["Authorization"] = "Bearer secret"

        status, _, response = call_wsgi(
            application, wsgi_environ(body, headers),
        )

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(json.loads(response)["error"], "receiver.signature_invalid")
        self.assertEqual(self.publication_resolutions, [])
        self.assertEqual(self.commits, [])

    def test_wrong_current_expectation_blocks_before_commit(self):
        def stale(publication):
            self.publication_resolutions.append(publication)
            return replace(
                expectation(publication.payload), source_sequence=999,
            )

        application = self.application(resolve_publication_expectation=stale)
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        headers["Authorization"] = "Bearer secret"

        status, _, response = call_wsgi(
            application, wsgi_environ(body, headers),
        )

        self.assertEqual(status, "409 Conflict")
        self.assertEqual(json.loads(response)["error"], "receiver.source_binding")
        self.assertEqual(len(self.publication_resolutions), 1)
        self.assertEqual(self.commits, [])

    def test_expectation_resolver_cannot_mutate_verified_publication(self):
        def mutating(publication):
            self.publication_resolutions.append(publication)
            publication.payload["source_sequence"] = 999
            return expectation(publication.payload)

        application = self.application(
            resolve_publication_expectation=mutating,
        )
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        headers["Authorization"] = "Bearer secret"

        status, _, response = call_wsgi(
            application, wsgi_environ(body, headers),
        )

        self.assertEqual(status, "409 Conflict")
        self.assertEqual(json.loads(response)["error"], "receiver.source_binding")
        self.assertEqual(payload["source_sequence"], 201)
        self.assertEqual(self.commits, [])

    def test_private_resolver_failure_is_content_free_and_retryable(self):
        def failed(_):
            raise RuntimeError("private CMS lookup detail")

        application = self.application(resolve_publication_expectation=failed)
        payload = publication_payload()
        body, headers, _, _ = wire(payload, self.publication_authority)
        headers["Authorization"] = "Bearer secret"

        status, _, response = call_wsgi(
            application, wsgi_environ(body, headers),
        )
        result = json.loads(response)

        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(result["error"], "receiver.expectation_failed")
        self.assertTrue(result["retryable"])
        self.assertNotIn(b"private", response)
        self.assertNotIn(payload["localizations"][0]["target_text"].encode(), response)
        self.assertEqual(self.commits, [])

    def test_unknown_authenticated_operation_is_content_free(self):
        application = self.application()
        body = RECEIVER._canonical_json(
            {"schema": "unknown.callback.v1", "secret": "do-not-return"},
            maximum=RECEIVER.MAX_REQUEST_BYTES,
        )
        headers = {
            "Authorization": "Bearer secret",
            "Content-Type": "application/json; charset=utf-8",
        }

        status, _, response = call_wsgi(
            application, wsgi_environ(body, headers),
        )

        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(json.loads(response)["error"], "receiver.operation_invalid")
        self.assertNotIn(b"do-not-return", response)
        self.assertEqual(self.commits, [])
        self.assertEqual(self.deletions, [])
        self.assertEqual(self.checks, [])

    def test_transport_rejections_happen_before_authentication(self):
        body, headers = wire_health(
            "publisher-health-probe-301", self.contract_sha256,
        )
        cases = (
            ({"PATH_INFO": "/wrong"}, "404 Not Found"),
            ({"REQUEST_METHOD": "GET"}, "405 Method Not Allowed"),
            ({"wsgi.url_scheme": "http"}, "400 Bad Request"),
            ({"QUERY_STRING": "debug=1"}, "400 Bad Request"),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, "400 Bad Request"),
            ({"CONTENT_TYPE": "text/plain"}, "415 Unsupported Media Type"),
            ({"CONTENT_LENGTH": ""}, "411 Length Required"),
        )

        for changes, expected_status in cases:
            environ = wsgi_environ(body, headers)
            environ.update(changes)
            with self.subTest(changes=changes):
                status, _, response = call_wsgi(self.application(), environ)
                self.assertEqual(status, expected_status)
                self.assertEqual(json.loads(response)["status"], "BLOCK")
                self.assertNotIn(b"publisher-health-probe-301", response)
        self.assertEqual(self.authentications, [])
        self.assertEqual(self.checks, [])

    def test_truncated_and_oversized_bodies_fail_without_authentication(self):
        application = self.application()
        body, headers = wire_health(
            "publisher-health-probe-301", self.contract_sha256,
        )
        truncated = wsgi_environ(body, headers)
        truncated["CONTENT_LENGTH"] = str(len(body) + 1)
        oversized = wsgi_environ(body, headers)
        oversized["CONTENT_LENGTH"] = str(RECEIVER.MAX_REQUEST_BYTES + 1)

        first = call_wsgi(application, truncated)
        second = call_wsgi(application, oversized)

        self.assertEqual(first[0], "400 Bad Request")
        self.assertEqual(second[0], "413 Content Too Large")
        self.assertEqual(self.authentications, [])
        self.assertEqual(self.checks, [])


if __name__ == "__main__":
    unittest.main()
