"""Synthetic safety tests for lossless long-XML rewrite planning."""
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


XML = load("test_native_rewrite_xml_planner",
           ROOT / "integrations" / "native_rewrite_xml.py")
WORKER = load("test_native_rewrite_xml_worker",
              ROOT / "integrations" / "native_rewrite_worker.py")


class XmlPlanTests(unittest.TestCase):
    def plan(self, source, chunk=1024):
        return XML.build_plan(source, chunk, 10, WORKER._document_plan)

    @staticmethod
    def unchanged(state):
        return {unit["value_id"]: unit["source"]
                for group in state["groups"] for unit in group["units"]}

    def test_lossless_plan_exposes_only_element_text(self):
        source = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<resources xmlns:x="urn:meta" x:id="fixed">'
            '<?xml-stylesheet type="text/xsl" href="fixed.xsl"?>'
            '<!-- host comment --><string name="title">Selkeä otsikko 42.</string>'
            '<string name="description">Teksti {{name}} &amp; &#065; numero 42.</string>'
            '<string name="url">https://example.test/x</string>'
            '<string name="code" translatable="false">rm -rf /</string>'
            '<?fixed host?></resources>'
        )
        manifest, state = self.plan(source)
        joined = "\n".join(
            unit["source"] for group in state["groups"] for unit in group["units"])
        self.assertIn("Selkeä otsikko 42.", joined)
        self.assertIn("Teksti", joined)
        self.assertNotIn("{{name}}", joined)
        self.assertNotIn("&amp;", joined)
        self.assertNotIn("&#065;", joined)
        self.assertNotIn("https://", joined)
        self.assertNotIn("rm -rf", joined)
        self.assertNotIn("host comment", joined)
        self.assertEqual(XML.assemble(source, state, self.unchanged(state)), source)
        self.assertEqual(XML.target_value_map(source)[1], manifest["skeleton_sha256"])

    def test_revises_text_without_reserializing_xml(self):
        source = (
            "<resources><string name='heading'>On tärkeää huomata tämä.</string>"
            '<string name="button">Avaa nyt</string></resources>'
        )
        _manifest, state = self.plan(source)
        candidates = self.unchanged(state)
        for group in state["groups"]:
            for unit in group["units"]:
                if "On tärkeää" in unit["source"]:
                    candidates[unit["value_id"]] = "Huomaa tämä."
                if "Avaa nyt" in unit["source"]:
                    candidates[unit["value_id"]] = "Avaa"
        target = XML.assemble(source, state, candidates)
        self.assertEqual(
            target,
            "<resources><string name='heading'>Huomaa tämä.</string>"
            '<string name="button">Avaa</string></resources>',
        )

    def test_preserves_namespaces_empty_elements_and_translate_no(self):
        source = (
            '<resources><plurals name="count"><item quantity="one">Muokkaa tämä.</item>'
            '<item quantity="other">Muokkaa nämä.</item></plurals>'
            '<string name="fixed" translatable="false">Älä paljasta.</string>'
            '</resources>'
        )
        _manifest, state = self.plan(source)
        joined = "\n".join(
            unit["source"] for group in state["groups"] for unit in group["units"])
        self.assertIn("Muokkaa tämä.", joined)
        self.assertIn("Muokkaa nämä.", joined)
        self.assertNotIn("Älä paljasta.", joined)
        self.assertEqual(XML.assemble(source, state, self.unchanged(state)), source)

    def test_dangerous_ambiguous_or_unsupported_xml_blocks(self):
        cases = (
            '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>',
            '<r xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="x"/></r>',
            '<r><![CDATA[Muokkaa tämä.]]></r>',
            '<r>Teksti <b>vahva</b></r>',
            '<r><x:p>Teksti</x:p></r>',
            '<r a="1" a="2">Teksti</r>',
            '<r xmlns:a="urn:x" xmlns:b="urn:x" a:id="1" b:id="2">Teksti</r>',
            '<r a=unquoted>Teksti</r>',
            '<r>Teksti &custom;</r>',
            '<r>Teksti &#0;</r>',
            '<r>Teksti</R>',
            '<r>Teksti</r><r>Toinen</r>',
            '<r>Teksti</r>outside',
            '<r>{% if user %}Teksti{% endif %}</r>',
            '<?xml version="1.1"?><r>Teksti</r>',
            '<catalog><title>Generic prose is not selected.</title></catalog>',
            '<resources><color name="technical">#ff00ff</color></resources>',
            '<resources xml:space="preserve"><string name="x">Teksti</string></resources>',
            '<resources xmlns:x="urn:secret"><x:string name="api">SECRET</x:string></resources>',
            '<resources><string xmlns="urn:secret" name="api">SECRET</string></resources>',
            '<resources><string name="x">Hello<!--fixed-->World</string></resources>',
            '<resources><string name="x">Hello<?fixed?>World</string></resources>',
            '<resources><!--x---><string name="x">Text</string></resources>',
            '<resources><string name="x"other="y">Text</string></resources>',
            '<resources><string name="x">Text</string></resources>\u00a0',
            '<resources xmlns:xi="http://www.w3.org/2001/XIncl&#117;de">'
            '<xi:include href="file:///etc/passwd"/></resources>',
            '<resources><string name="x">Unknown @resource form</string></resources>',
            '<resources><string>Missing name</string></resources>',
            '<resources><string name="">Empty name</string></resources>',
            '<resources><plurals name="count"><item quantity="singular">'
            'Wrong quantity</item></plurals></resources>',
            '<resources><string-array name="items"><item quantity="one">'
            'Wrong attribute</item></string-array></resources>',
            '<resources><plurals><item quantity="one">Missing name</item>'
            '</plurals></resources>',
            '<resources data-note="cafe\u0301"><string name="x">Text</string>'
            '</resources>',
            '<resources><script>SECRET</script><string name="x">Text</string>'
            '</resources>',
            '<resources><apiKey translatable="false">SECRET</apiKey>'
            '<string name="x">Text</string></resources>',
            '<resources><color translate="no">#fff</color>'
            '<string name="x">Text</string></resources>',
            "<resources><string name=\"x\">It's unsafe</string></resources>",
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(XML.XmlRewritePlanError):
                    self.plan(source)

    def test_candidate_cannot_inject_markup_or_entity(self):
        source = '<resources><string name="x">Teksti</string></resources>'
        for injected in ("<em>Teksti</em>", "Teksti &amp;", "Teksti &custom;"):
            with self.subTest(injected=injected):
                _manifest, state = self.plan(source)
                candidates = self.unchanged(state)
                candidates[next(iter(candidates))] = injected
                with self.assertRaises(XML.XmlRewritePlanError):
                    XML.assemble(source, state, candidates)

    def test_android_references_and_escapes_are_host_owned(self):
        source = (
            '<resources><string name="x">Open @string/app_name, '
            '@+id/button, @*android:color/accent, ?attr/colorPrimary, '
            '?android:attr/textColor, \\@string/literal and \\?attr/literal; '
            '\\@null, δοκιμή@παράδειγμα.ελ, परीक्षण@उदाहरण.भारत, '
            'اِختبار@مِثال.مصر; Line\\nnext \\u00E4.</string></resources>'
        )
        _manifest, state = self.plan(source, chunk=4096)
        joined = "\n".join(
            unit["source"] for group in state["groups"] for unit in group["units"])
        self.assertNotIn("@string/app_name", joined)
        self.assertNotIn("@+id/button", joined)
        self.assertNotIn("@*android:color/accent", joined)
        self.assertNotIn("?attr/colorPrimary", joined)
        self.assertNotIn("?android:attr/textColor", joined)
        self.assertNotIn("\\@string/literal", joined)
        self.assertNotIn("\\?attr/literal", joined)
        self.assertNotIn("\\@null", joined)
        self.assertNotIn("δοκιμή@παράδειγμα.ελ", joined)
        self.assertNotIn("परीक्षण@उदाहरण.भारत", joined)
        self.assertNotIn("اِختبار@مِثال.مصر", joined)
        self.assertNotIn("\\n", joined)
        self.assertNotIn("\\u00E4", joined)
        self.assertEqual(XML.assemble(source, state, self.unchanged(state)), source)

    def test_android_quote_wrapper_is_host_owned_and_controls_apostrophes(self):
        source = '<resources><string name="x">"  This is clear.  "</string></resources>'
        _manifest, state = self.plan(source)
        candidates = self.unchanged(state)
        value_id = next(iter(candidates))
        candidates[value_id] = "It's clearer."
        self.assertEqual(
            XML.assemble(source, state, candidates),
            '<resources><string name="x">"  It\'s clearer.  "</string></resources>',
        )
        for invalid in ('He said "yes".', "It's unsafe."):
            unquoted = '<resources><string name="x">This is clear.</string></resources>'
            _manifest, state = self.plan(unquoted)
            candidates = self.unchanged(state)
            candidates[next(iter(candidates))] = invalid
            with self.assertRaises(XML.XmlRewritePlanError):
                XML.assemble(unquoted, state, candidates)

    def test_encoded_control_attributes_remain_host_owned(self):
        source = (
            '<resources xmlns:x="urn:x">'
            '<string name="editable">Edit this sentence.</string>'
            '<string name="a" translatable="f&#97;lse">DO-NOT-SEND API SECRET 42</string>'
            '<string name="b" x:translate="n&#111;">DO-NOT-SEND TOKEN 99</string>'
            '</resources>'
        )
        _manifest, state = self.plan(source)
        joined = "\n".join(
            unit["source"] for group in state["groups"] for unit in group["units"])
        self.assertIn("Edit this sentence.", joined)
        self.assertNotIn("DO-NOT-SEND", joined)

        preserve = ('<resources><string name="x" xml:space="pres&#101;rve">'
                    'Do not expose</string></resources>')
        with self.assertRaises(XML.XmlRewritePlanError):
            self.plan(preserve)

    def test_depth_span_and_group_bounds_fail_closed(self):
        with self.assertRaises(XML.XmlRewritePlanError):
            self.plan("<resources>" + "<x>" * 128 + "Teksti" + "</x>" * 128
                      + "</resources>")
        too_many = "<resources>" + "".join(
            f'<string name="x{index}">Teksti {index}</string>'
            for index in range(XML.MAX_SPANS + 1)) + "</resources>"
        with self.assertRaises(XML.XmlRewritePlanError):
            self.plan(too_many, 65536)
        source = '<resources><string name="x">' + (
            "Pitkä teksti. " * 200) + "</string></resources>"
        with self.assertRaises(XML.XmlRewritePlanError):
            XML.build_plan(source, 512, 1, WORKER._document_plan)


if __name__ == "__main__":
    unittest.main()
