"""Synthetic safety tests for lossless long-Markdown rewrite planning."""
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MARKDOWN = load("test_native_rewrite_markdown_planner",
                ROOT / "integrations" / "native_rewrite_markdown.py")
WORKER = load("test_native_rewrite_markdown_worker",
              ROOT / "integrations" / "native_rewrite_worker.py")


class MarkdownPlanTests(unittest.TestCase):
    def plan(self, source, chunk=2048):
        return MARKDOWN.build_plan(source, chunk, 10, WORKER._document_plan)

    @staticmethod
    def unchanged(state):
        return {unit["value_id"]: unit["source"]
                for group in state["groups"] for unit in group["units"]}

    def test_exposes_only_rendered_prose(self):
        source = (
            "---\ntitle: fixed deployment metadata\n---\n"
            "# Selkeä otsikko\n\n"
            "Tämä teksti säilyttää `rm -rf /`, {{name}} ja "
            "[ohjeen](https://example.test/path \"fixed title\").\n\n"
            "> Tämä lainaus säilyy täsmälleen.\n"
            "> Myös toinen lainausrivi.\n\n"
            "```python\nSECRET = 'do-not-send'\n```\n\n"
            "- Ensimmäinen kohta 42.\n"
        )
        manifest, state = self.plan(source)
        owned = "\n".join(
            unit["source"] for group in state["groups"] for unit in group["units"])
        self.assertIn("Selkeä otsikko", owned)
        self.assertIn("Tämä teksti säilyttää", owned)
        self.assertIn("Ensimmäinen kohta 42.", owned)
        self.assertNotIn("rm -rf", owned)
        self.assertNotIn("{{name}}", owned)
        self.assertNotIn("https://", owned)
        self.assertNotIn("lainaus", owned)
        self.assertNotIn("SECRET", owned)
        self.assertNotIn("fixed deployment", owned)
        self.assertEqual(MARKDOWN.assemble(source, state, self.unchanged(state)), source)
        self.assertEqual(MARKDOWN.target_value_map(source)[1],
                         manifest["skeleton_sha256"])

    def test_rewrites_prose_without_reserializing_markdown(self):
        source = "# On tärkeää huomata tämä\n\n- Avaa nyt\n\n```sh\necho fixed\n```\n"
        _manifest, state = self.plan(source)
        candidates = self.unchanged(state)
        for group in state["groups"]:
            for unit in group["units"]:
                if unit["source"] == "On tärkeää huomata tämä":
                    candidates[unit["value_id"]] = "Huomaa tämä"
                if unit["source"] == "Avaa nyt":
                    candidates[unit["value_id"]] = "Avaa"
        self.assertEqual(
            MARKDOWN.assemble(source, state, candidates),
            "# Huomaa tämä\n\n- Avaa\n\n```sh\necho fixed\n```\n",
        )

    def test_native_review_projection_contains_only_ordered_rendered_prose(self):
        source = (
            "---\ntitle: SECRET metadata\n---\n"
            "# Selkeä otsikko\n\n"
            "Lue `fixed_code()` ja [ohje](https://example.test/SECRET).\n\n"
            "> SECRET lainaus\n\n"
            "```sh\necho SECRET\n```\n\n"
            "- Avaa nyt 42.\n"
        )
        projection = MARKDOWN.native_review_text(source)
        self.assertEqual(
            projection,
            "Selkeä otsikko\n\nAvaa nyt 42.",
        )
        self.assertEqual(MARKDOWN.language_validation_text(source), projection)
        for forbidden in (
                "title:", "SECRET", "#", "- ", "`", "[", "]",
                "https://", "fixed_code", "lainaus", "echo"):
            self.assertNotIn(forbidden, projection)

    def test_fence_lengths_and_fence_like_code_are_opaque(self):
        for source in (
            "# Otsikko\n\n````python\n``` is code\n````\n\nTeksti.\n",
            "# Otsikko\n\n~~~json\n{\"fixed\": true}\n~~~\n\nTeksti.\n",
        ):
            with self.subTest(source=source):
                _manifest, state = self.plan(source)
                owned = " ".join(
                    unit["source"] for group in state["groups"]
                    for unit in group["units"])
                self.assertNotIn("is code", owned)
                self.assertNotIn("fixed", owned)
                self.assertEqual(
                    MARKDOWN.assemble(source, state, self.unchanged(state)), source)

    def test_candidate_cannot_inject_markdown_or_line_breaks(self):
        source = "# Otsikko\n\nSelkeä teksti.\n"
        for candidate in (
            "# Uusi otsikko", "- uusi lista", "1. uusi lista", "`code`", "[x](y)",
            "<script>x</script>", "a &amp; b", "kaksi\nriviä", "{directive}",
        ):
            with self.subTest(candidate=candidate):
                _manifest, state = self.plan(source)
                values = self.unchanged(state)
                values[list(values)[-1]] = candidate
                with self.assertRaises(MARKDOWN.MarkdownRewritePlanError):
                    MARKDOWN.assemble(source, state, values)

        list_source = "# Otsikko\n\n- Nykyinen kohta\n"
        for candidate in ("- sisälista", "+ sisälista", "1. sisälista", "> lainaus"):
            with self.subTest(list_candidate=candidate):
                _manifest, state = self.plan(list_source)
                values = self.unchanged(state)
                values[list(values)[-1]] = candidate
                with self.assertRaises(MARKDOWN.MarkdownRewritePlanError):
                    MARKDOWN.assemble(list_source, state, values)

    def test_ambiguous_or_unsupported_markdown_blocks(self):
        cases = (
            "# Otsikko\n\n```python\nunclosed\n",
            "# Otsikko\n\n<div>raw html</div>\n",
            "# Otsikko\n\n| a | b |\n|---|---|\n",
            "# Otsikko\n\n:::note\ntext\n:::\n",
            "# Otsikko\n\nUse [nested [label]](https://example.test).\n",
            "# Otsikko\n\nUse [broken](https://example.test/a(b)).\n",
            "# Otsikko\n\nUse `unclosed code.\n",
            "# Otsikko\n\nDangling escape \\",
            "---\ntitle: unclosed front matter\n# not a close\n",
            "--- \ntitle: SECRET\n---\n# Otsikko\n",
            "--- # frontmatter\ntitle: SECRET\n---\n# Otsikko\n",
            "\ufeff--- # frontmatter\ntitle: SECRET\n---\n# Otsikko\n",
            "# Otsikko\n\nText with &unknown; entity.\n",
            "# Otsikko\n\nUse [la\\]bel](secret).\n",
            "# Otsikko\n\nUse [label][ref\\]x].\n",
            "# Otsikko\n\nHello {% if user %}SECRET{% endif %}.\n",
            "# Otsikko\n\nHello {# SECRET COMMENT #}.\n",
            "# Otsikko\n\nimport Widget from './Widget'\n",
            "# Otsikko\n\n!!! note \"SECRET TITLE\"\n    Body.\n",
            "# Otsikko\n\n??? info \"SECRET TITLE\"\n    Body.\n",
            "Read [multi\nline](secret) now.\n",
            "See ![multi\nline](image.png) now.\n",
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(MARKDOWN.MarkdownRewritePlanError):
                    self.plan(source)

    def test_reference_continuations_and_unicode_emails_remain_opaque(self):
        source = (
            "# Otsikko\n\n"
            "[id]: /target\n  \"SECRET TITLE\"\n\n"
            "[other]: /other\n\"OTHER SECRET TITLE\"\n\n"
            "Kirjoita osoitteisiin δοκιμή@παράδειγμα.ελ ja "
            "परीक्षण@उदाहरण.भारत. Lue [id].\n"
        )
        _manifest, state = self.plan(source)
        owned = " ".join(unit["source"] for group in state["groups"]
                         for unit in group["units"])
        self.assertNotIn("SECRET TITLE", owned)
        self.assertNotIn("OTHER SECRET TITLE", owned)
        for protected_part in ("δοκιμή", "παράδειγμα", "परीक्षण", "उदाहरण", "भारत"):
            self.assertNotIn(protected_part, owned)
        self.assertEqual(MARKDOWN.assemble(
            source, state, self.unchanged(state)), source)

    def test_bom_front_matter_and_tab_indented_code_are_host_owned(self):
        source = (
            "\ufeff---\ntitle: SECRET\n---\n# Otsikko\n\n"
            " \tSECRET_ONE=42\n\n"
            "   \tSECRET_TWO=84\n\nMuokattava teksti.\n"
        )
        _manifest, state = self.plan(source)
        owned = " ".join(unit["source"] for group in state["groups"]
                         for unit in group["units"])
        self.assertNotIn("title: SECRET", owned)
        self.assertNotIn("SECRET_ONE", owned)
        self.assertNotIn("SECRET_TWO", owned)
        self.assertIn("Muokattava teksti.", owned)
        self.assertEqual(MARKDOWN.assemble(
            source, state, self.unchanged(state)), source)

    def test_emphasis_capable_lines_are_host_owned(self):
        source = "# Otsikko\n\na_b_c\n\nMuokattava teksti.\n"
        _manifest, state = self.plan(source)
        owned = " ".join(unit["source"] for group in state["groups"]
                         for unit in group["units"])
        self.assertNotIn("a_b_c", owned)
        self.assertIn("Muokattava teksti.", owned)
        self.assertEqual(MARKDOWN.assemble(
            source, state, self.unchanged(state)), source)

    def test_unicode_scripts_and_nfc_are_supported(self):
        source = (
            "# Malti u Suomi\n\n"
            "Il-ħażna żżomm iċ-ċavetta 42, u käyttäjä näkee selkeän tekstin.\n\n"
            "- العربية واليونانية Ελληνικά säilyvät.\n"
        )
        _manifest, state = self.plan(source)
        self.assertEqual(MARKDOWN.assemble(source, state, self.unchanged(state)), source)
        with self.assertRaises(MARKDOWN.MarkdownRewritePlanError):
            self.plan("# Cafe\u0301\n\nTeksti.\n")

    def test_intent_does_not_misclassify_refrain_but_catches_unsafe_markdown(self):
        self.assertFalse(MARKDOWN.looks_like_markdown("[Refrain]\nSing it again."))
        self.assertFalse(MARKDOWN.looks_like_markdown("Hello {name}."))
        for source in (
            "# Heading\n\nText.", "> quote", "- item", "Use *care*.",
            "Use `code`.", "Use [link](https://example.test).", "```\ncode",
            ":::note\nText.", "Heading\n===", "    indented code",
            "| a | b |\n|---|---|",
            "Literal \\*asterisk.", "First line  \nSecond line",
            "First line\\\nSecond line", "*first line\nsecond line*",
            " \tSECRET=42", "   \tSECRET=42", "\ufeff---\ntitle: x\n---",
            "{% note %}Text{% endnote %}", "{# SECRET #}",
            "<% template %>", "import Widget from './Widget'",
            "export default Widget", "Text with &unknown; entity.",
            '!!! note "Title"\n    Body.', '??? info "Title"\n    Body.',
            "Read [multi\nline](secret) now.",
            "See ![multi\nline](image.png) now.",
            "Hello {user.name()}.", "Total: {count + 1}.",
            "Hello {42}.", 'Hello {"world"}.',
            "Hello {\n  user.name()\n}.",
            "Hello {items.map(item => {item.name})}.",
            "Hello {{foo:{bar:42}}}.",
            "Email <foo@example.test> now.",
            "Email <mailto:foo@example.test> now.",
            "Intro\r# Heading\r- item\r> quote\r```\rcode\r```\r",
            "---\r\ntitle: fixed\r\n---\r\nText.",
            "Heading\r\n===\r\nText.",
            "Intro\r\n---\r\nText.",
            "| A | B |\r\n|---|---|\r\n| x | y |\r\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(MARKDOWN.looks_like_markdown(source))

    def test_crlf_and_missing_final_newline_round_trip_exactly(self):
        for source in (
            "# Otsikko\r\n\r\nSelkeä teksti 42.\r\n",
            "# Otsikko\r\n\r\nSelkeä teksti 42.",
        ):
            with self.subTest(source=source):
                _manifest, state = self.plan(source)
                self.assertEqual(MARKDOWN.assemble(
                    source, state, self.unchanged(state)), source)

        for eol in ("\r\n", "\r"):
            source = eol.join((
                "---", "title: SECRET FIXED", "---", "# Otsikko", "",
                "Selkeä teksti 42.",
            ))
            _manifest, state = self.plan(source)
            owned = " ".join(unit["source"] for group in state["groups"]
                             for unit in group["units"])
            self.assertNotIn("SECRET FIXED", owned)
            self.assertEqual(MARKDOWN.assemble(
                source, state, self.unchanged(state)), source)

    def test_span_and_group_bounds_fail_closed(self):
        too_many = "\n\n".join(
            f"Paragraph {index}." for index in range(MARKDOWN.MAX_SPANS + 1))
        too_many = "# Heading\n\n" + too_many
        with self.assertRaises(MARKDOWN.MarkdownRewritePlanError):
            self.plan(too_many, 65536)
        source = "# Heading\n\n" + ("Pitkä teksti jatkuu. " * 300) + "\n"
        with self.assertRaises(MARKDOWN.MarkdownRewritePlanError):
            MARKDOWN.build_plan(source, 512, 1, WORKER._document_plan)


if __name__ == "__main__":
    unittest.main()
