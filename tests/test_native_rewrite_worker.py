"""Synthetic adapter fixtures, not native quality or authorship evidence."""
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "test_native_rewrite_module", ROOT / "integrations/native_rewrite_worker.py")
RW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RW)


class Creator:
    def __init__(self, target, error=None):
        self.target, self.error, self.calls = target, error, []

    def invoke(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return {"schema": RW.WORKER.CANDIDATE_SCHEMA, "phase": "transcreation",
                "locale": request.input["target"]["locale"], "candidate": self.target}


class Host:
    """Trusted in-memory fixture ledger; no actual model is called."""
    def __init__(self, confidence="high", mutation=None, error=None):
        self.confidence, self.mutation, self.error = confidence, mutation, error
        self.calls, self.ledger = [], {}

    def run_isolated(self, task, *, control):
        self.calls.append((task, control))
        if self.error:
            raise self.error
        key = control["execution_key"]
        if key not in self.ledger:
            response = {"schema": RW.WORKER.REVIEW_SCHEMA, "phase": task["phase"],
                        "locale": task["input"]["target"]["locale"], "status": "PASS",
                        "confidence": self.confidence, "blocking_defects": [], "major_defects": []}
            fields = {"schema", "execution_key", "request_sha256", "task_sha256",
                      "phase", "previous_receipt_sha256", "model_id", "model_version",
                      "inherit_context", "tools", "max_delegation_depth"}
            receipt = {k: control[k] for k in fields}
            receipt.update(response_sha256=RW.SUBAGENTS._hash(response),
                           agent_id="reviewer:" + task["phase"], session_id="isolated:" + key,
                           usage={"fixture": "test-only"})
            self.ledger[key] = {"response": response, "receipt": receipt}
        reply = json.loads(json.dumps(self.ledger[key]))
        if self.mutation:
            self.mutation(reply)
        return reply

    def verify_execution(self, receipt, *, control):
        return receipt == self.ledger[control["execution_key"]]["receipt"]


def profile(locale="fi-FI", **updates):
    return {"locale": locale, "audience": "General readers", "tone_profile": "Clear and personal",
            "target_terms": [], "profile_version": "fixture-profile-v1",
            "prompt_version": "fixture-prompt-v1", "software_version": "fixture-software-v1",
            "native_evidence": {
                "version": "fixture-not-real-quality-evidence-v1", "sha256": "f" * 64,
                "locale": locale, "dialect": updates.get("dialect"),
                "content_types": sorted(RW.TYPES), "reviewer_kind": "qualified_native_reference",
                "reviewer_id": "synthetic-fixture-native-reference"},
            **updates}


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "rewrite.sqlite"

    def worker(self, target="Tämä on selkeä teksti.", host=None, locale="fi-FI", **options):
        creator = options.pop("creator", Creator(target))
        worker = RW.NativeRewriteWorker(
            creator, host or Host(), ledger_path=self.path,
            creator_id="writer", creator_session_id="writer-session",
            model_id="fixture-model", model_version="fixture-model-1",
            host_policy_version="fixture-host-v1", profile=options.pop("profile", profile(locale)),
            **options)
        return worker, creator

    def test_locales_original_native_then_fidelity_and_exact_evidence(self):
        for index, (locale, original, revised) in enumerate((
            ("fi-FI", "On tärkeää huomata, että teksti on selkeä.", "Teksti on selkeä."),
            ("mt-MT", "Huwa importanti li ngħidu li t-test huwa ċar.", "It-test huwa ċar."),
            ("zh-Hant-TW", "需要指出的是，這段文字很清楚。", "這段文字很清楚。"),
            ("ar", "من المهم أن نذكر أن النص واضح.", "النص واضح."),
        )):
            host = Host()
            worker, creator = self.worker(revised, host, locale)
            result = worker.run(original, "prose", "case-" + str(index))
            self.assertEqual(result["target_text"], revised)
            self.assertNotIn("release_token", result)
            self.assertEqual(result["evidence_sha256"], RW._hash(result["evidence"]))
            self.assertEqual(result["evidence"]["source_sha256"], RW._text_hash(original))
            self.assertEqual([task["phase"] for task, _ in host.calls],
                             ["target_native", "source_fidelity"])
            native, control = host.calls[0]
            self.assertNotIn(original, json.dumps(native, ensure_ascii=False))
            for forbidden in ("source", "job_id", "provider_id", "creator_session_id"):
                self.assertNotIn('"' + forbidden + '"', json.dumps(native))
            self.assertEqual(control["tools"], [])
            self.assertFalse(control["inherit_context"])
            self.assertEqual(host.calls[1][0]["input"]["source"]["text"], original)
            self.assertEqual(len(creator.calls), 1)

    def test_idempotency_restart_reverifies_host_but_never_recreates(self):
        host = Host()
        first, creator = self.worker(host=host)
        result = first.run("Original text.", "prose", "same")
        other, new_creator = self.worker(host=host)
        self.assertEqual(other.run("Original text.", "prose", "same"), result)
        self.assertEqual(len(creator.calls), 1)
        self.assertEqual(new_creator.calls, [])
        self.assertEqual(len(host.ledger), 2)

    def test_changed_text_profile_and_type_cannot_reuse_request(self):
        worker, creator = self.worker()
        worker.run("Original.", "prose", "bound")
        for source, kind in (("Changed.", "prose"), ("Original.", "marketing")):
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, "idempotency_conflict"):
                worker.run(source, kind, "bound")
        revised, _ = self.worker(profile=profile(prompt_version="v2"))
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "idempotency_conflict"):
            revised.run("Original.", "prose", "bound")
        self.assertEqual(len(creator.calls), 1)

    def test_unchanged_and_condensed_text_not_translation_identity_or_volume_errors(self):
        for i, text in enumerate(("Tämä on jo hyvä teksti.", "la la la " * 4000)):
            worker, _ = self.worker(text)
            self.assertEqual(worker.run(text, "prose", "unchanged-" + str(i))["target_text"], text)
        worker, _ = self.worker("Selkeä teksti.")
        result = worker.run("Tämä on tärkeää. " * 2000, "prose", "long")
        self.assertEqual(result["target_text"], "Selkeä teksti.")

    def test_corrupt_receipts_locales_self_review_and_confidence_block(self):
        cases = [lambda r: r["receipt"].update(agent_id="writer"),
                 lambda r: r["receipt"].update(phase="source_fidelity"),
                 lambda r: r["response"].update(locale="mt-MT"),
                 lambda r: r["receipt"].update(request_sha256="0" * 64)]
        for index, mutate in enumerate(cases):
            worker, _ = self.worker(host=Host(mutation=mutate))
            with self.assertRaises(RW.NativeRewriteBlocked):
                worker.run("Original.", "prose", "invalid-" + str(index))
        host = Host(confidence="low")
        worker, _ = self.worker(host=host)
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "independent_review_required"):
            worker.run("Original.", "prose", "low")
        self.assertEqual(len(host.calls), 1)

    def test_failures_are_terminal_and_crash_ambiguity_does_not_create(self):
        creator = Creator("unused", TimeoutError())
        worker, _ = self.worker(creator=creator)
        for _ in range(2):
            with self.assertRaises(RW.NativeRewriteBlocked):
                worker.run("Original.", "prose", "failure")
        self.assertEqual(len(creator.calls), 1)
        clean, creator = self.worker()
        binding = RW._hash({"source_sha256": RW._text_hash("Original."),
                            "profile_policy": clean._policy_hash,
                            "content_type": "prose", "request_id": "crashed"})
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT INTO native_rewrites VALUES (?,?,?,NULL,NULL)",
                               ("crashed", binding, "creating"))
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "creation_outcome_unknown"):
            clean.run("Original.", "prose", "crashed")
        self.assertFalse(creator.calls)

    def test_crash_after_creation_resumes_persisted_candidate(self):
        class SimulatedCrash(BaseException):
            pass
        host = Host(error=SimulatedCrash())
        worker, creator = self.worker(host=host)
        with self.assertRaises(SimulatedCrash):
            worker.run("Original.", "prose", "resume")
        host.error = None
        restored, never_called = self.worker(host=host)
        result = restored.run("Original.", "prose", "resume")
        self.assertEqual(result["target_text"], creator.target)
        self.assertEqual(len(creator.calls), 1)
        self.assertFalse(never_called.calls)

    def test_concurrent_same_request_reserves_creator_before_external_work(self):
        entered, release = threading.Event(), threading.Event()
        class WaitingCreator(Creator):
            def invoke(self, request):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError
                return super().invoke(request)
        creator = WaitingCreator("Selkeä teksti.")
        worker, _ = self.worker(creator=creator)
        other, second_creator = self.worker()
        outcomes = []
        def execute():
            try:
                outcomes.append(worker.run("Original.", "prose", "concurrent"))
            except Exception as error:
                outcomes.append(error)
        thread = threading.Thread(target=execute)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, "creation_outcome_unknown"):
                other.run("Original.", "prose", "concurrent")
            self.assertFalse(second_creator.calls)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(len(creator.calls), 1)

    def test_protected_structure_code_and_distinct_numbers(self):
        safe = 'Pay 12, not 21. {{name}} `x=1` https://example.test/'
        self.assertEqual(RW.integrity_errors(safe, safe), [])
        self.assertTrue(RW.integrity_errors(safe, safe.replace("x=1", "x=2")))
        self.assertTrue(RW.integrity_errors('{"a":"Hei"}', '{"b":"Hei"}'))
        worker, _ = self.worker("Hei {{other}}")
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "integrity_failed"):
            worker.run("Hei {{name}}", "prose", "syntax")

    def test_dialect_is_explicit_source_free_profile_and_unavailable_host_blocks(self):
        host = Host()
        worker, _ = self.worker(host=host, profile=profile("de-AT", dialect="Viennese, restrained"))
        worker.run("Original.", "prose", "dialect")
        self.assertEqual(host.calls[0][0]["input"]["target"]["dialect"], "Viennese, restrained")
        with self.assertRaises(RW.NativeRewriteBlocked):
            self.worker(host=object())
        for invalid in ({"source": "SECRET"}, {"locale": "auto"}, {"dialect": ""}):
            with self.assertRaises(RW.NativeRewriteBlocked):
                self.worker(profile=profile(**invalid))

    def test_missing_or_inapplicable_native_evidence_never_starts_model(self):
        for index, change in enumerate((None, {"locale": "wrong"}, {"dialect": "other"},
                                        {"content_types": ["ui"]},
                                        {"reviewer_kind": "independent_model_evaluation",
                                         "reviewer_id": "fixture-model"})):
            configured = profile()
            if change is None:
                configured.pop("native_evidence")
            else:
                configured["native_evidence"].update(change)
            worker, creator = self.worker(profile=configured)
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, "independent_review_required"):
                worker.run("Original.", "prose", "evidence-" + str(index))
            self.assertFalse(creator.calls)

    def test_effective_profile_binding_includes_budget_and_host_policy(self):
        first, _ = self.worker()
        second, _ = self.worker(max_output_tokens=2048)
        self.assertNotEqual(first.profile_sha256, second.profile_sha256)

    def test_finnish_maltese_rewrite_through_http_adapter_and_durable_host(self):
        # Real adapter, WSGI endpoint, pinned policy and SQLite host ledger;
        # only the model launcher and quality records are synthetic fixtures.
        import test_website_localization_subagent_host as ENDPOINT
        cases = (
            ("fi-FI", "On tärkeää huomata, että teksti on selkeä.", "Teksti on selkeä."),
            ("mt-MT", "Huwa importanti li ngħidu li t-test huwa ċar.", "It-test huwa ċar."),
        )
        routes = []
        for locale, original, revised in cases:
            capture = Host()
            worker, _ = self.worker(revised, capture, locale)
            worker.run(original, "prose", "capture-" + locale)
            for task, _control in capture.calls:
                routes.append(ENDPOINT.HOST.PinnedReviewRoute(
                    route_id="rewrite-" + locale + "-" + task["phase"],
                    schema=task["schema"], phase=task["phase"],
                    target_locale=locale, content_type="prose",
                    task_policy_sha256=ENDPOINT.HOST.task_policy_sha256(task),
                    model_id="fixture-model", model_version="fixture-model-1",
                    host_policy_version="fixture-host-v1",
                    reviewer_agent_id="reviewer:" + task["phase"],
                    reviewer_role=("target-native-reviewer" if task["phase"] == "target_native"
                                   else "source-fidelity-reviewer")))
        launcher = ENDPOINT.FixtureLauncher()
        ledger = ENDPOINT.HOST.SQLiteReviewLedger(
            Path(self.temp.name) / "durable-host.sqlite", lease_seconds=65)
        app = ENDPOINT.HOST.ReviewHostApplication(
            host_id=ENDPOINT.HOST_ID, bearer_token=ENDPOINT.TOKEN,
            signer=ENDPOINT.HOST.HMACAttestationSigner(ENDPOINT.SECRET, ENDPOINT.KEY_ID),
            policy=ENDPOINT.HOST.PinnedReviewPolicy(routes), ledger=ledger,
            launcher=launcher, allow_loopback_http=True)
        for locale, original, revised in cases:
            client = ENDPOINT.HostEndpointTests.client(app)
            worker, creator = self.worker(revised, client, locale)
            result = worker.run(original, "prose", "http-" + locale)
            self.assertEqual(result["target_text"], revised)
            self.assertEqual(len(creator.calls), 1)
            for review in result["evidence"]["reviews"]:
                self.assertEqual(review["host_evidence"]["schema"], ENDPOINT.HTTP.EVIDENCE_SCHEMA)
            restarted, never_called = self.worker(revised, ENDPOINT.HostEndpointTests.client(app), locale)
            replayed = restarted.run(original, "prose", "http-" + locale)
            self.assertEqual(replayed, result)
            self.assertFalse(never_called.calls)
        self.assertEqual(ledger.count(), 4)
        self.assertEqual(len(launcher.calls), 4)
        self.assertEqual([call[0].phase for call in launcher.calls],
                         ["target_native", "source_fidelity"] * 2)
        for index, (_locale, original, _revised) in enumerate(cases):
            native, fidelity = launcher.calls[index * 2:index * 2 + 2]
            self.assertNotIn(original, json.dumps(native[1], ensure_ascii=False))
            self.assertNotIn("source", native[1]["input"])
            self.assertEqual(fidelity[1]["input"]["source"]["text"], original)
            self.assertNotEqual(native[0].reviewer_agent_id, fidelity[0].reviewer_agent_id)
            self.assertNotEqual(native[0].reviewer_session_id, fidelity[0].reviewer_session_id)


if __name__ == "__main__":
    unittest.main()
