from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
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


INTAKE = load(
    "blun_test_website_localization_native_reference_intake",
    ROOT / "integrations" / "website_localization_native_reference_intake.py",
)
FIXTURES = load(
    "blun_test_native_reference_intake_fixtures",
    ROOT / "tests" / "test_website_localization_benchmark.py",
)


class NativeReferenceIntakeTests(unittest.TestCase):
    route = "qualified-native-editorial-production"
    reviewer_id = "qualified-native-reviewer-17"
    reviewer_version = "credential-2026-08-30"

    def setUp(self):
        self.policy = FIXTURES.policy()
        self.job = FIXTURES.job("mt-MT", "0")
        self.verifier = FIXTURES.HmacNativeReferenceVerifier()
        self.authority = FIXTURES.HmacBenchmarkAuthority()

    def order(self, **kwargs):
        return INTAKE.create_native_reference_work_order(
            kwargs.get("job", self.job),
            kwargs.get("policy", self.policy),
            kwargs.get("route", self.route),
        )

    def request(self, order, text=None):
        return INTAKE.native_reference_verification_request_for_work_order(
            order,
            self.job,
            self.policy,
            self.route,
            text or FIXTURES.TARGETS["mt-MT"]["reference"],
            reviewer_id=self.reviewer_id,
            reviewer_version=self.reviewer_version,
        )

    def submission(self, order, request):
        return {
            "schema": INTAKE.SUBMISSION_SCHEMA,
            "work_order_id": order["work_order_id"],
            "work_order_sha256": hashlib.sha256(
                json.dumps(
                    order, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "verification_request": request,
            "qualification_receipt": self.verifier.receipt(request),
        }

    def accept(self, store, order, submission, **kwargs):
        return INTAKE.accept_native_reference_submission(
            store,
            order,
            submission,
            self.job,
            self.policy,
            self.route,
            native_reference_verifier=kwargs.get("verifier", self.verifier),
            evidence_authority=kwargs.get("authority", self.authority),
            operation_guard=kwargs.get("guard"),
            now=100,
        )

    def test_work_order_is_exact_bound_and_contains_no_target(self):
        order = self.order()
        encoded = json.dumps(order, ensure_ascii=False)

        self.assertEqual(order["target_locale"], "mt-MT")
        self.assertEqual(order["qualification"]["method"], "qualified_native_human")
        self.assertEqual(order["source"]["text"], self.job["source"]["text"])
        self.assertNotIn("target_text", encoded)
        self.assertNotIn(FIXTURES.TARGETS["mt-MT"]["reference"], encoded)
        self.assertTrue(order["work_order_id"].startswith(
            "native-reference-work-order:"
        ))

    def test_verified_submission_is_attested_and_saved_idempotently(self):
        order = self.order()
        request = self.request(order)
        submission = self.submission(order, request)
        with sqlite3.connect(":memory:") as connection:
            store = INTAKE._STORE.NativeReferenceArtifactStore(connection)
            first = self.accept(store, order, submission)
            second = self.accept(store, order, submission)

        self.assertEqual(first, second)
        self.assertEqual(first["request"], request)
        self.assertEqual(first["qualification_receipt"], submission[
            "qualification_receipt"
        ])

    def test_changed_policy_job_locale_or_route_makes_order_stale(self):
        order = self.order()
        cases = (
            (FIXTURES.job("mt-MT", "1"), self.policy, self.route),
            (FIXTURES.job("fi-FI", "0"), self.policy, self.route),
            (self.job, FIXTURES.policy(
                native_reference_revision="qualified-native-reference-2",
            ), self.route),
            (self.job, self.policy, "different-editorial-route"),
        )
        for job, policy, route in cases:
            with self.subTest(route=route, locale=job["target"]["locale"]):
                with self.assertRaises(
                    INTAKE.NativeReferenceIntakeFailed,
                ) as caught:
                    INTAKE.native_reference_verification_request_for_work_order(
                        order,
                        job,
                        policy,
                        route,
                        "Reference text",
                        reviewer_id=self.reviewer_id,
                        reviewer_version=self.reviewer_version,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "native_reference.intake.work_order_stale",
                )

    def test_work_order_hash_and_verification_request_tamper_block(self):
        order = self.order()
        request = self.request(order)
        for mutate in (
            lambda item: item.update(work_order_sha256="0" * 64),
            lambda item: item["verification_request"].update(
                target_locale="fi-FI",
            ),
            lambda item: item["verification_request"]["source"].update(
                text="changed source",
            ),
        ):
            submission = copy.deepcopy(self.submission(order, request))
            mutate(submission)
            with sqlite3.connect(":memory:") as connection:
                store = INTAKE._STORE.NativeReferenceArtifactStore(connection)
                with self.assertRaises(
                    INTAKE.NativeReferenceIntakeFailed,
                ) as caught:
                    self.accept(store, order, submission)
            self.assertIn(caught.exception.code, {
                "native_reference.intake.submission_invalid",
                "native_reference.intake.verification_request_mismatch",
            })

    def test_receipt_replay_for_changed_target_is_rejected_before_save(self):
        order = self.order()
        original = self.request(order)
        changed = self.request(order, "Referenza Maltija differenti.")
        submission = self.submission(order, original)
        submission["verification_request"] = changed
        with sqlite3.connect(":memory:") as connection:
            store = INTAKE._STORE.NativeReferenceArtifactStore(connection)
            with self.assertRaises(
                INTAKE.NativeReferenceIntakeFailed,
            ) as caught:
                self.accept(store, order, submission)
            count = connection.execute(
                "SELECT COUNT(*) FROM benchmark_native_references"
            ).fetchone()[0]

        self.assertEqual(
            caught.exception.code,
            "native_reference.intake.submission_rejected",
        )
        self.assertEqual(count, 0)

    def test_lease_guard_covers_receipt_sign_and_all_reverification(self):
        order = self.order()
        request = self.request(order)
        submission = self.submission(order, request)
        calls = []
        with sqlite3.connect(":memory:") as connection:
            store = INTAKE._STORE.NativeReferenceArtifactStore(connection)
            self.accept(
                store, order, submission, guard=lambda: calls.append("guard"),
            )

        self.assertGreaterEqual(len(calls), 6)

    def test_lost_lease_and_verifier_outage_are_retryable_and_content_free(self):
        secret = "private Maltese target"
        order = self.order()
        request = self.request(order)
        submission = self.submission(order, request)

        class UnavailableVerifier:
            def verify(self, _request, _receipt):
                raise TimeoutError(secret)

        cases = (
            ({"guard": lambda: (_ for _ in ()).throw(RuntimeError(secret))},
             "native_reference.intake.operation_guard_failed"),
            ({"verifier": UnavailableVerifier()},
             "native_reference.intake.verification_unavailable"),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected), sqlite3.connect(":memory:") as connection:
                store = INTAKE._STORE.NativeReferenceArtifactStore(connection)
                with self.assertRaises(
                    INTAKE.NativeReferenceIntakeFailed,
                ) as caught:
                    self.accept(store, order, submission, **overrides)
                self.assertEqual(caught.exception.code, expected)
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn(secret, str(caught.exception))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM benchmark_native_references"
                ).fetchone()[0], 0)

    def test_conflicting_valid_submission_never_replaces_first(self):
        order = self.order()
        first_request = self.request(order)
        changed_request = self.request(order, "Referenza Maltija oħra naturali.")
        with sqlite3.connect(":memory:") as connection:
            store = INTAKE._STORE.NativeReferenceArtifactStore(connection)
            first = self.accept(
                store, order, self.submission(order, first_request),
            )
            with self.assertRaises(
                INTAKE.NativeReferenceIntakeFailed,
            ) as caught:
                self.accept(
                    store, order, self.submission(order, changed_request),
                )
            loaded = store.load(
                self.job,
                self.policy,
                self.route,
                native_reference_verifier=self.verifier,
                evidence_authority=self.authority,
            )

        self.assertEqual(caught.exception.code, "native_reference.store.conflict")
        self.assertEqual(loaded, first)


if __name__ == "__main__":
    unittest.main()
