"""Synthetic GNU-PO rewrite-planner tests; not native-quality evidence."""
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


PO = load("test_native_rewrite_po_planner",
          ROOT / "integrations" / "native_rewrite_po.py")
WORKER = load("test_native_rewrite_po_worker",
              ROOT / "integrations" / "native_rewrite_worker.py")


class PoPlannerTests(unittest.TestCase):
    def plan(self, source, chunk_chars=2048, max_groups=10):
        return PO.build_plan(source, chunk_chars, max_groups, WORKER._document_plan)

    def test_header_comments_msgids_and_empty_values_are_not_selected(self):
        source = (
            '# fixed\r\nmsgid ""\r\nmsgstr ""\r\n'
            '"Language: fi\\n"\r\n\r\n'
            'msgctxt "button"\r\nmsgid "Open"\r\nmsgstr "Avaa"\r\n\r\n'
            'msgid "Missing"\r\nmsgstr ""\r\n'
        )
        manifest, state = self.plan(source)
        self.assertEqual([item["kind"] for item in manifest["values"]], ["msgstr"])
        owned = [unit["source"] for group in state["groups"] for unit in group["units"]]
        self.assertEqual(owned, ["Avaa"])
        self.assertNotIn("Language", "".join(owned))
        self.assertEqual(PO.assemble(
            source, state, {state["groups"][0]["units"][0]["value_id"]: "Avaa"}),
            source)

    def test_changed_multitoken_value_keeps_directives_tokens_and_line_endings(self):
        source = (
            '#, python-format\r\nmsgid "Hello %s"\r\nmsgstr ""\r\n'
            '"On tärkeää huomata, "\r\n"että teksti on selkeä %s."\r\n'
        )
        manifest, state = self.plan(source)
        unit = state["groups"][0]["units"][0]
        target = PO.assemble(source, state, {
            unit["value_id"]: "Teksti on selkeä %s.",
        })
        self.assertEqual(target.count("\r\n"), source.count("\r\n"))
        self.assertIn('msgid "Hello %s"', target)
        self.assertIn('msgstr "Teksti on selkeä %s."\r\n""\r\n', target)
        values, skeleton = PO.target_value_map(target)
        self.assertEqual(values[manifest["values"][0]["path"]],
                         "Teksti on selkeä %s.")
        self.assertEqual(skeleton, manifest["skeleton_sha256"])

    def test_candidate_quotes_backslashes_and_newlines_are_safely_escaped(self):
        source = 'msgid "Copy"\nmsgstr "Vanha teksti."\n' + '# pad\n' * 50
        _manifest, state = self.plan(source)
        unit = state["groups"][0]["units"][0]
        candidate = 'Uusi "teksti" polussa C:\\tmp.\nToinen rivi.'
        target = PO.assemble(source, state, {unit["value_id"]: candidate})
        self.assertNotIn('msgstr "Uusi "teksti"', target)
        values, _skeleton = PO.target_value_map(target)
        self.assertEqual(next(iter(values.values())), candidate)

    def test_plural_values_have_distinct_collision_safe_paths(self):
        source = (
            'msgid "One"\nmsgid_plural "Many"\n'
            'msgstr[0] "Yksi"\nmsgstr[1] "Monta"\n'
        )
        manifest, _state = self.plan(source)
        paths = [value["path"] for value in manifest["values"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual([value["kind"] for value in manifest["values"]],
                         ["msgstr[0]", "msgstr[1]"])

    def test_unsupported_or_ambiguous_syntax_fails_closed(self):
        cases = (
            ('msgid "Copy"\nmsgstr "bad\\x20escape"\n', "unsupported_escape"),
            ('"orphan"\nmsgid "Copy"\nmsgstr "Value"\n', "orphan_continuation"),
            ('msgid "One"\nmsgid "Two"\nmsgstr "Value"\n', "duplicate_field"),
            ('msgid "Copy"\nmsgstr[01] "Value"\n', "unsupported_syntax"),
            ('msgstr "Value"\nmsgid "Copy"\n', "field_order_invalid"),
            ('msgid "Copy"\nmsgstr[0] "Value"\n', "singular_fields_invalid"),
            ('msgid "One"\nmsgid_plural "Many"\nmsgstr[1] "Many"\n',
             "plural_fields_invalid"),
            ('msgid "Copy"\nmsgstr "Value"\n\nmsgid ""\nmsgstr "Meta"\n',
             "header_position_invalid"),
            ('msgid "Copy"\nmsgstr "Cafe\u0301"\n', "non_nfc"),
            ('msgid "Copy"\nmsgstr ""\n', "no_rewritable_values"),
        )
        for source, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(
                    PO.PoRewritePlanError, "long_po_" + code):
                self.plan(source)

    def test_candidate_normalization_and_missing_values_fail_closed(self):
        source = 'msgid "Copy"\nmsgstr "Café"\n'
        _manifest, state = self.plan(source)
        value_id = state["groups"][0]["units"][0]["value_id"]
        for candidates in ({}, {value_id: "Cafe\u0301"}, {value_id: ""}):
            with self.subTest(candidates=candidates), self.assertRaises(
                    (PO.PoRewritePlanError, KeyError)):
                PO.assemble(source, state, candidates)

    def test_per_value_protected_tokens_cannot_hide_behind_msgids(self):
        source = (
            'msgid "Hello %s"\nmsgstr ""\n'
            '"Pitkä arvo säilyttää %s ja {name}."\n'
        )
        _manifest, state = self.plan(source)
        value_id = state["groups"][0]["units"][0]["value_id"]
        target = PO.assemble(source, state, {
            value_id: "Pitkä arvo ei enää sisällä tunnisteita.",
        })
        self.assertTrue(WORKER._po_value_integrity_errors(source, target))


if __name__ == "__main__":
    unittest.main()
