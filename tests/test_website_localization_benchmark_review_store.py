from __future__ import annotations

import base64
import copy
import hashlib
import hmac
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


STORE = load(
    "blun_test_benchmark_review_store",
    ROOT / "integrations" / "website_localization_benchmark_review_store.py",
)
BENCHMARK = STORE._BENCHMARK
SUITE = BENCHMARK._SUITE


class Authority:
    def __init__(self, key=b"review-store-test-key", *, available=True):
        self.key = key
        self.available = available

    def sign(self, payload):
        if not self.available:
            raise RuntimeError("private signing failure")
        return BENCHMARK.BenchmarkSignature(
            "hmac-sha256-test",
            "benchmark-key",
            base64.b64encode(hmac.new(self.key, payload, hashlib.sha256).digest()).decode(),
        )

    def verify(self, payload, signature):
        if not self.available:
            raise RuntimeError("private verification failure")
        expected = base64.b64encode(
            hmac.new(self.key, payload, hashlib.sha256).digest()
        ).decode()
        return (
            signature.algorithm == "hmac-sha256-test"
            and signature.key_id == "benchmark-key"
            and hmac.compare_digest(signature.signature, expected)
        )


def policy(**overrides):
    manifest = SUITE.manifest()
    values = {
        "benchmark_version": "native-vs-baseline-1",
        "suite_version": manifest["version"],
        "suite_sha256": manifest["sha256"],
        "candidate_provider_id": "customer-llm",
        "candidate_model_id": "candidate-model",
        "candidate_model_version": "2026-09-08",
        "candidate_software_version": "6.43.0-dev",
        "candidate_worker_schema": BENCHMARK._WORKER.WORKER_SCHEMA,
        "candidate_glossary_version": "benchmark-glossary-1",
        "candidate_policy_version": "native-web-2",
        "attestation_algorithm": "hmac-sha256-test",
        "attestation_key_id": "benchmark-key",
        "baseline_id": "deepl-official-api",
        "baseline_version": "fixture-2026-09-08",
        "reviewer_id": "independent-native-panel",
        "reviewer_version": "2026-09-08",
        "native_reference_revision": "qualified-native-reference-1",
        "native_reference_verifier_id": "qualified-review-registry",
        "native_reference_verifier_version": "2026-09-08",
        "required_locales": ("mt-MT", "fi-FI"),
        "required_content_types": ("commercial",),
        "minimum_cases_per_locale": len(manifest["cases"]),
        "minimum_cases_per_content_type": 8,
    }
    values.update(overrides)
    return BENCHMARK.BenchmarkPolicy(**values)


def request(*, phase="target_native", locale="mt-MT", suffix="1"):
    system = (
        BENCHMARK._NATIVE_SYSTEM
        if phase == "target_native"
        else BENCHMARK._FIDELITY_SYSTEM
    )
    digest = hashlib.sha256(f"{phase}:{locale}:{suffix}".encode()).hexdigest()
    return BENCHMARK.BenchmarkReviewRequest(
        schema=BENCHMARK.BENCHMARK_SCHEMA,
        review_id="benchmark-review-" + digest,
        phase=phase,
        target_locale=locale,
        system_instruction=system,
        input={"blind_id": "blind-" + digest, "variants": []},
    )


def response(review_request, preference="A"):
    return {
        "schema": BENCHMARK.REVIEW_SCHEMA,
        "phase": review_request.phase,
        "target_locale": review_request.target_locale,
        "blind_id": review_request.input["blind_id"],
        "preference": preference,
        "variants": {
            "A": {"blocking_defects": [], "major_defects": []},
            "B": {"blocking_defects": [], "major_defects": []},
        },
    }


class Reviewer:
    def __init__(self, *, failure=None):
        self.calls = []
        self.failure = failure

    def review(self, review_request):
        self.calls.append(review_request)
        if self.failure is not None:
            raise self.failure
        return response(review_request)


class BenchmarkReviewEvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.store = STORE.BenchmarkReviewEvidenceStore(self.connection)
        self.policy = policy()
        self.authority = Authority()
        self.request = request()

    def durable(self, reviewer, **overrides):
        values = {
            "store": self.store,
            "policy": self.policy,
            "route_id": "independent-review-panel",
            "reviewer": reviewer,
            "evidence_authority": self.authority,
            "clock": lambda: 100,
        }
        values.update(overrides)
        return STORE.DurableBenchmarkReviewer(**values)

    def test_restart_reuses_exact_attested_response_without_reviewer_call(self):
        first_reviewer = Reviewer()
        first = self.durable(first_reviewer).review(self.request)
        second_reviewer = Reviewer(failure=AssertionError("must not be called"))
        second = self.durable(second_reviewer).review(self.request)

        self.assertEqual(first, second)
        self.assertEqual(len(first_reviewer.calls), 1)
        self.assertEqual(second_reviewer.calls, [])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM benchmark_review_evidence"
            ).fetchone()[0],
            1,
        )

    def test_each_ordered_phase_has_a_distinct_durable_identity(self):
        reviewer = Reviewer()
        native = self.durable(reviewer).review(self.request)
        fidelity_request = request(phase="source_fidelity")
        fidelity = self.durable(reviewer).review(fidelity_request)

        self.assertEqual(native["phase"], "target_native")
        self.assertEqual(fidelity["phase"], "source_fidelity")
        self.assertEqual(len(reviewer.calls), 2)
        rows = self.connection.execute(
            "SELECT review_id, request_sha256 FROM benchmark_review_evidence"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({tuple(row) for row in rows}), 2)

    def test_digest_and_attestation_tampering_block_before_reviewer(self):
        self.durable(Reviewer()).review(self.request)
        cases = (
            ("artifact_json = '{}'", "review.store.state_invalid"),
            ("artifact_sha256 = '" + "0" * 64 + "'", "review.store.state_invalid"),
        )
        original = self.connection.execute(
            "SELECT artifact_json, artifact_sha256 FROM benchmark_review_evidence"
        ).fetchone()
        for mutation, code in cases:
            with self.subTest(mutation=mutation):
                self.connection.execute(
                    "UPDATE benchmark_review_evidence "
                    "SET artifact_json = ?, artifact_sha256 = ?",
                    tuple(original),
                )
                self.connection.execute(
                    "UPDATE benchmark_review_evidence SET " + mutation
                )
                self.connection.commit()
                reviewer = Reviewer()
                with self.assertRaises(STORE.BenchmarkReviewEvidenceFailed) as caught:
                    self.durable(reviewer).review(self.request)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(reviewer.calls, [])

    def test_valid_json_with_wrong_attestation_blocks_before_reviewer(self):
        self.durable(Reviewer()).review(self.request)
        artifact_json = self.connection.execute(
            "SELECT artifact_json FROM benchmark_review_evidence"
        ).fetchone()[0]
        artifact = json.loads(artifact_json)
        artifact["attestation"]["signature"] = base64.b64encode(b"wrong").decode()
        changed = json.dumps(
            artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        self.connection.execute(
            "UPDATE benchmark_review_evidence SET artifact_json = ?, artifact_sha256 = ?",
            (changed, hashlib.sha256(changed.encode()).hexdigest()),
        )
        self.connection.commit()
        reviewer = Reviewer()

        with self.assertRaises(STORE.BenchmarkReviewEvidenceFailed) as caught:
            self.durable(reviewer).review(self.request)

        self.assertEqual(caught.exception.code, "review.store.artifact_invalid")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(reviewer.calls, [])

    def test_same_review_id_with_changed_request_blocks_before_reviewer(self):
        self.durable(Reviewer()).review(self.request)
        changed = copy.deepcopy(self.request)
        changed.input["blind_id"] = "blind-" + "f" * 64
        reviewer = Reviewer()

        with self.assertRaises(STORE.BenchmarkReviewEvidenceFailed) as caught:
            self.durable(reviewer).review(changed)

        self.assertEqual(caught.exception.code, "review.store.conflict")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(reviewer.calls, [])

    def test_invalid_response_is_terminal_and_never_persisted(self):
        reviewer = Reviewer()
        reviewer.review = lambda review_request: {
            **response(review_request), "preference": "candidate",
        }

        with self.assertRaises(STORE.BenchmarkReviewEvidenceFailed) as caught:
            self.durable(reviewer).review(self.request)

        self.assertEqual(caught.exception.code, "review.store.response_invalid")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM benchmark_review_evidence"
            ).fetchone()[0],
            0,
        )

    def test_conflicting_valid_response_never_replaces_first(self):
        first = response(self.request, "A")
        second = response(self.request, "B")
        stored = self.store.save(
            self.request, self.policy, "independent-review-panel", first,
            evidence_authority=self.authority, now=100,
        )

        with self.assertRaises(STORE.BenchmarkReviewEvidenceFailed) as caught:
            self.store.save(
                self.request, self.policy, "independent-review-panel", second,
                evidence_authority=self.authority, now=101,
            )

        self.assertEqual(caught.exception.code, "review.store.conflict")
        self.assertEqual(
            self.store.load(
                self.request, self.policy, "independent-review-panel",
                evidence_authority=self.authority,
            ),
            stored,
        )

    def test_guard_runs_before_review_and_attestation_operations(self):
        calls = []
        reviewer = Reviewer()
        durable = self.durable(
            reviewer,
            operation_guard=lambda: calls.append("guard"),
        )

        durable.review(self.request)

        self.assertEqual(len(reviewer.calls), 1)
        self.assertGreaterEqual(len(calls), 5)

    def test_reviewer_and_authority_failures_preserve_retryability(self):
        retryable_error = type(
            "ExternalReviewFailure",
            (RuntimeError,),
            {"retryable": True},
        )("private outage")
        with self.assertRaises(STORE.BenchmarkReviewEvidenceFailed) as caught:
            self.durable(Reviewer(failure=retryable_error)).review(self.request)
        self.assertEqual(caught.exception.code, "review.adapter_unavailable")
        self.assertTrue(caught.exception.retryable)

        unavailable = Authority(available=False)
        with self.assertRaises(STORE.BenchmarkReviewEvidenceFailed) as caught:
            self.durable(Reviewer(), evidence_authority=unavailable).review(
                request(suffix="authority"),
            )
        self.assertEqual(caught.exception.code, "review.store.attestation_unavailable")
        self.assertTrue(caught.exception.retryable)

    def test_health_is_read_only_content_free_and_matches_required_passes(self):
        secret = "private reviewer explanation"
        reviewed = response(self.request, "tie")
        reviewed["variants"]["A"]["major_defects"] = [{
            "class": "translationese",
            "excerpt": "private excerpt",
            "reason": secret,
        }]
        self.store.save(
            self.request,
            self.policy,
            "independent-review-panel",
            reviewed,
            evidence_authority=self.authority,
            now=100,
        )
        expected = ({
            "phase": self.request.phase,
            "request_sha256": BENCHMARK._hash_json(self.request.as_payload()),
            "response_sha256": BENCHMARK._hash_json(reviewed),
        },)
        before = self.connection.total_changes

        health = self.store.health(
            self.policy,
            "independent-review-panel",
            evidence_authority=self.authority,
            expected_passes=expected,
            now=101,
        )

        self.assertEqual(health.status, "healthy")
        self.assertEqual(dict(health.counts), {
            "historical": 0,
            "matched": 1,
            "required": 1,
            "scoped": 1,
            "source_fidelity": 0,
            "target_native": 1,
            "total": 1,
        })
        self.assertEqual(self.connection.total_changes, before)
        serialized = json.dumps(health.as_payload())
        self.assertNotIn(secret, serialized)
        self.assertNotIn("private excerpt", serialized)

    def test_health_blocks_missing_tampered_or_unverifiable_required_evidence(self):
        reviewed = self.store.save(
            self.request,
            self.policy,
            "independent-review-panel",
            response(self.request),
            evidence_authority=self.authority,
            now=100,
        )
        expected = ({
            "phase": self.request.phase,
            "request_sha256": BENCHMARK._hash_json(self.request.as_payload()),
            "response_sha256": BENCHMARK._hash_json(reviewed),
        },)
        missing = self.store.health(
            self.policy,
            "independent-review-panel",
            evidence_authority=self.authority,
            expected_passes=({
                **expected[0],
                "request_sha256": "f" * 64,
            },),
            now=101,
        )
        self.assertEqual(missing.status, "blocked")
        self.assertEqual(missing.reasons, ("review.store.required_missing",))

        unavailable = self.store.health(
            self.policy,
            "independent-review-panel",
            evidence_authority=Authority(available=False),
            expected_passes=expected,
            now=101,
        )
        self.assertEqual(unavailable.status, "blocked")
        self.assertEqual(
            unavailable.reasons,
            ("review.store.attestation_unavailable",),
        )

        self.connection.execute(
            "UPDATE benchmark_review_evidence SET artifact_sha256 = ?",
            ("0" * 64,),
        )
        self.connection.commit()
        tampered = self.store.health(
            self.policy,
            "independent-review-panel",
            evidence_authority=self.authority,
            expected_passes=expected,
            now=101,
        )
        self.assertEqual(tampered.status, "blocked")
        self.assertEqual(tampered.reasons, ("review.store.state_invalid",))


if __name__ == "__main__":
    unittest.main()
