from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


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


def release_evidence(target: str, *, locale="fi-FI", commercial=True):
    target_sha256 = hashlib.sha256(target.encode("utf-8")).hexdigest()
    review = None
    profile = None
    content_type = "cta"
    if commercial:
        content_type = "commercial"
        profile = CMS._PLANNER.COMMERCIAL_PROFILE
        review = {
            "schema": CMS._RELEASE._WORKER._COMMERCIAL.REVIEW_SUMMARY_SCHEMA,
            "profile": profile,
            "status": "verified",
            "review_required_dimensions": [],
            "evidence_sha256": "5" * 64,
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
        "commercial_review": review,
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
        self.assertEqual(evidence["commercial_review"]["status"], "verified")

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

if __name__ == "__main__":
    unittest.main()
