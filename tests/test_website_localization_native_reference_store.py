from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
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
    "blun_test_website_localization_native_reference_store",
    ROOT / "integrations" / "website_localization_native_reference_store.py",
)
FIXTURES = load(
    "blun_test_native_reference_store_fixtures",
    ROOT / "tests" / "test_website_localization_benchmark.py",
)


class NativeReferenceArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.policy = FIXTURES.policy()
        self.job = FIXTURES.job("fi-FI", "0")
        self.verifier = FIXTURES.HmacNativeReferenceVerifier()
        self.authority = FIXTURES.HmacBenchmarkAuthority()
        self.artifact = FIXTURES.native_reference(
            self.job,
            self.policy,
            self.verifier,
            self.authority,
        )

    def resolve(self, store, loader, **kwargs):
        return STORE.resolve_native_reference_artifact(
            store,
            self.job,
            self.policy,
            "qualified-native-vault-production",
            loader,
            native_reference_verifier=self.verifier,
            evidence_authority=self.authority,
            now=100,
            **kwargs,
        )

    def test_restart_reuses_exact_verified_reference_without_loader(self):
        events = []
        with sqlite3.connect(":memory:") as connection:
            store = STORE.NativeReferenceArtifactStore(connection)

            def loader():
                events.append("loader")
                return self.artifact

            first = self.resolve(
                store,
                loader,
                operation_guard=lambda: events.append("guard"),
            )
            second = self.resolve(
                store,
                lambda: self.fail("cache hit called the external loader"),
                operation_guard=lambda: events.append("guard"),
            )

        self.assertEqual(first, self.artifact)
        self.assertEqual(second, self.artifact)
        self.assertEqual(
            events,
            ["guard", "guard", "loader", "guard", "guard"],
        )

    def test_changed_route_policy_job_and_locale_cannot_reuse_reference(self):
        with sqlite3.connect(":memory:") as connection:
            store = STORE.NativeReferenceArtifactStore(connection)
            self.resolve(store, lambda: self.artifact)
            self.assertIsNone(store.load(
                self.job,
                self.policy,
                "different-native-vault",
                native_reference_verifier=self.verifier,
                evidence_authority=self.authority,
            ))
            changed_policy = FIXTURES.policy(
                native_reference_revision="qualified-native-reference-2",
            )
            self.assertIsNone(store.load(
                self.job,
                changed_policy,
                "qualified-native-vault-production",
                native_reference_verifier=self.verifier,
                evidence_authority=self.authority,
            ))
            for changed_job in (
                FIXTURES.job("fi-FI", "1"),
                FIXTURES.job("mt-MT", "0"),
            ):
                self.assertIsNone(store.load(
                    changed_job,
                    self.policy,
                    "qualified-native-vault-production",
                    native_reference_verifier=self.verifier,
                    evidence_authority=self.authority,
                ))

    def test_corrupt_state_blocks_before_external_loader(self):
        with sqlite3.connect(":memory:") as connection:
            store = STORE.NativeReferenceArtifactStore(connection)
            self.resolve(store, lambda: self.artifact)
            connection.execute("""
                UPDATE benchmark_native_references
                SET artifact_json = artifact_json || ' '
            """)
            connection.commit()
            calls = []
            with self.assertRaises(STORE.NativeReferenceStoreFailed) as caught:
                self.resolve(store, lambda: calls.append("loader"))

        self.assertEqual(
            caught.exception.code, "native_reference.store.state_invalid",
        )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(calls, [])

    def test_resigned_tamper_still_blocks_before_external_loader(self):
        with sqlite3.connect(":memory:") as connection:
            store = STORE.NativeReferenceArtifactStore(connection)
            self.resolve(store, lambda: self.artifact)
            row = connection.execute("""
                SELECT artifact_json FROM benchmark_native_references
            """).fetchone()
            changed = json.loads(row[0])
            changed["request"]["target_text"] += " Muutos."
            changed_json = json.dumps(
                changed,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute("""
                UPDATE benchmark_native_references
                SET artifact_json = ?, artifact_sha256 = ?
            """, (
                changed_json,
                hashlib.sha256(changed_json.encode("utf-8")).hexdigest(),
            ))
            connection.commit()
            calls = []
            with self.assertRaises(STORE.NativeReferenceStoreFailed) as caught:
                self.resolve(store, lambda: calls.append("loader"))

        self.assertEqual(
            caught.exception.code, "native_reference.store.artifact_invalid",
        )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(calls, [])

    def test_independent_writers_converge_and_conflicts_never_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-references.sqlite3"
            first_connection = sqlite3.connect(path)
            second_connection = sqlite3.connect(path)
            try:
                first = STORE.NativeReferenceArtifactStore(first_connection)
                second = STORE.NativeReferenceArtifactStore(second_connection)
                saved = first.save(
                    self.job,
                    self.policy,
                    "qualified-native-vault-production",
                    self.artifact,
                    native_reference_verifier=self.verifier,
                    evidence_authority=self.authority,
                    now=100,
                )
                self.assertEqual(second.save(
                    self.job,
                    self.policy,
                    "qualified-native-vault-production",
                    self.artifact,
                    native_reference_verifier=self.verifier,
                    evidence_authority=self.authority,
                    now=101,
                ), saved)
                changed = FIXTURES.native_reference(
                    self.job,
                    self.policy,
                    self.verifier,
                    self.authority,
                    text="Sujuva, mutta eri suomalainen viiteteksti.",
                )
                with self.assertRaises(
                    STORE.NativeReferenceStoreFailed,
                ) as caught:
                    second.save(
                        self.job,
                        self.policy,
                        "qualified-native-vault-production",
                        changed,
                        native_reference_verifier=self.verifier,
                        evidence_authority=self.authority,
                        now=102,
                    )
                self.assertEqual(
                    caught.exception.code, "native_reference.store.conflict",
                )
                self.assertEqual(first.load(
                    self.job,
                    self.policy,
                    "qualified-native-vault-production",
                    native_reference_verifier=self.verifier,
                    evidence_authority=self.authority,
                ), saved)
            finally:
                first_connection.close()
                second_connection.close()

    def test_verifier_outage_is_retryable_without_reacquisition(self):
        class UnavailableVerifier:
            def verify(self, request, receipt):
                raise TimeoutError

        with sqlite3.connect(":memory:") as connection:
            store = STORE.NativeReferenceArtifactStore(connection)
            self.resolve(store, lambda: self.artifact)
            calls = []
            with self.assertRaises(STORE.NativeReferenceStoreFailed) as caught:
                STORE.resolve_native_reference_artifact(
                    store,
                    self.job,
                    self.policy,
                    "qualified-native-vault-production",
                    lambda: calls.append("loader"),
                    native_reference_verifier=UnavailableVerifier(),
                    evidence_authority=self.authority,
                    now=101,
                )

        self.assertEqual(
            caught.exception.code,
            "native_reference.store.verification_unavailable",
        )
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(calls, [])

    def test_store_failure_uses_campaign_retry_policy_without_content(self):
        secret = "confidential Finnish reference"
        policy = FIXTURES.campaign_policy()
        with sqlite3.connect(":memory:") as connection:
            campaign_store = FIXTURES.CAMPAIGN.BenchmarkCampaignStore(
                connection,
            )
            campaign_id = campaign_store.create(
                policy, max_attempts=2, now=100,
            )

            def unavailable(_):
                raise STORE.NativeReferenceStoreFailed(
                    "native_reference.store.verification_unavailable",
                    retryable=True,
                ) from RuntimeError(secret)

            outcome = FIXTURES.CAMPAIGN.run_next_benchmark_case(
                campaign_store,
                policy,
                campaign_id,
                "benchmark-worker",
                unavailable,
                FIXTURES.CampaignCandidateReviewer(),
                blinding_key=b"native-reference-campaign-blinding-key",
                native_reference_verifier=(
                    FIXTURES.CampaignNativeReferenceVerifier()
                ),
                evidence_authority=FIXTURES.CampaignAuthority(),
                clock=lambda: 100,
                retry_base_seconds=5,
            )
            status = campaign_store.status(policy, campaign_id)

        self.assertEqual(outcome.status, "retry_wait")
        self.assertEqual(
            outcome.error_code,
            "benchmark.campaign.dependency."
            "native_reference.store.verification_unavailable",
        )
        self.assertNotIn(secret, json.dumps(status))

    def test_operation_guard_and_loader_failures_are_content_free(self):
        secret = "private native reference text"
        with sqlite3.connect(":memory:") as connection:
            store = STORE.NativeReferenceArtifactStore(connection)
            calls = []
            with self.assertRaises(STORE.NativeReferenceStoreFailed) as caught:
                self.resolve(
                    store,
                    lambda: calls.append("loader"),
                    operation_guard=lambda: (_ for _ in ()).throw(
                        RuntimeError(secret)
                    ),
                )
            self.assertEqual(
                caught.exception.code,
                "native_reference.operation_guard_failed",
            )
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(calls, [])
            with self.assertRaises(STORE.NativeReferenceStoreFailed) as caught:
                self.resolve(
                    store,
                    lambda: (_ for _ in ()).throw(RuntimeError(secret)),
                )
            self.assertEqual(
                caught.exception.code, "native_reference.loader_failed",
            )
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn(secret, str(caught.exception))

    def test_durable_reference_completes_one_campaign_case(self):
        policy = FIXTURES.campaign_policy()
        authority = FIXTURES.CampaignAuthority()
        verifier = FIXTURES.CampaignNativeReferenceVerifier()
        reviewer = FIXTURES.CampaignCandidateReviewer()
        lookups = []
        with (
            sqlite3.connect(":memory:") as campaign_connection,
            sqlite3.connect(":memory:") as reference_connection,
        ):
            campaign_store = FIXTURES.CAMPAIGN.BenchmarkCampaignStore(
                campaign_connection,
            )
            reference_store = STORE.NativeReferenceArtifactStore(
                reference_connection,
            )
            campaign_id = campaign_store.create(policy, now=100)

            def resolve_inputs(payload):
                inputs = FIXTURES.campaign_inputs(
                    payload, policy, verifier, authority,
                )

                def load_reference():
                    lookups.append(payload["job_id"])
                    return inputs.native_reference_artifact

                reference = STORE.resolve_native_reference_artifact(
                    reference_store,
                    payload,
                    policy,
                    "qualified-native-vault-production",
                    load_reference,
                    native_reference_verifier=verifier,
                    evidence_authority=authority,
                    operation_guard=lambda: None,
                    now=100,
                )
                return FIXTURES.FOREIGN_CAMPAIGN.BenchmarkCaseInputs(
                    candidate_result=inputs.candidate_result,
                    baseline_artifact=inputs.baseline_artifact,
                    assets=inputs.assets,
                    native_reference_artifact=reference,
                )

            outcome = FIXTURES.CAMPAIGN.run_next_benchmark_case(
                campaign_store,
                policy,
                campaign_id,
                "benchmark-worker",
                resolve_inputs,
                reviewer,
                blinding_key=b"native-reference-campaign-blinding-key",
                native_reference_verifier=verifier,
                evidence_authority=authority,
                clock=lambda: 100,
            )
            status = campaign_store.status(policy, campaign_id)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(lookups), 1)
        self.assertNotIn(
            FIXTURES.TARGETS["fi-FI"]["reference"],
            json.dumps(status, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
