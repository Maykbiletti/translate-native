"""Synthetic correction fixtures: protocol tests, not native-quality evidence."""
import json
import copy
import copy
import tempfile
import threading
import unittest
from pathlib import Path

import test_native_rewrite_worker as FIX
import test_native_rewrite_service as API

RW = FIX.RW

OFFICIAL_EU_LOCALES = (
    "bg-BG", "hr-HR", "cs-CZ", "da-DK", "nl-NL", "en-IE", "et-EE",
    "fi-FI", "fr-FR", "de-AT", "el-GR", "hu-HU", "ga-IE", "it-IT",
    "lv-LV", "lt-LT", "mt-MT", "pl-PL", "pt-PT", "ro-RO", "sk-SK",
    "sl-SI", "es-ES", "sv-SE",
)


def defect(candidate, *, confidence="high", blocking=False, excerpt=None):
    dimensions = {
        name: ("FAIL" if name == "rhythm_and_cohesion" else "PASS")
        for name in RW.NATIVE_DIMENSIONS
    }
    if confidence == "low":
        dimensions["idiom_and_word_choice"] = "NOT_ASSESSED"
    return {"status": "FAIL", "confidence": confidence,
            "holistic_assessment": {
                "reads_as_native_original": False,
                "reason": "Synthetic fixture marks the complete candidate as unnatural.",
                "repair_scope": "whole_text",
                "dimensions": dimensions,
            },
            "major_defects": [] if blocking else [{
                "severity": "major", "class": "idiom", "excerpt": excerpt or candidate,
                "reason": "Synthetic editorial finding.",
                "impact": "The introduction delays the point.",
                "revision_direction": "Remove the empty introductory formula."}],
            "blocking_defects": [{"severity": "blocking", "class": "meaning",
                                  "excerpt": candidate, "reason": "Synthetic blocking defect.",
                                  "impact": "The intended meaning cannot be trusted.",
                                  "revision_direction": "Escalate instead of revising automatically."}]
                                 if blocking else [],
            "uncertainties": ([{"class": "fixture_uncertainty",
                                "reason": "Synthetic confidence is low.",
                                "evidence_needed": "Independent native evidence."}]
                              if confidence == "low" else [])}


class Creator(FIX.Creator):
    def __init__(self, first, second, crash=False):
        super().__init__(first)
        self.second, self.crash = second, crash

    def invoke(self, request):
        if "editorial_feedback" in request.input:
            if self.crash:
                self.calls.append(request)
                raise SimulatedCrash()
            self.target = self.second
        return super().invoke(request)


class Host(FIX.Host):
    def __init__(self, bad, *, reject_all=False, change=None):
        super().__init__()
        self.bad, self.reject_all, self.change = bad, reject_all, change

    def run_isolated(self, task, *, control):
        reply = super().run_isolated(task, control=control)
        if task["phase"] == "target_native" and (
                task["input"]["candidate"] == self.bad or self.reject_all):
            reply["response"].update(self.change or defect(task["input"]["candidate"]))
            reply["receipt"]["response_sha256"] = RW.SUBAGENTS._hash(reply["response"])
            self.ledger[control["execution_key"]] = json.loads(json.dumps(reply))
        return reply


class SimulatedCrash(BaseException):
    pass


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def worker(self, creator, host, locale="fi-FI", **options):
        return RW.NativeRewriteWorker(
            creator, host, ledger_path=self.path / "rewrite.sqlite",
            creator_id="writer", creator_session_id="writer-session",
            model_id="fixture-model", model_version="fixture-model-1",
            host_policy_version="fixture-host-v1", profile=FIX.profile(locale), **options)

    def test_one_correction_gets_new_native_and_original_fidelity_review(self):
        for locale, source, bad, good in (
            ("fi-FI", "Teksti on selkeä.", "On tärkeää huomata, että teksti on selkeä.", "Teksti on selkeä."),
            ("mt-MT", "It-test huwa ċar.", "Huwa importanti li ngħidu li t-test huwa ċar.", "It-test huwa ċar."),
            ("ar", "النص واضح.", "من المهم أن نذكر أن النص واضح.", "النص واضح."),
        ):
            creator, host = Creator(bad, good), Host(bad)
            worker = self.worker(creator, host, locale)
            result = worker.run(source, "prose", locale)
            self.assertEqual(result["target_text"], good)
            self.assertEqual(len(creator.calls), 2)
            self.assertEqual(set(creator.calls[1].input["editorial_feedback"]), {"candidate", "review"})
            self.assertEqual([task["phase"] for task, _ in host.calls],
                             ["target_native", "target_native", "source_fidelity"])
            second_native = host.calls[1][0]["input"]
            self.assertNotIn("editorial_feedback", second_native)
            self.assertNotIn("source", second_native)
            self.assertNotIn(bad, json.dumps(second_native, ensure_ascii=False))
            self.assertNotIn("job_id", second_native)
            self.assertNotEqual(host.calls[0][1]["execution_key"], host.calls[1][1]["execution_key"])
            self.assertEqual(host.calls[2][0]["input"]["source"]["text"], source)
            self.assertEqual(result["evidence"]["corrections_used"], 1)
            self.assertEqual(result["evidence"]["correction_history"][0]["target_sha256"],
                             RW._text_hash(bad))
            never_called = Creator("unused", "unused")
            replay = self.worker(never_called, host, locale).run(source, "prose", locale)
            self.assertEqual(replay, result)
            self.assertFalse(never_called.calls)
            self.assertEqual(len(host.ledger), 3)

    def test_spanish_whole_text_translationese_requires_fresh_complete_rewrite(self):
        source = ("Cuando abrí la ventana esta mañana, olía a lluvia. Abajo, frente a la "
                  "casa, mi vecino intentaba meter una maleta demasiado grande en un "
                  "maletero demasiado pequeño. Aunque tenía prisa, bajé a ayudarlo.")
        stiff = ("Cuando abrí la ventana esta mañana, olía a lluvia. Abajo, frente a la "
                 "casa, mi vecino intentaba meter una maleta demasiado grande en un "
                 "maletero demasiado pequeño. La verdad es que tenía prisa. Aun así, "
                 "bajé y lo ayudé.")
        natural = ("Cuando abrí la ventana esta mañana, olía a lluvia. Abajo, frente a la "
                   "casa, mi vecino intentaba encajar una maleta enorme en un maletero "
                   "demasiado pequeño. Aunque tenía prisa, bajé a echarle una mano.")
        creator, host = Creator(stiff, natural), Host(stiff, change={
            **defect(stiff),
            "major_defects": [{
                "severity": "major", "class": "translationese",
                "excerpt": "La verdad es que tenía prisa. Aun así, bajé y lo ayudé.",
                "reason": "The passage is grammatical but reads as a literal sequence.",
                "impact": "The narrative voice and information flow remain stiff.",
                "revision_direction": "Recast the passage from its intent, not by word swaps.",
            }],
        })
        result = self.worker(creator, host, "es-ES").run(
            source, "prose", "spanish-holistic-regression")
        self.assertEqual(result["target_text"], natural)
        self.assertEqual([task["phase"] for task, _ in host.calls],
                         ["target_native", "target_native", "source_fidelity"])
        first_review = host.calls[0][0]["input"]["response_schema"]
        self.assertEqual(first_review["holistic_assessment"]["repair_scope"],
                         "none, local, passage, or whole_text")

    def test_whole_text_verdict_is_required_for_every_eu_locale_and_other_scripts(self):
        # Protocol coverage only: these fixtures make no native-quality claim.
        for locale in (*OFFICIAL_EU_LOCALES, "ar", "hi-IN", "ja-JP"):
            with self.subTest(locale=locale):
                candidate = "Synthetic whole-text review fixture."
                creator, host = Creator(candidate, candidate), Host(candidate)
                worker = self.worker(creator, host, locale, max_corrections=0)
                with self.assertRaisesRegex(
                        RW.NativeRewriteBlocked, "independent_review_required"):
                    worker.run(candidate, "prose", "holistic-" + locale)
                schema = host.calls[0][0]["input"]["response_schema"]
                self.assertIn("holistic_assessment", schema)
                self.assertEqual(schema["locale"], locale)
                self.assertEqual(set(schema["holistic_assessment"]["dimensions"]),
                                 set(RW.NATIVE_DIMENSIONS))

    def test_uncertain_blocking_unanchored_disabled_and_legal_do_not_correct(self):
        bad = "On tärkeää huomata, että teksti on selkeä."
        for i, (change, options, kind) in enumerate((
            (defect(bad, confidence="low"), {}, "prose"),
            (defect(bad, blocking=True), {}, "prose"),
            (defect(bad, excerpt="not in candidate"), {}, "prose"),
            (defect(bad), {"max_corrections": 0}, "prose"),
            (defect(bad), {}, "legal"),
        )):
            creator, host = Creator(bad, "Teksti on selkeä."), Host(bad, change=change)
            worker = self.worker(creator, host, **options)
            reason = ("uncertainty_requires_review" if i == 0 else
                      "review_invalid" if i == 2 else "independent_review_required")
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, reason):
                worker.run("Teksti on selkeä.", kind, "blocked-" + str(i))
            self.assertEqual(len(creator.calls), 1)

    def test_structured_findings_and_uncertainties_are_strictly_validated(self):
        candidate = "On tärkeää huomata, että teksti on selkeä."
        base = defect(candidate)
        cases = []
        missing_impact = copy.deepcopy(base)
        missing_impact["major_defects"][0].pop("impact")
        cases.append(missing_impact)
        wrong_severity = copy.deepcopy(base)
        wrong_severity["major_defects"][0]["severity"] = "blocking"
        cases.append(wrong_severity)
        low_without_uncertainty = copy.deepcopy(base)
        low_without_uncertainty.update(confidence="low", uncertainties=[])
        cases.append(low_without_uncertainty)
        pass_with_defect = copy.deepcopy(base)
        pass_with_defect["status"] = "PASS"
        cases.append(pass_with_defect)
        malformed_uncertainty = defect(candidate, confidence="low")
        malformed_uncertainty["uncertainties"][0].pop("evidence_needed")
        cases.append(malformed_uncertainty)
        pass_with_negative_holistic = {**defect(candidate), "status": "PASS",
                                      "major_defects": []}
        cases.append(pass_with_negative_holistic)
        negative_without_defect = {**defect(candidate), "major_defects": []}
        cases.append(negative_without_defect)
        local_negative = copy.deepcopy(defect(candidate))
        local_negative["holistic_assessment"]["repair_scope"] = "local"
        cases.append(local_negative)
        missing_dimension = copy.deepcopy(defect(candidate))
        missing_dimension["holistic_assessment"]["dimensions"].pop(
            "rhythm_and_cohesion")
        cases.append(missing_dimension)
        invalid_dimension = copy.deepcopy(defect(candidate))
        invalid_dimension["holistic_assessment"]["dimensions"][
            "rhythm_and_cohesion"] = "NO_SIGNALS"
        cases.append(invalid_dimension)
        aggregate_conflict = copy.deepcopy(defect(candidate))
        aggregate_conflict["holistic_assessment"]["reads_as_native_original"] = True
        cases.append(aggregate_conflict)
        not_assessed_without_uncertainty = copy.deepcopy(defect(candidate))
        not_assessed_without_uncertainty["holistic_assessment"]["dimensions"][
            "rhythm_and_cohesion"] = "NOT_ASSESSED"
        cases.append(not_assessed_without_uncertainty)
        for index, change in enumerate(cases):
            creator, host = Creator(candidate, "Teksti on selkeä."), Host(candidate, change=change)
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, "review_invalid"):
                self.worker(creator, host).run("Teksti on selkeä.", "prose", "schema-" + str(index))
            self.assertEqual(len(creator.calls), 1)

    def test_high_confidence_uncertainty_escalates_without_correction(self):
        candidate = "On tärkeää huomata, että teksti on selkeä."
        change = defect(candidate)
        change["uncertainties"] = [{
            "class": "dialect_evidence",
            "reason": "Synthetic regional evidence is insufficient.",
            "evidence_needed": "Independent qualified native review.",
        }]
        change["holistic_assessment"]["dimensions"][
            "idiom_and_word_choice"] = "NOT_ASSESSED"
        creator = Creator(candidate, "Teksti on selkeä.")
        host = Host(candidate, change=change)
        with self.assertRaisesRegex(
                RW.NativeRewriteBlocked, "uncertainty_requires_review"):
            self.worker(creator, host).run(
                "Teksti on selkeä.", "prose", "high-uncertainty")
        self.assertEqual(len(creator.calls), 1)

    def test_not_assessed_dimension_routes_to_independent_evidence(self):
        candidate = "Synthetic review fixture."
        change = {
            "status": "FAIL", "confidence": "low",
            "blocking_defects": [], "major_defects": [],
            "uncertainties": [{
                "class": "locale_evidence",
                "reason": "The fixture has no qualified evidence for this locale.",
                "evidence_needed": "Independent qualified native review.",
            }],
            "holistic_assessment": {
                "reads_as_native_original": False,
                "reason": "One required dimension is not reliably assessable.",
                "repair_scope": "none",
                "dimensions": {
                    name: ("NOT_ASSESSED" if name == "idiom_and_word_choice"
                           else "PASS")
                    for name in RW.NATIVE_DIMENSIONS
                },
            },
        }
        creator, host = Creator(candidate, candidate), Host(candidate, change=change)
        with self.assertRaisesRegex(
                RW.NativeRewriteBlocked, "uncertainty_requires_review"):
            self.worker(creator, host, "mt-MT").run(
                candidate, "prose", "dimension-not-assessed")
        self.assertEqual(len(creator.calls), 1)

    def test_second_failure_unchanged_and_invalid_syntax_never_loop_or_release(self):
        bad = "On tärkeää huomata, että teksti on selkeä {{name}}."
        for i, (second, reject_all, reason) in enumerate((
            ("Teksti on selkeä {{name}}.", True, "independent_review_required"),
            (bad, False, "correction_unchanged"),
            ("Teksti on selkeä {{other}}.", False, "integrity_failed"),
        )):
            creator, host = Creator(bad, second), Host(bad, reject_all=reject_all)
            worker = self.worker(creator, host)
            for _ in range(2):
                with self.assertRaisesRegex(RW.NativeRewriteBlocked, reason):
                    worker.run("Teksti on selkeä {{name}}.", "prose", "limit-" + str(i))
            self.assertEqual(len(creator.calls), 2)
            self.assertLessEqual(len(host.ledger), 2)

    def test_ambiguous_correction_never_restarts_creator(self):
        creator, host = Creator("Intro.", "Good.", crash=True), Host("Intro.")
        worker = self.worker(creator, host)
        with self.assertRaises(SimulatedCrash):
            worker.run("Original.", "prose", "crash")
        restored = self.worker(creator, host)
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "correction_outcome_unknown"):
            restored.run("Original.", "prose", "crash")
        self.assertEqual(len(creator.calls), 2)

    def test_persisted_correction_resumes_reviews_without_creation(self):
        class CrashingHost(Host):
            def run_isolated(self, task, *, control):
                if task["input"]["candidate"] == "Good." and not self.ledger.get("crashed"):
                    self.ledger["crashed"] = True
                    raise SimulatedCrash()
                return super().run_isolated(task, control=control)
        creator, host = Creator("Intro.", "Good."), CrashingHost("Intro.")
        worker = self.worker(creator, host)
        with self.assertRaises(SimulatedCrash):
            worker.run("Original.", "prose", "resume")
        never_called = Creator("unused", "unused")
        result = self.worker(never_called, host).run("Original.", "prose", "resume")
        self.assertEqual(result["target_text"], "Good.")
        self.assertFalse(never_called.calls)
        self.assertEqual(len(creator.calls), 2)

    def test_budget_is_strict_and_changes_release_policy(self):
        creator, host = Creator("Intro.", "Good."), Host("Intro.")
        for invalid in (-1, 2, True, 0.0, "1"):
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, "correction_budget_invalid"):
                self.worker(creator, host, max_corrections=invalid)
        self.assertNotEqual(self.worker(creator, host, max_corrections=0).profile_sha256,
                            self.worker(creator, host, max_corrections=1).profile_sha256)

    def test_concurrent_correction_cannot_duplicate_or_poison_owner(self):
        entered, release = threading.Event(), threading.Event()
        class WaitingCreator(Creator):
            def invoke(self, request):
                if "editorial_feedback" in request.input:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError
                return super().invoke(request)
        creator, host = WaitingCreator("Intro.", "Good."), Host("Intro.")
        worker = self.worker(creator, host)
        outcomes = []
        def run():
            try:
                outcomes.append(worker.run("Original.", "prose", "concurrent"))
            except Exception as error:
                outcomes.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            never_called = Creator("unused", "unused")
            other = self.worker(never_called, host)
            with self.assertRaisesRegex(RW.NativeRewriteBlocked, "correction_outcome_unknown"):
                other.run("Original.", "prose", "concurrent")
            self.assertFalse(never_called.calls)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(other.run("Original.", "prose", "concurrent"), outcomes[0])
        self.assertEqual(len(creator.calls), 2)

    def test_corrected_fidelity_failure_never_requests_third_draft(self):
        class FidelityHost(Host):
            def run_isolated(self, task, *, control):
                reply = super().run_isolated(task, control=control)
                if task["phase"] == "source_fidelity":
                    finding = defect(task["input"]["candidate"])
                    finding.pop("holistic_assessment")
                    reply["response"].update(finding)
                    reply["receipt"]["response_sha256"] = RW.SUBAGENTS._hash(reply["response"])
                    self.ledger[control["execution_key"]] = reply
                return reply
        creator, host = Creator("Intro.", "Good."), FidelityHost("Intro.")
        with self.assertRaisesRegex(RW.NativeRewriteBlocked, "independent_review_required"):
            self.worker(creator, host).run("Original.", "prose", "fidelity")
        self.assertEqual(len(creator.calls), 2)
        self.assertEqual(len(host.calls), 3)

    def test_correction_through_real_http_host_ledger_for_finnish_and_maltese(self):
        import test_website_localization_subagent_host as ENDPOINT
        cases = (("fi-FI", "On tärkeää huomata, että teksti on selkeä.", "Teksti on selkeä."),
                 ("mt-MT", "Huwa importanti li ngħidu li t-test huwa ċar.", "It-test huwa ċar."))
        routes = []
        for locale, bad, good in cases:
            capture = Host(bad)
            self.worker(Creator(bad, good), capture, locale).run(good, "prose", "capture-" + locale)
            for task, _control in (capture.calls[0], capture.calls[2]):
                routes.append(ENDPOINT.HOST.PinnedReviewRoute(
                    route_id="correction-" + locale + "-" + task["phase"],
                    schema=task["schema"], phase=task["phase"], target_locale=locale,
                    content_type="prose", task_policy_sha256=ENDPOINT.HOST.task_policy_sha256(task),
                    model_id="fixture-model", model_version="fixture-model-1",
                    host_policy_version="fixture-host-v1", reviewer_agent_id="reviewer:" + task["phase"],
                    reviewer_role=("target-native-reviewer" if task["phase"] == "target_native"
                                   else "source-fidelity-reviewer")))
        class Launcher(ENDPOINT.FixtureLauncher):
            def _execution(self, assignment, task):
                result = super()._execution(assignment, task)
                if task["phase"] == "target_native" and task["input"]["candidate"] in {c[1] for c in cases}:
                    result["response"].update(defect(task["input"]["candidate"]))
                return result
        launcher = Launcher()
        ledger = ENDPOINT.HOST.SQLiteReviewLedger(self.path / "host.sqlite", lease_seconds=65)
        app = ENDPOINT.HOST.ReviewHostApplication(
            host_id=ENDPOINT.HOST_ID, bearer_token=ENDPOINT.TOKEN,
            signer=ENDPOINT.HOST.HMACAttestationSigner(ENDPOINT.SECRET, ENDPOINT.KEY_ID),
            policy=ENDPOINT.HOST.PinnedReviewPolicy(routes), ledger=ledger,
            launcher=launcher, allow_loopback_http=True)
        for locale, bad, good in cases:
            creator = Creator(bad, good)
            worker = self.worker(creator, ENDPOINT.HostEndpointTests.client(app), locale)
            result = worker.run(good, "prose", "http-" + locale)
            self.assertEqual(result["target_text"], good)
            never_called = Creator("unused", "unused")
            restarted = self.worker(never_called, ENDPOINT.HostEndpointTests.client(app), locale)
            self.assertEqual(restarted.run(good, "prose", "http-" + locale), result)
            self.assertFalse(never_called.calls)
            self.assertEqual(len(creator.calls), 2)
        self.assertEqual(ledger.count(), 6)
        self.assertEqual(len(launcher.calls), 6)
        for index, (_locale, bad, good) in enumerate(cases):
            first, corrected, fidelity = launcher.calls[index * 3:index * 3 + 3]
            self.assertNotEqual(first[0].reviewer_session_id, corrected[0].reviewer_session_id)
            self.assertNotIn("editorial_feedback", corrected[1]["input"])
            self.assertNotIn(bad, json.dumps(corrected[1], ensure_ascii=False))
            self.assertEqual(fidelity[1]["input"]["source"]["text"], good)

    def test_real_host_rejects_incomplete_actionable_review_before_attestation(self):
        import test_website_localization_subagent_host as ENDPOINT
        candidate = "On tärkeää huomata, että teksti on selkeä."
        capture = Host(candidate, change=defect(candidate))
        worker = self.worker(Creator(candidate, "Teksti on selkeä."), capture)
        worker.run("Teksti on selkeä.", "prose", "host-schema-capture")
        task, _control = capture.calls[0]
        route = ENDPOINT.HOST.PinnedReviewRoute(
            route_id="rewrite-structured-contract", schema=task["schema"],
            phase=task["phase"], target_locale="fi-FI", content_type="prose",
            task_policy_sha256=ENDPOINT.HOST.task_policy_sha256(task),
            model_id="fixture-model", model_version="fixture-model-1",
            host_policy_version="fixture-host-v1", reviewer_agent_id="reviewer:target_native",
            reviewer_role="target-native-reviewer")
        valid = defect(candidate)
        valid.update(schema=RW.REVIEW_SCHEMA, phase="target_native", locale="fi-FI")
        self.assertEqual(
            ENDPOINT.HOST.ReviewHostApplication._validate_review_response(valid, route, task),
            valid)
        invalid = copy.deepcopy(valid)
        invalid["major_defects"][0].pop("revision_direction")
        with self.assertRaises(ENDPOINT.HOST.ReviewHostBlocked):
            ENDPOINT.HOST.ReviewHostApplication._validate_review_response(invalid, route, task)
        contradictory = copy.deepcopy(valid)
        contradictory["holistic_assessment"]["dimensions"][
            "rhythm_and_cohesion"] = "PASS"
        contradictory["holistic_assessment"]["reads_as_native_original"] = False
        with self.assertRaises(ENDPOINT.HOST.ReviewHostBlocked):
            ENDPOINT.HOST.ReviewHostApplication._validate_review_response(
                contradictory, route, task)
        missing = copy.deepcopy(valid)
        missing["holistic_assessment"]["dimensions"].pop(
            "voice_genre_and_intentional_repetition")
        with self.assertRaises(ENDPOINT.HOST.ReviewHostBlocked):
            ENDPOINT.HOST.ReviewHostApplication._validate_review_response(
                missing, route, task)

    def test_guard_signs_only_final_revision_and_delivery_rejects_old_candidate(self):
        source, bad, good = "Teksti on selkeä.", "On tärkeää huomata, että teksti on selkeä.", "Teksti on selkeä."
        creator, host = Creator(bad, good), Host(bad)
        worker = self.worker(creator, host)
        service = API.SERVICE.GuardService(self.path / "key", self.path / "audit.jsonl",
                                         rewrite_workers={"standard": worker})
        client = API.ADAPTER.NativeRewriteClient(service.handle)
        client.register_session(session_id="session", session_epoch="a" * 64)
        result = client.rewrite(source_text=source, language="fi-FI", profile_id="standard",
                                request_id="deliver", content_type="prose",
                                session_id="session", session_epoch="a" * 64,
                                agent_id="writer")
        sent = []
        args = dict(source_text=source, language="fi-FI", profile_id="standard", request_id="deliver",
                    content_type="prose",
                    session_id="session", session_epoch="a" * 64, agent_id="writer", channel="test", send=sent.append)
        with self.assertRaises(API.ADAPTER.RewriteDeliveryBlocked):
            client.deliver({**result, "target_text": bad}, **args)
        self.assertEqual(sent, [])
        client.deliver(result, **args)
        self.assertEqual(sent, [good])
        self.assertNotIn(bad, (self.path / "audit.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
