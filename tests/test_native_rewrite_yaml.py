"""Synthetic YAML planner tests, not native-quality evidence."""
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


YAML = load("test_native_rewrite_yaml_planner",
            ROOT / "integrations" / "native_rewrite_yaml.py")
WORKER = load("test_native_rewrite_yaml_worker",
              ROOT / "integrations" / "native_rewrite_worker.py")


class YamlPlannerTests(unittest.TestCase):
    def plan(self, source, chunk_chars=1500):
        return YAML.build_plan(source, chunk_chars, 10, WORKER._document_plan)

    def test_round_trip_changes_only_string_values(self):
        source = ('\ufeff# SECRET stays hidden\r\nhero:\r\n'
                  '  title: "Muodollinen otsikko"\r\n'
                  "  body: 'Hei {name}, hinta on 42 €.' # keep\r\n"
                  'cta: Aloita nyt\r\n')
        manifest, state = self.plan(source)
        candidates = {unit["value_id"]: unit["source"].replace(
            "Muodollinen", "Luonteva").replace("Aloita nyt", "Kokeile nyt")
            for group in state["groups"] for unit in group["units"]}
        target = YAML.assemble(source, state, candidates)
        self.assertIn('title: "Luonteva otsikko"\r\n', target)
        self.assertIn('cta: Kokeile nyt\r\n', target)
        self.assertIn("# SECRET stays hidden", target)
        self.assertIn("# keep", target)
        self.assertEqual(manifest["selector_profile"], YAML.SELECTOR_PROFILE)

    def test_projection_and_owned_units_hide_container_metadata(self):
        source = ('# API_SECRET\ninternal.key:\n'
                  '  title: "Näkyvä teksti 42."\n'
                  "  body: 'Toinen arvo {name}.' # https://fixed.test\n")
        _manifest, state = self.plan(source)
        owned_text = repr([unit["source"] for group in state["groups"]
                           for unit in group["units"]])
        projection = YAML.native_review_text(source)
        self.assertEqual(projection, "Näkyvä teksti 42.\n\nToinen arvo {name}.")
        for protected in ("API_SECRET", "internal.key", "fixed.test", "title", "body"):
            self.assertNotIn(protected, owned_text)
            self.assertNotIn(protected, projection)

    def test_quote_styles_decode_and_reencode_losslessly(self):
        source = 'double: "Rivi\\nKaksi ä"\nsingle: \'It\'\'s clear\'\nplain: Natural text\n'
        _manifest, state = self.plan(source)
        units = [unit for group in state["groups"] for unit in group["units"]]
        values = {unit["value_id"]: unit["source"].replace("clear", "natural")
                  for unit in units}
        target = YAML.assemble(source, state, values)
        self.assertIn('double: "Rivi\\nKaksi ä"', target)
        self.assertIn("single: 'It''s natural'", target)
        self.assertIn("plain: Natural text", target)

    def test_ambiguous_features_and_types_fail_closed(self):
        cases = (
            ("items:\n  - one\n  - two\n", "long_yaml_feature_unsupported"),
            ("a: &base text\nb: *base\n", "long_yaml_plain_scalar_unsafe"),
            ("a: |\n  block text\nb: other\n", "long_yaml_plain_scalar_unsafe"),
            ("a: yes\nb: text\n", "long_yaml_plain_scalar_unsafe"),
            ("a: text\na: other\n", "long_yaml_duplicate_path"),
            ("a:\n   b: text\nc: other\n", "long_yaml_indentation_invalid"),
        )
        for source, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(
                    YAML.YamlRewritePlanError, code):
                self.plan(source)

    def test_candidate_cannot_change_type_or_container(self):
        source = ('screen:\n  title: "Hei {name}."\n  cta: Aloita nyt\n'
                  'enabled: true\ncount: 42\n')
        _manifest, state = self.plan(source)
        candidates = {unit["value_id"]: unit["source"]
                      for group in state["groups"] for unit in group["units"]}
        plain = next(leaf for leaf in state["manifest_leaves"] if leaf["style"] == "plain")
        candidates[plain["unit_ids"][0]] = "true"
        with self.assertRaisesRegex(YAML.YamlRewritePlanError,
                                    "long_yaml_plain_scalar_unsafe"):
            YAML.assemble(source, state, candidates)
        unchanged = {unit["value_id"]: unit["source"]
                     for group in state["groups"] for unit in group["units"]}
        target = YAML.assemble(source, state, unchanged)
        self.assertIn("enabled: true\ncount: 42\n", target)
        self.assertTrue(WORKER.integrity_errors(
            source, source.replace("title:", "heading:")))
        self.assertTrue(WORKER.integrity_errors(
            source, source.replace("{name}", "name")))

    def test_intent_requires_resource_shape(self):
        self.assertTrue(YAML.looks_like_yaml("title: Text\nbody: More text\n"))
        self.assertFalse(YAML.looks_like_yaml("A sentence: with punctuation."))
        self.assertFalse(YAML.looks_like_yaml("[Refrain]\nSing it again."))


if __name__ == "__main__":
    unittest.main()
