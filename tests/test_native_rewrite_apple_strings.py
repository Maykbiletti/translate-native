"""Synthetic Apple .strings planner tests, not native-quality evidence."""
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STRINGS = load("test_native_rewrite_apple_strings_planner",
               ROOT / "integrations" / "native_rewrite_apple_strings.py")
WORKER = load("test_native_rewrite_apple_strings_worker",
              ROOT / "integrations" / "native_rewrite_worker.py")


class AppleStringsPlannerTests(unittest.TestCase):
    def plan(self, source, chunk_chars=1500):
        return STRINGS.build_plan(
            source, chunk_chars, 10, WORKER._document_plan)

    def test_round_trip_changes_only_nonempty_values(self):
        source = ('\ufeff/* keep SECRET */\r\n'
                  '"welcome.key" = "Hei {name}, summa on %1$@ ja 42 €.";\r\n'
                  '// fixed\r\n"empty" = "";\r\n')
        manifest, state = self.plan(source)
        candidates = {unit["value_id"]: unit["source"].replace("Hei", "Terve")
                      for group in state["groups"] for unit in group["units"]}
        target = STRINGS.assemble(source, state, candidates)
        self.assertEqual(
            target,
            source.replace('"Hei {name}, summa on %1$@ ja 42 €."',
                           '"Terve {name}, summa on %1$@ ja 42 €."'))
        self.assertEqual(manifest["selector_profile"], STRINGS.SELECTOR_PROFILE)
        self.assertIn("SECRET", target)
        self.assertIn('"empty" = "";', target)

    def test_decodes_safe_escapes_and_reencodes_changed_value(self):
        source = '"key\\U002Ename" = "Rivi\\nKaksi \\U00E4 %d";\n'
        _manifest, state = self.plan(source)
        units = [unit for group in state["groups"] for unit in group["units"]]
        self.assertEqual("Rivi\nKaksi ä %d", "".join(unit["source"] for unit in units))
        candidates = {unit["value_id"]: unit["source"].replace("Kaksi", "toinen")
                      for unit in units}
        target = STRINGS.assemble(source, state, candidates)
        self.assertIn('"Rivi\\n', target)
        self.assertIn("ä %d", target)
        self.assertIn('"key\\U002Ename"', target)

    def test_comments_keys_and_layout_never_enter_owned_values(self):
        source = ('/* API_SECRET */\n"technical.key"\t=\t"Näkyvä teksti 42.";\n'
                  '// https://fixed.test\n"second" = "Toinen arvo.";\n')
        _manifest, state = self.plan(source)
        owned = repr([unit for group in state["groups"] for unit in group["units"]])
        for protected in ("API_SECRET", "technical.key", "https://fixed.test"):
            self.assertNotIn(protected, owned)

    def test_carriage_return_line_comment_does_not_hide_following_entry(self):
        source = '// fixed\r"key" = "Näkyvä arvo.";\r'
        _manifest, state = self.plan(source)
        units = [unit for group in state["groups"] for unit in group["units"]]
        self.assertEqual("Näkyvä arvo.", "".join(unit["source"] for unit in units))

    def test_ambiguous_or_unsafe_syntax_fails_closed(self):
        cases = (
            ('"a" = "one";\n"a" = "two";\n', "long_apple_strings_duplicate_key"),
            ('"a" = "bad\\q";\n', "long_apple_strings_unsupported_escape"),
            ('"a" = "unterminated;\n', "long_apple_strings_raw_control_character"),
            ('"a" /* comment */ = "value";\n', "long_apple_strings_equals_expected"),
            ('/* open\n"a" = "value";\n', "long_apple_strings_unterminated_comment"),
            ('"a" = "";\n', "long_apple_strings_no_rewritable_values"),
        )
        for source, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(
                    STRINGS.AppleStringsRewritePlanError, code):
                self.plan(source)

    def test_skeleton_and_token_changes_are_detected(self):
        source = '/* fixed */\n"key" = "Hei {name}, %d.";\n'
        _manifest, state = self.plan(source)
        candidates = {unit["value_id"]: unit["source"]
                      for group in state["groups"] for unit in group["units"]}
        target = STRINGS.assemble(source, state, candidates)
        values, skeleton = STRINGS.target_value_map(target)
        self.assertEqual(values["entry[0]/value"], "Hei {name}, %d.")
        self.assertEqual(skeleton, STRINGS.target_value_map(source)[1])
        self.assertTrue(WORKER.integrity_errors(
            source, target.replace("{name}", "name")))
        self.assertTrue(WORKER.integrity_errors(
            source, target.replace('"key"', '"other"')))


if __name__ == "__main__":
    unittest.main()
