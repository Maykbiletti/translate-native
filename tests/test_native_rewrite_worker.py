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
        candidate = self.target(request) if callable(self.target) else self.target
        if request.input["response_schema"]["schema"] == RW.LONG_SCHEMA:
            return {"schema": RW.LONG_SCHEMA, "phase": "transcreation",
                    "locale": request.input["target"]["locale"],
                    "chunk_id": request.input["chunk_id"],
                    "completion_status": "complete", "candidate": candidate}
        return {"schema": RW.WORKER.CANDIDATE_SCHEMA, "phase": "transcreation",
                "locale": request.input["target"]["locale"], "candidate": candidate}

    def verified_completion(self, request, response):
        return {
            "schema": RW.SUBAGENTS.CREATOR_COMPLETION_SCHEMA,
            "request_sha256": RW.SUBAGENTS._hash(request.as_payload()),
            "response_sha256": RW.SUBAGENTS._hash(response),
            "finish_reason": "complete",
            "output_tokens": max(1, len(response["candidate"]) // 4),
            "provider_execution_id": "fixture-" + request.request_id[-48:],
        }

    def verify_completion(self, evidence, request, response):
        return evidence == self.verified_completion(request, response)


class Host:
    """Trusted in-memory fixture ledger; no actual model is called."""
    def __init__(self, confidence="high", mutation=None, error=None, review_factory=None):
        self.confidence, self.mutation, self.error = confidence, mutation, error
        self.review_factory = review_factory
        self.calls, self.ledger = [], {}

    def run_isolated(self, task, *, control):
        self.calls.append((task, control))
        if self.error:
            raise self.error
        key = control["execution_key"]
        if key not in self.ledger:
            response = {"schema": RW.REVIEW_SCHEMA, "phase": task["phase"],
                        "locale": task["input"]["target"]["locale"], "status": "PASS",
                        "confidence": self.confidence, "blocking_defects": [], "major_defects": [],
                        "uncertainties": ([] if self.confidence == "high" else [{
                            "class": "fixture_uncertainty", "reason": "Synthetic low confidence.",
                            "evidence_needed": "Independent native fixture evidence."}])}
            if self.confidence == "low":
                response["status"] = "FAIL"
            if self.review_factory is not None:
                response = self.review_factory(task, response)
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
        text = "Tämä on jo hyvä teksti."
        worker, _ = self.worker(text)
        self.assertEqual(worker.run(text, "prose", "unchanged-short")["target_text"], text)

        refrain = ("la la la " * 4000).strip()
        worker, creator = self.worker(
            lambda request: request.input["owned_source"]["text"],
            max_output_tokens=8192)
        result = worker.run(refrain, "prose", "unchanged-long")
        self.assertEqual(result["target_text"], refrain)
        self.assertGreater(len(creator.calls), 1)
        self.assertTrue(worker.validate_document_evidence(
            refrain, result["target_text"], result["evidence"]["document"],
            content_type="prose", request_id="unchanged-long",
            correction_history=[]))

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
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "uncertainty_requires_review"):
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

    def test_concurrent_long_request_cannot_poison_segment_owner_or_restart_it(self):
        entered, release = threading.Event(), threading.Event()

        class WaitingCreator(Creator):
            def invoke(self, request):
                if not self.calls:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError
                return super().invoke(request)

        source = ("Pitkä omistettu osa säilyy kokonaisena 42. " * 180).strip()
        creator = WaitingCreator(lambda request: request.input["owned_source"]["text"])
        worker, _ = self.worker(creator=creator)
        other, second_creator = self.worker(
            creator=Creator(lambda request: request.input["owned_source"]["text"]))
        outcomes = []

        def execute():
            try:
                outcomes.append(worker.run(source, "prose", "long-concurrent"))
            except Exception as error:
                outcomes.append(error)

        thread = threading.Thread(target=execute)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(
                    RW.NativeRewriteBlocked, "long_document_segment_outcome_unknown"):
                other.run(source, "prose", "long-concurrent")
            self.assertFalse(second_creator.calls)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(outcomes[0]["target_text"], source)
        expected = len(outcomes[0]["evidence"]["document"]["chunks"])
        self.assertEqual(len(creator.calls), expected)

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
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, "native_evidence_required"):
                worker.run("Original.", "prose", "evidence-" + str(index))
            self.assertFalse(creator.calls)

    def test_effective_profile_binding_includes_budget_and_host_policy(self):
        first, _ = self.worker()
        second, _ = self.worker(max_output_tokens=2048)
        self.assertNotEqual(first.profile_sha256, second.profile_sha256)

        legacy, _ = self.worker("Lyhyt teksti.", max_output_tokens=128)
        self.assertEqual(legacy.run("Lyhyt teksti.", "prose", "legacy-128")[
            "target_text"], "Lyhyt teksti.")
        legacy_long, creator = self.worker(
            creator=Creator("unused"), max_output_tokens=512)
        with self.assertRaisesRegex(
                RW.NativeRewriteBlocked, "long_document_budget_insufficient"):
            legacy_long.run("Pitkä teksti. " * 500, "prose", "legacy-long")
        self.assertFalse(creator.calls)

    def test_long_document_completion_capacity_structure_and_manifest_fail_closed(self):
        class LengthLimitedCreator(Creator):
            def verified_completion(self, request, response):
                evidence = super().verified_completion(request, response)
                evidence["finish_reason"] = "length"
                return evidence

        class MissingCompletionCreator(Creator):
            verified_completion = None

        source = ("Kappale säilyttää kaikki tiedot ja numeron 42. " * 180).strip()
        worker, creator = self.worker(creator=LengthLimitedCreator(
            lambda request: request.input["owned_source"]["text"]))
        with self.assertRaises(RW.NativeRewriteBlocked):
            worker.run(source, "prose", "length-limited")
        self.assertEqual(len(creator.calls), 1)
        worker, creator = self.worker(creator=MissingCompletionCreator(
            lambda request: request.input["owned_source"]["text"]))
        with self.assertRaisesRegex(
                RW.NativeRewriteBlocked, "creator_completion_unavailable"):
            worker.run(source, "prose", "completion-missing")
        self.assertEqual(len(creator.calls), 1)

        too_large = ("Liian pitkä asiakirja. " * 1000).strip()
        worker, creator = self.worker(creator=Creator("unused"), max_output_tokens=2048)
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "long_document_too_large"):
            worker.run(too_large, "prose", "too-large")
        self.assertFalse(creator.calls)

        structured = json.dumps({"content": "pitkä " * 1000}, ensure_ascii=False)
        worker, creator = self.worker(creator=Creator("unused"))
        with self.assertRaisesRegex(RW.NativeRewriteBlocked,
                                    "long_document_structured_unsupported"):
            worker.run(structured, "prose", "structured-long")
        self.assertFalse(creator.calls)

        clean_worker, _ = self.worker(
            creator=Creator(lambda request: request.input["owned_source"]["text"]),
            max_output_tokens=8192)
        accepted = clean_worker.run(source, "prose", "manifest-valid")
        mutations = (
            lambda document: document["chunks"][0].__setitem__(
                "target_end", document["chunks"][0]["target_end"] + 1),
            lambda document: document["chunks"].reverse(),
            lambda document: document["chunks"][0].__setitem__(
                "creation_request_sha256", "0" * 64),
            lambda document: document["chunks"][0].__setitem__(
                "creation_response_sha256", "0" * 64),
            lambda document: document["chunks"][0].__setitem__(
                "creation_status", "reused"),
            lambda document: document["chunks"][0].__setitem__(
                "completion_status", "length"),
            lambda document: document["chunks"][0]["creator_completion"].__setitem__(
                "finish_reason", "length"),
            lambda document: document["chunks"][0]["creator_completion"].__setitem__(
                "provider_execution_id", "forged-execution"),
        )
        for mutate in mutations:
            document = json.loads(json.dumps(accepted["evidence"]["document"]))
            mutate(document)
            self.assertFalse(clean_worker.validate_document_evidence(
                source, accepted["target_text"], document, content_type="prose",
                request_id="manifest-valid", correction_history=[]))

    def test_no_whitespace_segmentation_uses_explicit_sentence_boundaries(self):
        japanese = ("自然な文章です。家族👨‍👩‍👧‍👦と番号42を保ちます。" * 120)
        manifest, chunks, separators, prefix, suffix = RW._document_plan(
            japanese, 512, RW.LONG_MAX_CHUNKS)
        self.assertEqual(manifest["segmentation_policy"],
                         RW.LONG_SEGMENTATION_POLICY)
        self.assertEqual(prefix + "".join(
            body + separators[index] for index, (_item, body) in enumerate(chunks)
        ) + suffix, japanese)
        for _item, body in chunks[:-1]:
            self.assertIn(body[-1], ".!?。！？｡؟।॥")

    def test_unsegmented_unicode_blocks_instead_of_splitting_grapheme_clusters(self):
        self.assertEqual(sum(last - first + 1 for first, last in
                             RW._GCB_CONTINUATION_RANGES_V17), 2619)
        self.assertEqual(sum(last - first + 1 for first, last in
                             RW._GCB_PREPEND_RANGES_V17), 27)
        self.assertTrue(all(first <= last and (index == 0 or
                            RW._GCB_CONTINUATION_RANGES_V17[index - 1][1] < first)
                            for index, (first, last) in enumerate(
                                RW._GCB_CONTINUATION_RANGES_V17)))
        emoji_tag = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
        for cluster in ("क्ष", "각", emoji_tag):
            source = "字" * 254 + cluster + "字" * 300
            with self.subTest(cluster=cluster):
                with self.assertRaisesRegex(
                        RW.NativeRewriteBlocked,
                        "rewrite.long_document_safe_boundary_unavailable"):
                    RW._document_plan(source, 256, RW.LONG_MAX_CHUNKS)

        extending_punctuation = "字" * 254 + ".\u0301" + "字" * 300
        with self.assertRaisesRegex(
                RW.NativeRewriteBlocked,
                "rewrite.long_document_safe_boundary_unavailable"):
            RW._document_plan(extending_punctuation, 256, RW.LONG_MAX_CHUNKS)

        for extender in (
                "\u0301", "\u200c", "\u200d", "\u0e33", "\uff9e",
                "\ufe0f", "\U0001f3fb", "\U000e0067"):
            extending_whitespace = "字" * 200 + " " + extender + "字" * 99
            with self.subTest(extender=extender):
                with self.assertRaisesRegex(
                        RW.NativeRewriteBlocked,
                        "rewrite.long_document_safe_boundary_unavailable"):
                    RW._document_plan(
                        extending_whitespace, 256, RW.LONG_MAX_CHUNKS)
                leading = " " + extender + ("字。" * 200)
                with self.assertRaisesRegex(
                        RW.NativeRewriteBlocked,
                        "rewrite.long_document_safe_boundary_unavailable"):
                    RW._document_plan(leading, 256, RW.LONG_MAX_CHUNKS)

        trailing_prepend = ("字。" * 200) + "\u0600 "
        with self.assertRaisesRegex(
                RW.NativeRewriteBlocked,
                "rewrite.long_document_safe_boundary_unavailable"):
            RW._document_plan(trailing_prepend, 256, RW.LONG_MAX_CHUNKS)

        internal_prepend = "字" * 200 + "\u0600 " + "字" * 99
        with self.assertRaisesRegex(
                RW.NativeRewriteBlocked,
                "rewrite.long_document_safe_boundary_unavailable"):
            RW._document_plan(internal_prepend, 256, RW.LONG_MAX_CHUNKS)

    def test_long_document_restart_reuses_persisted_chunks_without_second_start(self):
        class SimulatedCrash(BaseException):
            pass

        class CrashAfterFirstSegment(RW.NativeRewriteWorker):
            def _create_long_segment(self, *args, **kwargs):
                result = super()._create_long_segment(*args, **kwargs)
                if not getattr(self, "_crashed", False):
                    self._crashed = True
                    raise SimulatedCrash()
                return result

        source = ("Pitkä synteettinen kappale säilyy kokonaisena. " * 180).strip()
        first_creator = Creator(lambda request: request.input["owned_source"]["text"])
        first = CrashAfterFirstSegment(
            first_creator, Host(), ledger_path=self.path,
            creator_id="writer", creator_session_id="writer-session",
            model_id="fixture-model", model_version="fixture-model-1",
            host_policy_version="fixture-host-v1", profile=profile())
        with self.assertRaises(SimulatedCrash):
            first.run(source, "prose", "long-resume")
        self.assertEqual(len(first_creator.calls), 1)

        second_creator = Creator(lambda request: request.input["owned_source"]["text"])
        resumed, _ = self.worker(creator=second_creator)
        result = resumed.run(source, "prose", "long-resume")
        count = len(result["evidence"]["document"]["chunks"])
        self.assertEqual(len(second_creator.calls), count - 1)
        self.assertEqual(result["target_text"], source)

    def test_long_document_correction_is_global_and_only_recreates_affected_chunk(self):
        failed = False

        def review_factory(task, response):
            nonlocal failed
            if task["phase"] == "target_native" and not failed:
                failed = True
                response.update(status="FAIL", major_defects=[{
                    "severity": "major", "class": "formulaic_opening",
                    "excerpt": "OSA-000", "reason": "Synthetic fixture finding.",
                    "impact": "The opening is repetitive.",
                    "revision_direction": "Make the opening direct.",
                }])
            return response

        paragraphs = [
            f"OSA-{index:03d} Tämä synteettinen kappale sisältää tiedon {index} ja ääkköset."
            for index in range(140)
        ]
        source = "\n\n".join(paragraphs)

        def revise(request):
            if "editorial_feedback" in request.input:
                return request.input["editorial_feedback"]["candidate"].replace(
                    "OSA-000", "OSA-000 korjattu", 1)
            return request.input["owned_source"]["text"]

        host = Host(review_factory=review_factory)
        worker, creator = self.worker(creator=Creator(revise), host=host)
        result = worker.run(source, "prose", "long-correction")
        count = len(result["evidence"]["document"]["chunks"])
        self.assertEqual(len(creator.calls), count + 1)
        self.assertEqual(result["evidence"]["corrections_used"], 1)
        self.assertIn("OSA-000 korjattu", result["target_text"])
        self.assertEqual([task["phase"] for task, _control in host.calls],
                         ["target_native", "target_native", "source_fidelity"])
        self.assertEqual(sum(
            item["creation_status"] == "created"
            for item in result["evidence"]["document"]["chunks"]), 1)
        tampered = json.loads(json.dumps(result["evidence"]["document"]))
        reused = next(item for item in tampered["chunks"]
                      if item["creation_status"] == "reused")
        reused["creation_attempt"] = 1
        self.assertFalse(worker.validate_document_evidence(
            source, result["target_text"], tampered, content_type="prose",
            request_id="long-correction",
            correction_history=result["evidence"]["correction_history"]))

    def test_long_maltese_and_non_latin_fixtures_use_document_reviews(self):
        cases = (
            ("mt-MT", "Dan huwa test sintetiku twil b'ċ, ġ, għ, ħ u ż. "),
            ("ar", "هذا نص اصطناعي طويل يحافظ على الأرقام 42 وعلامات الترقيم. "),
        )
        for index, (locale, seed) in enumerate(cases):
            source = (seed * 100).strip()
            host = Host()
            worker, creator = self.worker(
                creator=Creator(lambda request: request.input["owned_source"]["text"]),
                host=host, locale=locale)
            result = worker.run(source, "prose", "long-script-" + str(index))
            self.assertEqual(result["target_text"], source)
            self.assertGreater(len(creator.calls), 1)
            native, fidelity = (task for task, _control in host.calls)
            self.assertNotIn("source", native["input"])
            self.assertNotIn("review_scope", native["input"])
            self.assertEqual(fidelity["input"]["source"]["text"], source)

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
