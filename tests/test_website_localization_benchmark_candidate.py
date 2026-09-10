from __future__ import annotations

import copy
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


CANDIDATE = load(
    "blun_test_website_localization_benchmark_candidate",
    ROOT / "integrations" / "website_localization_benchmark_candidate.py",
)
FIXTURES = load(
    "blun_test_benchmark_candidate_fixtures",
    ROOT / "tests" / "test_website_localization_benchmark.py",
)


class SuccessfulProvider:
    def __init__(self, candidate="Kasvata yritystäsi BLUNin avulla."):
        self.candidate = candidate
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        if request.phase == "transcreation":
            return {
                "schema": CANDIDATE._WORKER.CANDIDATE_SCHEMA,
                "phase": request.phase,
                "locale": request.input["target"]["locale"],
                "candidate": self.candidate,
            }
        return {
            "schema": CANDIDATE._WORKER.REVIEW_SCHEMA,
            "phase": request.phase,
            "locale": request.input["target"]["locale"],
            "status": "PASS",
            "confidence": "high",
            "blocking_defects": [],
            "major_defects": [],
        }


class TracedAuthority(FIXTURES.HmacBenchmarkAuthority):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def sign(self, payload):
        self.events.append("sign")
        return super().sign(payload)

    def verify(self, payload, signature):
        self.events.append("verify")
        return super().verify(payload, signature)


class BenchmarkCandidateAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.policy = FIXTURES.policy()
        self.job = FIXTURES.job("fi-FI", "0")
        self.authority = FIXTURES.HmacBenchmarkAuthority()

    def assets(self):
        source = FIXTURES.assets("fi-FI")
        return CANDIDATE._WORKER.LocalizationAssets(
            glossary_version=source.glossary_version,
            policy_version=source.policy_version,
            audience=source.audience,
            tone_profile=source.tone_profile,
            glossary=tuple(
                CANDIDATE._WORKER.GlossaryTerm(term.source, term.target, term.note)
                for term in source.glossary
            ),
            protected_terms=source.protected_terms,
        )

    def resolve(self, store, provider, **kwargs):
        return CANDIDATE.resolve_candidate_acquisition(
            store,
            self.job,
            self.policy,
            "customer-model-production",
            self.assets(),
            provider,
            evidence_authority=self.authority,
            now=100,
            **kwargs,
        )

    def test_restart_reuses_exact_attested_candidate_without_model_calls(self):
        provider = SuccessfulProvider()
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "candidates.sqlite3")
            with sqlite3.connect(database) as connection:
                store = CANDIDATE.CandidateAcquisitionStore(connection)
                first = self.resolve(store, provider)
            with sqlite3.connect(database) as connection:
                restarted = CANDIDATE.CandidateAcquisitionStore(connection)
                second = self.resolve(
                    restarted,
                    None,
                )

        self.assertEqual(first, second)
        self.assertEqual(first["candidate"], provider.candidate)
        self.assertEqual(len(provider.requests), 3)

    def test_route_policy_job_and_locale_changes_cannot_reuse_candidate(self):
        with sqlite3.connect(":memory:") as connection:
            store = CANDIDATE.CandidateAcquisitionStore(connection)
            store.save(
                self.job,
                self.policy,
                "customer-model-production",
                FIXTURES.candidate_result(self.job),
                evidence_authority=self.authority,
                now=100,
            )
            self.assertIsNone(store.load(
                self.job,
                self.policy,
                "different-customer-route",
                evidence_authority=self.authority,
            ))
            changed_policy = FIXTURES.policy(baseline_version="new-baseline")
            self.assertIsNone(store.load(
                self.job,
                changed_policy,
                "customer-model-production",
                evidence_authority=self.authority,
            ))
            for changed_job in (
                FIXTURES.job("fi-FI", "1"),
                FIXTURES.job("mt-MT", "0"),
            ):
                self.assertIsNone(store.load(
                    changed_job,
                    self.policy,
                    "customer-model-production",
                    evidence_authority=self.authority,
                ))

    def test_corrupt_digest_blocks_before_model_access(self):
        calls = []
        with sqlite3.connect(":memory:") as connection:
            store = CANDIDATE.CandidateAcquisitionStore(connection)
            store.save(
                self.job, self.policy, "customer-model-production",
                FIXTURES.candidate_result(self.job),
                evidence_authority=self.authority, now=100,
            )
            connection.execute("""
                UPDATE benchmark_candidate_acquisitions
                SET artifact_json = artifact_json || ' '
            """)
            connection.commit()
            with self.assertRaises(CANDIDATE.CandidateAcquisitionFailed) as caught:
                self.resolve(
                    store,
                    type("Provider", (), {
                        "invoke": lambda self, request: calls.append(request)
                    })(),
                )

        self.assertEqual(caught.exception.code, "candidate.store.state_invalid")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(calls, [])

    def test_rehashed_candidate_tamper_fails_attestation_before_model(self):
        calls = []
        with sqlite3.connect(":memory:") as connection:
            store = CANDIDATE.CandidateAcquisitionStore(connection)
            store.save(
                self.job, self.policy, "customer-model-production",
                FIXTURES.candidate_result(self.job),
                evidence_authority=self.authority, now=100,
            )
            row = connection.execute("""
                SELECT artifact_json FROM benchmark_candidate_acquisitions
            """).fetchone()
            artifact = json.loads(row[0])
            changed = "Tämä on sujuva mutta erilainen suomalainen ehdokasteksti."
            artifact["candidate_result"]["candidate"] = changed
            artifact["candidate_result"]["target_sha256"] = hashlib.sha256(
                changed.encode("utf-8")
            ).hexdigest()
            serialized = json.dumps(
                artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            connection.execute("""
                UPDATE benchmark_candidate_acquisitions
                SET artifact_json = ?, artifact_sha256 = ?
            """, (serialized, hashlib.sha256(serialized.encode()).hexdigest()))
            connection.commit()
            with self.assertRaises(CANDIDATE.CandidateAcquisitionFailed) as caught:
                self.resolve(
                    store,
                    type("Provider", (), {
                        "invoke": lambda self, request: calls.append(request)
                    })(),
                )

        self.assertEqual(caught.exception.code, "candidate.store.artifact_invalid")
        self.assertEqual(calls, [])

    def test_identical_writers_converge_but_different_candidate_conflicts(self):
        result = FIXTURES.candidate_result(self.job)
        changed = FIXTURES.candidate_result(
            self.job, "Kasvata liiketoimintaasi luontevasti.",
        )
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "candidates.sqlite3")
            with (
                sqlite3.connect(database) as first_connection,
                sqlite3.connect(database) as second_connection,
            ):
                first_store = CANDIDATE.CandidateAcquisitionStore(
                    first_connection,
                )
                second_store = CANDIDATE.CandidateAcquisitionStore(
                    second_connection,
                )
                first = first_store.save(
                    self.job, self.policy, "customer-model-production", result,
                    evidence_authority=self.authority, now=100,
                )
                repeated = second_store.save(
                    self.job, self.policy, "customer-model-production", result,
                    evidence_authority=self.authority, now=101,
                )
                with self.assertRaises(
                    CANDIDATE.CandidateAcquisitionFailed,
                ) as caught:
                    second_store.save(
                        self.job, self.policy, "customer-model-production", changed,
                        evidence_authority=self.authority, now=102,
                    )
                retained = first_store.load(
                    self.job, self.policy, "customer-model-production",
                    evidence_authority=self.authority,
                )

        self.assertEqual(first, repeated)
        self.assertEqual(retained, first)
        self.assertEqual(caught.exception.code, "candidate.store.conflict")
        self.assertFalse(caught.exception.retryable)

    def test_cached_authority_outage_is_retryable_without_model_access(self):
        class UnavailableAuthority:
            def sign(self, payload):
                raise AssertionError("cached result must not be signed again")

            def verify(self, payload, signature):
                raise RuntimeError("temporary authority outage")

        calls = []
        with sqlite3.connect(":memory:") as connection:
            store = CANDIDATE.CandidateAcquisitionStore(connection)
            store.save(
                self.job, self.policy, "customer-model-production",
                FIXTURES.candidate_result(self.job),
                evidence_authority=self.authority, now=100,
            )
            with self.assertRaises(CANDIDATE.CandidateAcquisitionFailed) as caught:
                CANDIDATE.resolve_candidate_acquisition(
                    store,
                    self.job,
                    self.policy,
                    "customer-model-production",
                    self.assets(),
                    type("Provider", (), {
                        "invoke": lambda self, request: calls.append(request)
                    })(),
                    evidence_authority=UnavailableAuthority(),
                )

        self.assertEqual(
            caught.exception.code, "candidate.store.attestation_unavailable",
        )
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(calls, [])

    def test_guard_precedes_every_model_and_attestation_operation(self):
        events = []
        authority = TracedAuthority(events)
        provider = SuccessfulProvider()

        class TracedProvider:
            def invoke(_, request):
                events.append("provider")
                return provider.invoke(request)

        with sqlite3.connect(":memory:") as connection:
            store = CANDIDATE.CandidateAcquisitionStore(connection)
            CANDIDATE.resolve_candidate_acquisition(
                store,
                self.job,
                self.policy,
                "customer-model-production",
                self.assets(),
                TracedProvider(),
                evidence_authority=authority,
                operation_guard=lambda: events.append("guard"),
                now=100,
            )
            CANDIDATE.resolve_candidate_acquisition(
                store,
                self.job,
                self.policy,
                "customer-model-production",
                self.assets(),
                TracedProvider(),
                evidence_authority=authority,
                operation_guard=lambda: events.append("guard"),
                now=101,
            )

        self.assertEqual(events[::2], ["guard"] * (len(events) // 2))
        self.assertEqual(
            events[1::2],
            ["provider", "provider", "provider", "sign", "verify",
             "verify", "verify", "verify"],
        )

    def test_worker_and_guard_failures_are_content_free_and_retryable(self):
        secret = "private model prompt and output"
        with sqlite3.connect(":memory:") as connection:
            store = CANDIDATE.CandidateAcquisitionStore(connection)
            with self.assertRaises(CANDIDATE.CandidateAcquisitionFailed) as caught:
                self.resolve(
                    store,
                    SuccessfulProvider(),
                    operation_guard=lambda: (_ for _ in ()).throw(
                        RuntimeError(secret)
                    ),
                )
            self.assertEqual(
                caught.exception.code,
                "candidate.worker.provider.candidate.operation_guard_failed",
            )
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn(secret, str(caught.exception))

    def test_store_failure_uses_campaign_retry_policy_without_content(self):
        secret = "confidential Finnish candidate"
        policy = FIXTURES.campaign_policy()
        with sqlite3.connect(":memory:") as connection:
            campaign_store = FIXTURES.CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = campaign_store.create(policy, max_attempts=2, now=100)

            def unavailable(_):
                raise CANDIDATE.CandidateAcquisitionFailed(
                    "candidate.store.attestation_unavailable", retryable=True,
                ) from RuntimeError(secret)

            outcome = FIXTURES.CAMPAIGN.run_next_benchmark_case(
                campaign_store,
                policy,
                campaign_id,
                "benchmark-worker",
                unavailable,
                FIXTURES.CampaignCandidateReviewer(),
                blinding_key=b"candidate-campaign-blinding-key-material",
                native_reference_verifier=FIXTURES.CampaignNativeReferenceVerifier(),
                evidence_authority=FIXTURES.CampaignAuthority(),
                clock=lambda: 100,
                retry_base_seconds=5,
            )
            status = campaign_store.status(policy, campaign_id)

        self.assertEqual(outcome.status, "retry_wait")
        self.assertEqual(
            outcome.error_code,
            "benchmark.campaign.dependency.candidate.store.attestation_unavailable",
        )
        self.assertNotIn(secret, json.dumps(status))

    def test_durable_candidate_completes_one_campaign_case_without_text_leak(self):
        policy = FIXTURES.campaign_policy()
        authority = FIXTURES.CampaignAuthority()
        verifier = FIXTURES.CampaignNativeReferenceVerifier()
        reviewer = FIXTURES.CampaignCandidateReviewer()
        with (
            sqlite3.connect(":memory:") as campaign_connection,
            sqlite3.connect(":memory:") as candidate_connection,
        ):
            campaign_store = FIXTURES.CAMPAIGN.BenchmarkCampaignStore(
                campaign_connection,
            )
            candidate_store = CANDIDATE.CandidateAcquisitionStore(
                candidate_connection,
            )
            campaign_id = campaign_store.create(policy, now=100)

            def resolve_inputs(payload):
                inputs = FIXTURES.campaign_inputs(
                    payload, policy, verifier, authority,
                )
                candidate = candidate_store.save(
                    payload,
                    policy,
                    "customer-model-production",
                    inputs.candidate_result,
                    evidence_authority=authority,
                    now=100,
                )
                return FIXTURES.FOREIGN_CAMPAIGN.BenchmarkCaseInputs(
                    candidate_result=candidate,
                    baseline_artifact=inputs.baseline_artifact,
                    assets=inputs.assets,
                    native_reference_artifact=inputs.native_reference_artifact,
                )

            outcome = FIXTURES.CAMPAIGN.run_next_benchmark_case(
                campaign_store,
                policy,
                campaign_id,
                "benchmark-worker",
                resolve_inputs,
                reviewer,
                blinding_key=b"candidate-campaign-blinding-key-material",
                native_reference_verifier=verifier,
                evidence_authority=authority,
                clock=lambda: 100,
            )
            status = campaign_store.status(policy, campaign_id)

        self.assertEqual(outcome.status, "succeeded", outcome)
        self.assertNotIn(
            FIXTURES.TARGETS["fi-FI"]["candidate"],
            json.dumps(status, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
