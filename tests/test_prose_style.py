"""Synthetic style regressions; no authorship or native-quality benchmark."""
import hashlib
import unittest

from test_language_guard_mcp import MODULE
from test_response_subagent_review import CREATOR, LedgerHost, reviewer
from test_website_localization_worker import WORKER, job


class ProseStyleTests(unittest.TestCase):
    def test_changed_editor_instructions_invalidate_worker_request_identity(self):
        task = job()
        original = WORKER._request(task, "target_native", "old editorial policy", {})
        changed = WORKER._request(task, "target_native", WORKER._TARGET_REVIEW_SYSTEM, {})
        self.assertNotEqual(original.request_id, changed.request_id)
        again = WORKER._request(task, "target_native", WORKER._TARGET_REVIEW_SYSTEM, {})
        self.assertEqual(again.request_id, changed.request_id)

    def report(self, text, locale="de-DE", content_type="prose"):
        return MODULE.validate_text(text, locale, content_type=content_type)

    def test_orthography_pass_does_not_hide_style_signals(self):
        text = " ".join([
            "Darüber hinaus können unsere Gäste die nächsten Schritte gemeinsam besprechen.",
            "In diesem Zusammenhang können unsere Gäste die nächsten Schritte gemeinsam planen.",
        ] * 8)
        result = self.report(text)
        self.assertEqual(result["status"], "PASS")
        style = result["style_review"]
        self.assertEqual(style["status"], "REVIEW_RECOMMENDED")
        codes = {f["code"] for f in style["findings"]}
        self.assertIn("style-repeated-sentence", codes)
        self.assertIn("style-formulaic-transitions", codes)
        self.assertIn("style-uniform-sentence-length", codes)
        for finding in style["findings"]:
            self.assertTrue(finding["requires_context_review"])
            for span in finding["spans"]:
                self.assertTrue(text[span["start"]:span["end"]].endswith("."))
        self.assertEqual(style["authorship"], "NOT_ASSESSED")
        self.assertEqual(style["target_sha256"], hashlib.sha256(text.encode()).hexdigest())

    def test_long_text_and_exact_binding(self):
        text = "Unsere Gäste können heute die nächsten Schritte in Ruhe gemeinsam besprechen. " * 400
        report = self.report(text)["style_review"]
        self.assertGreater(len(text), 29705)
        self.assertEqual(report["metrics"]["sentence_count"], 400)
        self.assertNotEqual(report["target_sha256"], self.report(text + " ")["style_review"]["target_sha256"])
        self.assertLessEqual(len(report["findings"][0]["spans"]), 10)

    def test_varied_prose_is_not_given_an_authorship_or_native_certificate(self):
        # Synthetic varied lengths and unique lexical sentences, not a native benchmark.
        text = " ".join("Unsere Gäste " + " ".join(["planen"] * n) + " morgen."
                        for n in range(4, 20))
        style = self.report(text)["style_review"]
        self.assertEqual(style["status"], "NO_SIGNALS")
        self.assertEqual(style["native_quality"], "REQUIRES_NATIVE_REVIEW")
        self.assertEqual(style["semantic_repetition"], "REQUIRES_NATIVE_REVIEW")
        self.assertEqual(style["authorship"], "NOT_ASSESSED")

    def test_style_diagnostics_never_override_encoding_block(self):
        result = self.report("Unsere Gäste kommen morgen. " * 30 + "\ufffd")
        self.assertEqual(result["status"], "BLOCK")
        self.assertFalse(result["release_allowed"])

    def test_different_quantities_are_not_identical_sentences(self):
        text = " ".join(f"Unsere Gäste können im September {n} verschiedene Angebote für ihre Reise auswählen."
                        for n in range(10, 30))
        codes = {f["code"] for f in self.report(text)["style_review"]["findings"]}
        self.assertNotIn("style-repeated-sentence", codes)

    def test_code_and_blockquotes_not_scored(self):
        sentence = "Darüber hinaus können unsere Gäste die nächsten Schritte gemeinsam besprechen."
        for text in ["```text\n" + (sentence + "\n") * 20 + "```",
                     ("> " + sentence + "\n") * 20,
                     ("- " + sentence + "\n") * 20]:
            self.assertEqual(self.report(text)["style_review"]["status"], "NOT_ASSESSED")

    def test_unsupported_formats_short_text_and_word_segmentation_are_explicit(self):
        for text, locale, kind in [("Guten Morgen!", "de-DE", "prose"),
                                   ("你好。" * 100, "zh-CN", "prose"),
                                   ("<p>Hallo.</p>" * 100, "de-DE", "prose"),
                                   ("Unsere Gäste kommen morgen. " * 100, "de-DE", "ui")]:
            report = self.report(text, locale, kind)["style_review"]
            self.assertEqual(report["status"], "NOT_ASSESSED")
            self.assertEqual(report["native_quality"], "REQUIRES_NATIVE_REVIEW")

    def test_finnish_maltese_and_swedish_not_forced_through_german_phrases(self):
        for locale, sentence in [
            ("fi-FI", "Voit hallita tilaustasi milloin tahansa ja tarkistaa kaikki tiedot omalta tililtäsi."),
            ("mt-MT", "Tista’ timmaniġġja l-abbonament tiegħek fi kwalunkwe ħin u tara d-dettalji kollha."),
            ("sv-SE", "Du kan ändra ditt abonnemang när du vill och läsa alla uppgifter här."),
        ]:
            style = self.report((sentence + " ") * 12, locale)["style_review"]
            self.assertEqual(style["status"], "REVIEW_RECOMMENDED")
            self.assertEqual(style["metrics"]["stock_transition_profile"], "NOT_ASSESSED")
            self.assertIn("style-repeated-sentence", [f["code"] for f in style["findings"]])

    def test_all_language_native_adapter_receives_full_text_without_source(self):
        for locale in ("de-DE", "fi-FI", "mt-MT", "zh-CN", "ar-SA"):
            host = LedgerHost()
            reviewer(host).review("Synthetic fixture", locale, "prose", **CREATOR)
            task, control = host.calls[0]
            self.assertEqual(task["input"]["candidate"], "Synthetic fixture")
            self.assertNotIn("source", task["input"])
            self.assertFalse(control["inherit_context"])
            self.assertIn("repeated theses even when paraphrased", task["system_instruction"])
            self.assertIn("Never infer human or AI authorship", task["system_instruction"])
