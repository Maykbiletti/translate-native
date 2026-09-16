"""Synthetic host fixtures; these tests are not native-language quality evidence."""

from __future__ import annotations

import importlib.util
import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "integrations" / "response_subagent_review.py"
SPEC = importlib.util.spec_from_file_location("test_response_subagent_review_impl", PATH)
assert SPEC and SPEC.loader
REVIEW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = REVIEW
SPEC.loader.exec_module(REVIEW)


TARGETS = {
    "fi-FI": "Voit hallita tilaustasi milloin tahansa.",
    "mt-MT": "Tista’ timmaniġġja l-abbonament tiegħek fi kwalunkwe ħin.",
}


def identity(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


CREATOR = {
    "creator_id_sha256": identity("creator-main"),
    "creator_session_id_sha256": identity("creator-session"),
}


class LedgerHost:
    def __init__(self, *, confidence="high", mutate=None, error=None):
        self.calls, self.ledger = [], {}
        self.confidence, self.mutate, self.error = confidence, mutate, error

    def run_isolated(self, task, *, control):
        self.calls.append((REVIEW._copy(task), REVIEW._copy(control)))
        if self.error:
            raise self.error
        response = {
            "schema": REVIEW.RESPONSE_SCHEMA,
            "phase": "target_native",
            "locale": task["input"]["target"]["locale"],
            "status": "PASS",
            "confidence": self.confidence,
            "findings": [],
            "uncertainties": [],
        }
        receipt = {
            key: control[key] for key in {
                "schema", "execution_key", "request_sha256", "task_sha256",
                "phase", "previous_receipt_sha256", "model_id", "model_version",
                "inherit_context", "tools", "max_delegation_depth",
                "reviewer_role", "assignment_id",
            }
        }
        receipt.update(response_sha256=REVIEW._hash(response),
                       agent_id="native-reviewer", session_id="isolated-review-session")
        reply = {"response": response, "receipt": receipt}
        self.ledger[control["execution_key"]] = REVIEW._copy(reply)
        if self.mutate:
            self.mutate(reply)
        return reply

    def verify_execution(self, receipt, *, control):
        saved = self.ledger.get(control["execution_key"])
        return saved is not None and receipt == saved["receipt"]


def reviewer(host=None):
    return REVIEW.ResponseSubagentReviewer(
        host or LedgerHost(), model_id="review-model", model_version="model-1",
        host_policy_version="isolated-host-1",
        quality_profile_version="eu-native-1", prompt_version="native-prompt-1",
        software_version="6.185.0",
        native_brief={"audience": "Website users", "tone_profile": "Natural and clear",
                      "target_terms": ["Translate Native"]},
    )


class ResponseSubagentReviewTests(unittest.TestCase):
    def test_finnish_and_maltese_are_source_blind_and_bound(self):
        for locale, target in TARGETS.items():
            with self.subTest(locale=locale):
                host = LedgerHost()
                result = reviewer(host).review(
                    target, locale, "prose", **CREATOR,
                )
                self.assertEqual(result["evidence"]["target_locale"], locale)
                self.assertEqual(result["evidence_sha256"], REVIEW._hash(result["evidence"]))
                task, control = host.calls[0]
                self.assertEqual(set(task["input"]), {
                    "candidate", "target", "content_type", "quality_profile",
                    "response_schema", "audience", "tone_profile", "target_terms",
                })
                self.assertNotIn("source", task["input"])
                self.assertNotIn("creator_id", task["input"])
                self.assertFalse(control["inherit_context"])
                self.assertEqual(control["tools"], [])
                self.assertEqual(control["max_delegation_depth"], 0)
                self.assertEqual(control["reviewer_role"], "target-native-reviewer")
                self.assertRegex(control["assignment_id"], r"^[0-9a-f]{64}$")

    def test_self_review_wrong_binding_and_changed_candidate_block(self):
        mutations = {
            "self": lambda reply: reply["receipt"].update(agent_id="creator-main"),
            "phase": lambda reply: reply["receipt"].update(phase="source_fidelity"),
            "role": lambda reply: reply["receipt"].update(reviewer_role="creator"),
            "locale": lambda reply: reply["response"].update(locale="sv-SE"),
            "digest": lambda reply: reply["receipt"].update(response_sha256="0" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                with self.assertRaises(REVIEW.ResponseReviewBlocked):
                    reviewer(LedgerHost(mutate=mutate)).review(
                        TARGETS["fi-FI"], "fi-FI", "prose",
                        **CREATOR,
                    )
        first = reviewer().review(
            TARGETS["fi-FI"], "fi-FI", "prose",
            **CREATOR,
        )
        second = reviewer().review(
            TARGETS["fi-FI"] + " Nyt.", "fi-FI", "prose",
            **CREATOR,
        )
        self.assertNotEqual(first["evidence_sha256"], second["evidence_sha256"])

    def test_low_confidence_timeout_and_unavailable_host_fail_closed(self):
        with self.assertRaisesRegex(REVIEW.ResponseReviewBlocked, "independent_review_required"):
            reviewer(LedgerHost(confidence="low")).review(
                TARGETS["mt-MT"], "mt-MT", "prose",
                **CREATOR,
            )
        with self.assertRaisesRegex(REVIEW.ResponseReviewBlocked, "timeout") as timeout:
            reviewer(LedgerHost(error=TimeoutError())).review(
                TARGETS["fi-FI"], "fi-FI", "prose",
                **CREATOR,
            )
        self.assertTrue(timeout.exception.retryable)
        with self.assertRaisesRegex(REVIEW.ResponseReviewBlocked, "host_unavailable"):
            reviewer(object())

    def test_permanent_host_verification_failure_stays_terminal(self):
        class HostFailure(RuntimeError):
            host_subagent_failure = True
            code = "http.attestation_rejected"
            retryable = False

        class VerificationFailureHost(LedgerHost):
            def verify_execution(self, receipt, *, control):
                raise HostFailure()

        with self.assertRaisesRegex(
            REVIEW.ResponseReviewBlocked, "http.attestation_rejected",
        ) as blocked:
            reviewer(VerificationFailureHost()).review(
                TARGETS["fi-FI"], "fi-FI", "prose", **CREATOR,
            )
        self.assertFalse(blocked.exception.retryable)

    def test_finding_uncertainty_and_type_confusion_block(self):
        class PermissiveHost(LedgerHost):
            def verify_execution(self, receipt, *, control):
                return True

        def uncertain(reply):
            reply["response"]["findings"] = [{
                "code": "idiom", "severity": "minor", "reason": "Needs review",
                "uncertainty": "Register evidence is incomplete",
            }]
            reply["receipt"]["response_sha256"] = REVIEW._hash(reply["response"])

        with self.assertRaisesRegex(
            REVIEW.ResponseReviewBlocked, "independent_review_required",
        ):
            reviewer(PermissiveHost(mutate=uncertain)).review(
                TARGETS["mt-MT"], "mt-MT", "prose", **CREATOR,
            )

        with self.assertRaisesRegex(REVIEW.ResponseReviewBlocked, "receipt_binding"):
            reviewer(PermissiveHost(
                mutate=lambda reply: reply["receipt"].update(inherit_context=0),
            )).review(TARGETS["fi-FI"], "fi-FI", "prose", **CREATOR)


if __name__ == "__main__":
    unittest.main()
