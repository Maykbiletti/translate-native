"""Synthetic host ledger fixtures: not native-language quality evidence."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_website_localization_runner import RUNNER, IncrementingClock, candidate, review
from test_website_localization_release_coordinator import (
    COORDINATOR, CMS, PLANNER, RELEASE, WORKER, CMSAuthority, ApprovalAuthority,
    EvidenceProvider, ReceiptVerifier, change_event, load, ROOT,
)


SUB = load("test_host_subagents", ROOT / "integrations/website_localization_subagents.py")
TARGETS = {"fi-FI": "Kasvata yritystäsi BLUN-palvelun avulla.",
           "mt-MT": "Kabbar in-negozju tiegħek ma’ BLUN."}


class Creator:
    def __init__(self):
        self.calls = []

    def invoke(self, request):
        self.calls.append(request)
        locale = request.input["target"]["locale"]
        return candidate(locale, TARGETS[locale])


class LedgerHost:
    """In-memory trusted-host double with an authoritative execution ledger.

    Actual isolation/token/deadline enforcement is a host integration obligation,
    not something this fixture or a model response can demonstrate.
    """

    def __init__(self, *, mutate=None, confidence="high", fail=False, error=None):
        self.calls, self.ledger = [], {}
        self.mutate, self.confidence, self.fail, self.error = mutate, confidence, fail, error

    def run_isolated(self, task, *, control):
        self.calls.append((SUB._copy(task), SUB._copy(control)))
        if self.error:
            raise self.error
        key = control["execution_key"]
        if key in self.ledger:
            return SUB._copy(self.ledger[key])
        phase, locale = task["phase"], task["input"]["target"]["locale"]
        findings = [{"class": "native", "excerpt": "fixture", "reason": "synthetic defect"}] if self.fail else []
        response = review(locale, phase, "FAIL" if findings else "PASS", findings,
                          confidence=self.confidence)
        fields = {"schema", "execution_key", "request_sha256", "task_sha256",
                  "phase", "previous_receipt_sha256", "model_id", "model_version",
                  "inherit_context", "tools", "max_delegation_depth"}
        receipt = {name: control[name] for name in fields}
        receipt.update(response_sha256=SUB._hash(response),
                       agent_id="reviewer:" + phase, session_id="isolated:" + key,
                       usage={"fixture": "test-only"})
        reply = {"response": response, "receipt": receipt}
        self.ledger[key] = SUB._copy(reply)
        if self.mutate:
            self.mutate(reply)
        return reply

    def verify_execution(self, receipt, *, control):
        saved = self.ledger.get(control["execution_key"])
        return saved is not None and receipt == saved["receipt"]


def adapter(host=None, **overrides):
    options = dict(creator_id="writer", creator_session_id="writer-session",
                   model_id="configured-model", model_version="model-1",
                   host_policy_version="isolated-host-1",
                   native_brief={"audience": "Business owners", "tone_profile": "Warm and concise",
                                 "target_terms": ["BLUN"]})
    options.update(overrides)
    return SUB.HostSubagentProvider(Creator(), host or LedgerHost(), **options)


def event_for(provider, locale="fi-FI"):
    event = change_event(targets=(locale,))
    event["localization"].update(provider_id=provider.provider_id,
                                 model_id="configured-model", model_version="model-1")
    return event


def planned(provider, locale="fi-FI"):
    return PLANNER.plan_from_mapping(event_for(provider, locale)["localization"])


def worker_assets():
    return WORKER.LocalizationAssets(
        glossary_version="public-glossary-3", policy_version="native-web-1",
        audience="SOURCE_METADATA_SENTINEL", tone_profile="SOURCE_TONE_SENTINEL",
        glossary=(WORKER.GlossaryTerm("SOURCE_GLOSSARY_SENTINEL", "target", "SOURCE_NOTE_SENTINEL"),),
        protected_terms=("BLUN",),
    )


class HostSubagentTests(unittest.TestCase):
    def execute(self, provider, locale="fi-FI"):
        job = planned(provider, locale).jobs[0].as_payload()
        return WORKER.run_localization_job(job, worker_assets(), provider)

    def test_ordered_isolated_reviews_for_finnish_and_maltese(self):
        for locale in TARGETS:
            with self.subTest(locale=locale):
                host = LedgerHost()
                provider = adapter(host)
                result = self.execute(provider, locale)
                self.assertEqual(result["candidate"], TARGETS[locale])
                self.assertTrue(result["release_required"])
                self.assertEqual([t["phase"] for t, _ in host.calls],
                                 ["target_native", "source_fidelity"])
                native, control = host.calls[0]
                serialized = json.dumps(native)
                for forbidden in ("Build your business", "SOURCE_", "job_id", '"source"',
                                  "writer-session", "previous_receipt", "provider_id"):
                    self.assertNotIn(forbidden, serialized)
                self.assertEqual(set(native["input"]), {
                    "candidate", "target", "content_type", "quality_profile", "response_schema",
                    "audience", "tone_profile", "target_terms"})
                self.assertFalse(control["inherit_context"])
                self.assertEqual(control["tools"], [])
                self.assertEqual(control["max_delegation_depth"], 0)
                self.assertEqual(host.calls[1][0]["input"]["source"]["text"],
                                 "Build your business with BLUN.")
                self.assertEqual(host.calls[1][1]["previous_receipt_sha256"],
                                 SUB._hash(host.ledger[control["execution_key"]]["receipt"]))
                for task, ctl in host.calls:
                    reply = host.ledger[ctl["execution_key"]]
                    expected = SUB._hash({"schema": "translate-native.host-review-commitment.v1",
                                          "response": reply["response"],
                                          "host_evidence": reply["receipt"]})
                    actual = next(x for x in result["quality_passes"] if x["phase"] == task["phase"])
                    self.assertEqual(actual["response_sha256"], expected)

    def test_wrong_receipt_bindings_and_self_review_block(self):
        changes = {
            "agent_id": "writer", "session_id": "writer-session",
            "request_sha256": "0" * 64, "task_sha256": "0" * 64,
            "response_sha256": "0" * 64, "phase": "source_fidelity",
            "model_id": "other-model", "inherit_context": True,
            "tools": ["read_file"], "max_delegation_depth": 1,
            "previous_receipt_sha256": "0" * 64,
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                host = LedgerHost(mutate=lambda r: r["receipt"].update({key: value}))
                with self.assertRaises(WORKER.LocalizationWorkerBlocked):
                    self.execute(adapter(host))
                self.assertEqual(len(host.calls), 1)

    def test_model_cannot_forge_host_ledger(self):
        host = LedgerHost(mutate=lambda r: r["receipt"].update(agent_id="forged-reviewer"))
        with self.assertRaisesRegex(WORKER.LocalizationWorkerBlocked, "unverified_execution"):
            self.execute(adapter(host))

    def test_fidelity_cannot_reuse_native_agent_or_session(self):
        for field in ("agent_id", "session_id"):
            host = LedgerHost()
            def mutate(reply):
                if reply["receipt"]["phase"] == "source_fidelity":
                    native = next(iter(host.ledger.values()))["receipt"]
                    reply["receipt"][field] = native[field]
            host.mutate = mutate
            with self.assertRaisesRegex(WORKER.LocalizationWorkerBlocked, "self_review"):
                self.execute(adapter(host))

    def test_native_failure_never_starts_fidelity(self):
        host = LedgerHost(fail=True)
        with self.assertRaisesRegex(WORKER.LocalizationWorkerBlocked, "review.target_native.failed"):
            self.execute(adapter(host))
        self.assertEqual(len(host.calls), 1)

    def test_low_confidence_still_requires_independent_evidence(self):
        result = self.execute(adapter(LedgerHost(confidence="low")))
        self.assertTrue(result["independent_review_required"])
        self.assertEqual(result["review_confidence"], {"target_native": "low", "source_fidelity": "low"})

    def test_unavailable_host_and_invalid_budgets_block(self):
        with self.assertRaisesRegex(SUB.SubagentReviewBlocked, "host_unavailable"):
            adapter(object())
        for kw in ({"timeout_seconds": 0}, {"max_output_tokens": True}):
            with self.assertRaises(SUB.SubagentReviewBlocked):
                adapter(**kw)
        with self.assertRaisesRegex(SUB.SubagentReviewBlocked, "native_brief_required"):
            adapter(native_brief={})
        for error in (TimeoutError("private detail"), RuntimeError("private detail")):
            with self.assertRaises(WORKER.LocalizationWorkerBlocked) as raised:
                self.execute(adapter(LedgerHost(error=error)))
            self.assertTrue(raised.exception.retryable)
            self.assertNotIn("private", str(raised.exception))

    def test_policy_changes_invalidate_job_identity(self):
        first = adapter()
        for kw in ({"host_policy_version": "isolated-host-2"}, {"max_output_tokens": 2048},
                   {"creator_session_id": "new-session"}):
            other = adapter(**kw)
            self.assertNotEqual(first.provider_id, other.provider_id)
            self.assertNotEqual(planned(first).jobs[0].job_id, planned(other).jobs[0].job_id)

    def test_candidate_changes_and_replay_are_rejected(self):
        provider = adapter()
        calls = []
        original = provider.invoke
        def record(request):
            calls.append(request)
            return original(request)
        provider.invoke = record
        self.execute(provider)
        task, control = provider._host.calls[0]
        with self.assertRaises(SUB.SubagentReviewBlocked):
            provider.invoke(provider._creator.calls[0])
        # Review evidence cannot be attached to another response.
        payload = provider._creation
        request = WORKER.ProviderRequest(**payload)
        with self.assertRaises(SUB.SubagentReviewBlocked):
            provider.invoke(replace(request, phase="source_fidelity"))
        with self.assertRaisesRegex(SUB.SubagentReviewBlocked, "evidence_missing"):
            provider.verified_call_evidence(calls[1], review("fi-FI", "target_native", confidence="low"))
        self.assertEqual(control["task_sha256"], SUB._hash(task))

    def test_rebound_candidate_source_and_glossary_changes_block(self):
        for field in ("candidate", "source", "glossary"):
            provider = adapter()
            original = provider.invoke
            job = planned(provider).jobs[0].as_payload()
            def alter(request):
                if request.phase == "source_fidelity":
                    data = SUB._copy(request.input)
                    data[field] = "changed"
                    request = WORKER._request(job, request.phase, request.system_instruction, data)
                return original(request)
            provider.invoke = alter
            with self.assertRaisesRegex(WORKER.LocalizationWorkerBlocked, "subagents.(candidate|source)_binding"):
                self.execute(provider)
            self.assertEqual(len(provider._host.calls), 1)

    def test_native_request_cannot_run_without_creation(self):
        recorder = adapter()
        self.execute(recorder)
        job = planned(recorder).jobs[0].as_payload()
        request = WORKER._request(job, "target_native", "fixture", {"job_id": job["job_id"]})
        with self.assertRaisesRegex(SUB.SubagentReviewBlocked, "phase_order"):
            adapter().invoke(request)

    def test_bad_response_locale_and_missing_receipt_block(self):
        for mutate in (lambda r: r.pop("receipt"),
                       lambda r: r["receipt"].update(extra="unexpected"),
                       lambda r: r["response"].update(locale="sv-SE")):
            with self.assertRaises(WORKER.LocalizationWorkerBlocked):
                self.execute(adapter(LedgerHost(mutate=mutate)))

    def test_identity_only_change_changes_evidence_not_target(self):
        first_host = LedgerHost()
        first = self.execute(adapter(first_host))
        host = LedgerHost()
        original = host.run_isolated
        def different(task, *, control):
            reply = original(task, control=control)
            reply["receipt"]["agent_id"] += ":other"
            host.ledger[control["execution_key"]] = SUB._copy(reply)
            return reply
        host.run_isolated = different
        second = self.execute(adapter(host))
        self.assertEqual(first["target_sha256"], second["target_sha256"])
        self.assertNotEqual(first["quality_passes"][1]["response_sha256"],
                            second["quality_passes"][1]["response_sha256"])
        self.assertEqual(first_host.calls[1][1]["request_sha256"], host.calls[1][1]["request_sha256"])
        self.assertNotEqual(first_host.calls[1][1]["execution_key"], host.calls[1][1]["execution_key"])

    def test_legal_review_still_requires_human(self):
        provider = adapter()
        event = event_for(provider)
        event["localization"]["content_type"] = "legal"
        job = PLANNER.plan_from_mapping(event["localization"]).jobs[0].as_payload()
        result = WORKER.run_localization_job(job, worker_assets(), provider)
        self.assertTrue(result["human_review_required"])
        self.assertTrue(result["release_required"])

    def test_commercial_evidence_remains_in_host_commitment(self):
        from test_commercial_localization import SOURCE, TARGET, evidence
        host = LedgerHost()
        original = host.run_isolated
        def commercial(task, *, control):
            reply = original(task, control=control)
            if task["phase"] == "source_fidelity":
                reply["response"]["commercial_review"] = evidence(SOURCE, TARGET)
                reply["receipt"]["response_sha256"] = SUB._hash(reply["response"])
                host.ledger[control["execution_key"]] = SUB._copy(reply)
            return reply
        host.run_isolated = commercial
        provider = adapter(host)
        provider._creator.invoke = lambda r: candidate("sv-SE", TARGET)
        event = event_for(provider, "sv-SE")
        event["localization"].update(content_type="commercial", source_text=SOURCE)
        job = PLANNER.plan_from_mapping(event["localization"]).jobs[0].as_payload()
        result = WORKER.run_localization_job(job, worker_assets(), provider)
        self.assertEqual(result["commercial_review"]["status"], "verified")
        task, control = host.calls[-1]
        reply = host.ledger[control["execution_key"]]
        expected = SUB._hash({"schema": "translate-native.host-review-commitment.v1",
                              "response": reply["response"], "host_evidence": reply["receipt"]})
        self.assertEqual(result["quality_passes"][-1]["response_sha256"], expected)

    def test_retained_response_mutation_cannot_change_reviewed_snapshot(self):
        provider = adapter()
        original_invoke = provider.invoke
        original_evidence = provider.verified_call_evidence
        retained = {}
        def capture(request):
            response = original_invoke(request)
            retained[request.phase] = response
            return response
        def mutate(request, response):
            proof = original_evidence(request, response)
            if request.phase == "target_native":
                retained[request.phase]["major_defects"].append({
                    "class": "injected", "excerpt": "after review", "reason": "not reviewed"})
            return proof
        provider.invoke, provider.verified_call_evidence = capture, mutate
        result = self.execute(provider)
        self.assertTrue(result["release_required"])
        native_ctl = provider._host.calls[0][1]
        saved = provider._host.ledger[native_ctl["execution_key"]]
        self.assertEqual(result["quality_passes"][1]["response_sha256"], SUB._hash({
            "schema": "translate-native.host-review-commitment.v1",
            "response": saved["response"], "host_evidence": saved["receipt"]}))

    def test_legacy_provider_cannot_service_delegated_identity(self):
        configured = adapter()
        with self.assertRaisesRegex(WORKER.LocalizationWorkerBlocked, "evidence.required"):
            WORKER.run_localization_job(planned(configured).jobs[0].as_payload(),
                                        worker_assets(), Creator())

    def test_evidence_callback_mutation_or_missing_evidence_blocks(self):
        for mutate in (True, False):
            provider = adapter()
            original = provider.verified_call_evidence
            def broken(request, response):
                if request.phase == "transcreation":
                    return original(request, response)
                if mutate:
                    request.input["candidate"] = "changed after review"
                    return {"fake": True}
                return None
            provider.verified_call_evidence = broken
            with self.assertRaises(WORKER.LocalizationWorkerBlocked):
                self.execute(provider)

    def test_queue_restart_and_signed_release_commit_to_host_evidence(self):
        host = LedgerHost()
        configured = adapter(host)
        event = event_for(configured)
        plan = PLANNER.plan_from_mapping(event["localization"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            db = sqlite3.connect(path)
            queue = CMS._QUEUE.LocalizationQueue(db)
            release_db, cms_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
            store = RELEASE.LocalizationReleaseStore(release_db, queue)
            bridge = CMS.WebsiteLocalizationCMSBridge(cms_db, queue, store)
            authority = CMSAuthority(b"fixture-event-key")
            bridge.ingest_change(event, authority.sign(CMS._canonical_json(event).encode()), authority, now=90)
            # Exercise actual registered provider resolver and runner.
            outcome = RUNNER.run_next_localization_job(
                queue, "worker", lambda _: configured,
                lambda _: RUNNER._WORKER.LocalizationAssets(
                    glossary_version="public-glossary-3", policy_version="native-web-1",
                    audience="Website visitors", tone_profile="Concise", protected_terms=("BLUN",)),
                clock=IncrementingClock(), lease_seconds=20)
            self.assertEqual(outcome.status, "succeeded")
            db.close()
            db = sqlite3.connect(path)
            queue = CMS._QUEUE.LocalizationQueue(db)
            store = RELEASE.LocalizationReleaseStore(release_db, queue)
            bridge = CMS.WebsiteLocalizationCMSBridge(cms_db, queue, store)
            result = store.validated_result(plan, plan.jobs[0].job_id)
            evidence, signer = EvidenceProvider(), ApprovalAuthority()
            evidence_db = sqlite3.connect(":memory:")
            outcome = COORDINATOR.run_next_release(
                bridge, event["event_id"], authority, evidence,
                ReceiptVerifier("quality", evidence.requests), signer, CMSAuthority(b"fixture-publish-key"),
                evidence_revision="test-evidence-1", now=200, approval_ttl_seconds=1000,
                evidence_state=COORDINATOR.QualityEvidenceStateStore(evidence_db),
                evidence_worker_id="evidence-worker")
            self.assertEqual(outcome.status, "delivery_ready")
            self.assertEqual(signer.sign_calls, 1)
            self.assertEqual(evidence.requests[0].result_sha256, SUB._hash(result))
            changed = SUB._copy(result)
            changed["quality_passes"][1]["response_sha256"] = "0" * 64
            self.assertNotEqual(SUB._hash(changed), evidence.requests[0].result_sha256)
            store.lookup(plan, plan.jobs[0].job_id, signer, now=201)
            release_db.execute("UPDATE localization_approvals SET result_json = ?, result_sha256 = ?",
                               (SUB._raw(changed).decode(), SUB._hash(changed)))
            release_db.commit()
            with self.assertRaisesRegex(RELEASE.LocalizationReleaseBlocked, "approval.binding_mismatch"):
                store.lookup(plan, plan.jobs[0].job_id, signer, now=201)
            db.close()
            release_db.close()
            cms_db.close()
            evidence_db.close()

    def test_queue_owns_bounded_retries_and_host_deduplicates_reviews(self):
        host = LedgerHost()
        first = adapter(host)
        result = self.execute(first)
        second = adapter(host)
        replay = self.execute(second)
        self.assertEqual(result, replay)
        self.assertEqual(len(host.ledger), 2)
        configured = adapter(LedgerHost(error=TimeoutError()))
        db = sqlite3.connect(":memory:")
        queue = RUNNER._QUEUE.LocalizationQueue(db)
        queue.enqueue_plan(planned(configured), max_attempts=1, now=90)
        outcome = RUNNER.run_next_localization_job(
            queue, "worker", lambda _: configured,
            lambda _: RUNNER._WORKER.LocalizationAssets(
                glossary_version="public-glossary-3", policy_version="native-web-1",
                audience="Visitors", tone_profile="Concise"),
            clock=IncrementingClock(), lease_seconds=20)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "provider.subagents.timeout")
        with self.assertRaises(RUNNER._QUEUE.LocalizationQueueBlocked):
            queue.result(planned(configured).jobs[0].job_id)
        db.close()


if __name__ == "__main__":
    unittest.main()
