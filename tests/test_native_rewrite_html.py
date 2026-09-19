"""Synthetic safety tests for lossless long-HTML rewrite planning."""
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HTML = load("test_native_rewrite_html_planner",
            ROOT / "integrations" / "native_rewrite_html.py")
WORKER = load("test_native_rewrite_html_worker",
              ROOT / "integrations" / "native_rewrite_worker.py")


class HtmlPlanTests(unittest.TestCase):
    def plan(self, source, chunk=1024):
        return HTML.build_plan(source, chunk, 10, WORKER._document_plan)

    @staticmethod
    def unchanged(state):
        return {unit["value_id"]: unit["source"]
                for group in state["groups"] for unit in group["units"]}

    def test_lossless_plan_exposes_only_linguistic_spans(self):
        source = ('<!doctype html><html><head><meta name="description" '
                  'content="Selkeä kuvaus 42."><style>.x{color:red}</style>'
                  '<script type="application/ld+json">'
                  '{"headline":"Do not expose"}</script></head><body><main>'
                  '<p>Selkeä teksti {{name}} &amp; numero 42.</p>'
                  '<a href="https://example.test/x?y=1" title="Avaa sivu">Avaa</a>'
                  '<code><span title="fixed">rm -rf /</span></code>'
                  '</main></body></html>')
        manifest, state = self.plan(source)
        owned = [unit["source"] for group in state["groups"] for unit in group["units"]]
        joined = "\n".join(owned)
        self.assertIn("Selkeä kuvaus 42.", joined)
        self.assertIn("Avaa sivu", joined)
        self.assertNotIn("{{name}}", joined)
        self.assertNotIn("&amp;", joined)
        self.assertNotIn("https://", joined)
        self.assertNotIn("rm -rf", joined)
        self.assertNotIn("Do not expose", joined)
        self.assertEqual(HTML.assemble(source, state, self.unchanged(state)), source)
        self.assertEqual(HTML.target_value_map(source)[1], manifest["skeleton_sha256"])

    def test_revises_text_and_attribute_without_reserializing_markup(self):
        source = ("<main><p>On tärkeää huomata tämä.</p>"
                  "<img alt='Vanha kuvaus' src=\"x.png\"></main>")
        _manifest, state = self.plan(source)
        candidates = self.unchanged(state)
        for group in state["groups"]:
            for unit in group["units"]:
                if "On tärkeää" in unit["source"]:
                    candidates[unit["value_id"]] = "Huomaa tämä."
                if "Vanha kuvaus" in unit["source"]:
                    candidates[unit["value_id"]] = "Selkeä kuvaus"
        target = HTML.assemble(source, state, candidates)
        self.assertEqual(
            target,
            "<main><p>Huomaa tämä.</p><img alt='Selkeä kuvaus' src=\"x.png\"></main>",
        )

    def test_malformed_ambiguous_and_foreign_html_block(self):
        cases = (
            "<main><p>Teksti</main>",
            "<main><p>Teksti",
            "<main><p>Teksti <strong>vahva</strong></p></main>",
            "<main><p title=Teksti>Teksti</p></main>",
            "<main><p title=\"A\" TITLE=\"B\">Teksti</p></main>",
            "<main><svg viewBox=\"0 0 1 1\"><text>Teksti</text></svg></main>",
            "<main><textarea>Teksti</textarea></main>",
            "<main><plaintext>Kiinteä</plaintext><p>SALAINEN</p></main>",
            "<main><div/>Teksti</main>",
            "<main><p *ngIf=\"enabled\">Teksti</p></main>",
            "<main><p #reference>Teksti</p></main>",
            "<main><p\u00a0title=\"Teksti\">Sisältö</p></main>",
            "<main><p\vtitle=\"Teksti\">Sisältö</p></main>",
            "<main><script>kiinteä</ script><p>SALAINEN</p></main>",
            "<main><style>kiinteä</ style><p>SALAINEN</p></main>",
            "<main><script><!--<script></script><p>SALAINEN</p></main>",
            "<main><p>Teksti &copy ilman puolipistettä</p></main>",
            "<main><p>{% if user %}Teksti{% endif %}</p></main>",
            "<!ENTITY x \"y\"><main><p>Teksti</p></main>",
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(HTML.HtmlRewritePlanError):
                    self.plan(source)

    def test_candidate_cannot_inject_markup_entity_or_attribute_quote(self):
        cases = (
            ("<main><p>Teksti</p></main>", "<em>Teksti</em>"),
            ("<main><p>Teksti</p></main>", "Teksti &copy;"),
            ('<main><img alt="Teksti" src="x"></main>', 'Uusi "teksti"'),
            ("<main><img alt='Teksti' src=\"x\"></main>", "Uusi 'teksti'"),
        )
        for source, injected in cases:
            with self.subTest(source=source, injected=injected):
                _manifest, state = self.plan(source)
                candidates = self.unchanged(state)
                candidates[next(iter(candidates))] = injected
                with self.assertRaises(HTML.HtmlRewritePlanError):
                    HTML.assemble(source, state, candidates)

    def test_depth_span_and_group_bounds_fail_closed(self):
        with self.assertRaises(HTML.HtmlRewritePlanError):
            self.plan("<div>" * 129 + "Teksti" + "</div>" * 129)
        too_many = "<main>" + "".join(
            f"<p>Teksti {index}</p>" for index in range(HTML.MAX_SPANS + 1)) + "</main>"
        with self.assertRaises(HTML.HtmlRewritePlanError):
            self.plan(too_many, 65536)
        source = "<main><p>" + ("Pitkä teksti. " * 200) + "</p></main>"
        with self.assertRaises(HTML.HtmlRewritePlanError):
            HTML.build_plan(source, 512, 1, WORKER._document_plan)


if __name__ == "__main__":
    unittest.main()
