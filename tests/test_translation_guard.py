from __future__ import annotations

import importlib.util
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "translate-native" / "scripts" / "translation_guard.py"
SPEC = importlib.util.spec_from_file_location("translation_guard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


class ProtectedTextTests(unittest.TestCase):
    def test_preserves_placeholders_urls_and_code(self) -> None:
        source = (
            "Hello {{name}}. Open https://example.com/account and run "
            "`billing sync --retry`."
        )
        target = (
            "Hej {{name}}! Öppna https://example.com/account och kör "
            "`billing sync --retry`."
        )
        self.assertEqual([], GUARD.compare_tokens(source, target, "$"))

    def test_sentence_punctuation_is_not_part_of_url(self) -> None:
        source = "Open https://example.com/invite, then continue."
        target = "请打开 https://example.com/invite，然后继续。"
        self.assertEqual([], GUARD.compare_tokens(source, target, "$"))

    def test_reports_missing_and_changed_tokens(self) -> None:
        errors = GUARD.compare_tokens(
            "Hello {name}. Visit https://example.com/help.",
            "Hola. Visita https://example.com/ayuda.",
            "$",
        )
        message = "\n".join(errors)
        self.assertIn("missing", message)
        self.assertIn("ICU argument 'name'", message)
        self.assertIn("https://example.com/help", message)
        self.assertIn("added", message)


class JsonTests(unittest.TestCase):
    def test_allows_native_string_values(self) -> None:
        source = {
            "welcome": "Welcome, {name}!",
            "files": ["Delete %1$d file", "Delete %1$d files"],
            "enabled": True,
            "limit": 5,
        }
        target = {
            "welcome": "Ongi etorri, {name}!",
            "files": ["Ezabatu %1$d fitxategi", "Ezabatu %1$d fitxategi"],
            "enabled": True,
            "limit": 5,
        }
        self.assertEqual([], GUARD.compare_json(source, target))

    def test_rejects_keys_types_lengths_and_fixed_values(self) -> None:
        source = {"welcome": "Hello {name}", "items": ["One", "Two"], "ok": True, "limit": 5}
        target = {"benvinguda": "Hola", "items": ["Un"], "ok": False, "limit": "5"}
        message = "\n".join(GUARD.compare_json(source, target))
        self.assertIn("missing key 'welcome'", message)
        self.assertIn("added key 'benvinguda'", message)
        self.assertIn("array length changed", message)
        self.assertIn("non-string value changed", message)
        self.assertIn("type changed", message)


class IcuTests(unittest.TestCase):
    def test_preserves_plural_contract(self) -> None:
        source = "{count, plural, one {# file for {name}} other {# files for {name}}}"
        target = "{count, plural, one {{name}: # fitxer} other {{name}: # fitxers}}"
        self.assertEqual([], GUARD.compare_tokens(source, target, "$"))

    def test_rejects_changed_plural_contract(self) -> None:
        source = "{count, plural, one {# file} other {# files}}"
        target = "{total, select, one {fitxer bat} many {fitxategiak}}"
        message = "\n".join(GUARD.compare_tokens(source, target, "$"))
        self.assertIn("ICU argument 'count'", message)
        self.assertIn("ICU formatter 'plural'", message)
        self.assertIn("ICU selector 'other'", message)
        self.assertIn("ICU number sign '#'", message)


class HtmlTests(unittest.TestCase):
    SOURCE = """<!doctype html>
<!-- component:v2 -->
<main class="billing">
  <a href="https://example.com/billing" title="Billing for {name}" aria-label="Open billing">Hello, <strong>{name}</strong></a>
  <input name="query" placeholder="Search {{count}} invoices">
  <script>window.route = "/billing";</script>
</main>
"""

    def test_allows_visible_text_and_linguistic_attributes(self) -> None:
        target = """<!doctype html>
<!-- component:v2 -->
<main class="billing">
  <a href="https://example.com/billing" title="Facturación de {name}" aria-label="Abrir facturación">Hola, <strong>{name}</strong></a>
  <input name="query" placeholder="Buscar {{count}} facturas">
  <script>window.route = "/billing";</script>
</main>
"""
        self.assertEqual([], GUARD.compare_html(self.SOURCE, target))

    def test_rejects_structure_attributes_placeholders_and_code_changes(self) -> None:
        target = """<!doctype html>
<!-- translated comment -->
<main class="payments">
  <a href="https://example.com/pagos" title="Facturación" aria-label="Abrir facturación">Hola, <b>{name}</b></a>
  <input name="query" placeholder="Buscar facturas">
  <script>window.route = "/pagos";</script>
</main>
"""
        message = "\n".join(GUARD.compare_html(self.SOURCE, target))
        self.assertIn("comment", message)
        self.assertIn("payments", message)
        self.assertIn("https://example.com/pagos", message)
        self.assertIn("template", message)
        self.assertIn("window.route", message)

    def test_allows_translated_meta_descriptions_and_social_copy(self) -> None:
        source = """<head>
<meta name="description" content="Manage invoices for {name}">
<meta property="og:title" content="Billing made simple">
<meta name="twitter:description" content="Review {{count}} invoices">
</head>"""
        target = """<head>
<meta name="description" content="Verwalte Rechnungen für {name}">
<meta property="og:title" content="Einfache Rechnungsverwaltung">
<meta name="twitter:description" content="Prüfe {{count}} Rechnungen">
</head>"""
        self.assertEqual([], GUARD.compare_html(source, target))

    def test_rejects_changed_technical_meta_content(self) -> None:
        source = '<meta name="viewport" content="width=device-width, initial-scale=1">'
        target = '<meta name="viewport" content="width=900, initial-scale=2">'
        message = "\n".join(GUARD.compare_html(source, target))
        self.assertIn("viewport", message)
        self.assertIn("width=900", message)

    def test_meta_translation_must_preserve_placeholders(self) -> None:
        source = '<meta name="description" content="Billing for {name}">'
        target = '<meta name="description" content="Rechnungsverwaltung">'
        message = "\n".join(GUARD.compare_html(source, target))
        self.assertIn("ICU argument", message)

    def test_jsonld_linguistic_fields_translate_but_urls_and_structure_do_not(self) -> None:
        source = '<script type="application/ld+json">{"@type":"Article","headline":"Hello {name}","url":"https://example.com/a"}</script>'
        target = '<script type="application/ld+json">{"@type":"Article","headline":"Hallo {name}","url":"https://example.com/a"}</script>'
        self.assertEqual([], GUARD.compare_html(source, target))
        broken = target.replace("https://example.com/a", "https://example.com/b")
        self.assertTrue(GUARD.compare_html(source, broken))


class UnicodeTests(unittest.TestCase):
    def test_accepts_nfc(self) -> None:
        self.assertEqual([], GUARD.normalization_errors("Čeština — 中文", Path("target.txt")))

    def test_rejects_decomposed_text(self) -> None:
        decomposed = unicodedata.normalize("NFD", "Café")
        errors = GUARD.normalization_errors(decomposed, Path("target.txt"))
        self.assertEqual(1, len(errors))
        self.assertIn("not Unicode NFC-normalized", errors[0])


class AdditionalFormatTests(unittest.TestCase):
    def test_xml_allows_text_but_preserves_structure_attributes_and_tokens(self) -> None:
        source = '<resources><string name="welcome">Hello {name}</string></resources>'
        target = '<resources><string name="welcome">Hallo {name}</string></resources>'
        self.assertEqual([], GUARD.compare_xml(source, target))
        broken = '<resources><string name="other">Hallo</string></resources>'
        self.assertTrue(GUARD.compare_xml(source, broken))

    def test_po_preserves_msgids_and_placeholders(self) -> None:
        source = 'msgid "Hello {name}"\nmsgstr "Hello {name}"\n'
        target = 'msgid "Hello {name}"\nmsgstr "Hallo {name}"\n'
        self.assertEqual([], GUARD.compare_po(source, target))
        self.assertTrue(GUARD.compare_po(source, 'msgid "Changed"\nmsgstr "Hallo"\n'))

    def test_apple_strings_preserves_keys_and_tokens(self) -> None:
        source = '"welcome" = "Hello %@";\n'
        target = '"welcome" = "Hallo %@";\n'
        self.assertEqual([], GUARD.compare_apple_strings(source, target))
        self.assertTrue(GUARD.compare_apple_strings(source, '"other" = "Hallo";\n'))
        self.assertTrue(GUARD.compare_apple_strings(source, '"welcome" = "Hallo";\n'))

    def test_subtitles_preserve_timestamps(self) -> None:
        source = '1\n00:00:01,000 --> 00:00:03,000\nHello\n'
        target = '1\n00:00:01,000 --> 00:00:03,000\nHallo\n'
        self.assertEqual([], GUARD.compare_subtitles(source, target))
        self.assertTrue(GUARD.compare_subtitles(source, target.replace("03,000", "04,000")))


if __name__ == "__main__":
    unittest.main()
