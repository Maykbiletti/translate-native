"""Synthetic SRT/WebVTT rewrite-planner tests; not native-quality evidence."""
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SUB = load("test_native_rewrite_subtitle_planner",
           ROOT / "integrations" / "native_rewrite_subtitle.py")


class SubtitlePlannerTests(unittest.TestCase):
    def plan(self, source, chunk_chars=2048, max_groups=10):
        return SUB.build_plan(source, chunk_chars, max_groups)

    def test_srt_timestamps_ids_blank_lines_and_crlf_are_host_owned(self):
        source = (
            "1\r\n00:00:01,000 --> 00:00:03,000\r\n"
            "On tärkeää huomata, että teksti on selkeä.\r\n\r\n"
            "2\r\n00:00:04,000 --> 00:00:06,000\r\nToinen rivi.\r\n"
        )
        manifest, state = self.plan(source)
        self.assertEqual(manifest["format"], "srt")
        candidates = {leaf["unit_ids"][0]: leaf["source"].replace(
            "On tärkeää huomata, että ", "") for leaf in state["leaves"]}
        target = SUB.assemble(source, state, candidates)
        self.assertIn("teksti on selkeä.", target)
        self.assertEqual(target.count("\r\n"), source.count("\r\n"))
        self.assertIn("1\r\n00:00:01,000 --> 00:00:03,000\r\n", target)
        mapped, skeleton = SUB.candidate_map(source, target)
        self.assertEqual(mapped, candidates)
        self.assertEqual(skeleton, manifest["skeleton_sha256"])

    def test_webvtt_metadata_settings_and_protected_tokens_are_exact(self):
        source = (
            "\ufeffWEBVTT fixture\n\nNOTE fixed metadata\nDo not expose this.\n\n"
            "cue-a\n00:01.000 --> 00:03.000 line:90% align:start\n"
            "Huwa importanti <i>ħafna</i> għal {name} fuq https://example.test/x.\n"
        )
        manifest, state = self.plan(source)
        unit = state["groups"][0]["units"][0]
        self.assertNotIn("NOTE fixed", unit["source"])
        self.assertNotIn("<i>", unit["source"])
        self.assertNotIn("{name}", unit["source"])
        self.assertGreaterEqual(len(SUB.MARKER_PATTERN.findall(unit["source"])), 4)
        candidate = unit["source"].replace("Huwa importanti ", "Dan jgħodd ")
        target = SUB.assemble(source, state, {unit["value_id"]: candidate})
        self.assertIn("NOTE fixed metadata", target)
        self.assertIn("00:01.000 --> 00:03.000 line:90% align:start", target)
        self.assertIn("<i>ħafna</i>", target)
        self.assertIn("{name}", target)
        self.assertIn("https://example.test/x", target)
        self.assertEqual(SUB.candidate_map(source, target)[1],
                         manifest["skeleton_sha256"])

    def test_multiline_cue_may_reflow_but_not_change_line_count(self):
        source = (
            "WEBVTT\n\n00:01.000 --> 00:03.000\n"
            "Ensimmäinen rivi\nToinen rivi\n"
        )
        _manifest, state = self.plan(source)
        unit = state["groups"][0]["units"][0]
        target = SUB.assemble(source, state, {
            unit["value_id"]: "Luonteva ensimmäinen\nrivi jatkuu tässä",
        })
        self.assertIn("Luonteva ensimmäinen\nrivi jatkuu tässä", target)
        for candidate in ("Yksi rivi", "Liikaa\nrivejä\nnyt"):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(
                    SUB.SubtitleRewritePlanError, "line|candidate"):
                SUB.assemble(source, state, {unit["value_id"]: candidate})

    def test_missing_reordered_or_changed_markers_fail_closed(self):
        source = (
            "1\n00:00:01,000 --> 00:00:03,000\n"
            "Avaa <i>{name}</i> osoitteessa https://example.test.\n"
        )
        _manifest, state = self.plan(source)
        unit = state["groups"][0]["units"][0]
        markers = SUB.MARKER_PATTERN.findall(unit["source"])
        cases = (
            unit["source"].replace(markers[0], "", 1),
            unit["source"].replace(markers[0], markers[1], 1),
            unit["source"] + markers[0],
        )
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(
                    SUB.SubtitleRewritePlanError, "protected_syntax_changed"):
                SUB.assemble(source, state, {unit["value_id"]: candidate})

    def test_ambiguous_or_unsupported_formats_block(self):
        cases = (
            ("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Text\n",
             "ass_unsupported"),
            ("1\n00:00:01,000 --> 00:00:03,000\nText\n   \n", "whitespace_line"),
            ("cue\n00:00:01,000 --> 00:00:03,000\nText\n", "cue_identifier"),
            ("WEBVTT\n\nSTYLE\n::cue { color: lime; }\n", "no_rewritable"),
            ("1\n00:00:01,000 --> 00:00:03,000\n Text\n", "whitespace"),
            ("1\n00:00:01,000 --> 00:00:03,000\n"
             "__TN_SUB_0000_0123456789abcdef__\n", "marker_collision"),
        )
        for source, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(
                    SUB.SubtitleRewritePlanError, code):
                self.plan(source)

    def test_language_validation_text_excludes_container_and_protected_syntax(self):
        source = (
            "1\n00:00:01,000 --> 00:00:03,000\n"
            "النص واضح <i>{name}</i> https://example.test/fixed 42.\n"
        )
        prose = SUB.language_validation_text(source)
        self.assertIn("النص واضح", prose)
        for protected in ("00:00", "<i>", "{name}", "https://"):
            self.assertNotIn(protected, prose)


if __name__ == "__main__":
    unittest.main()
