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


class UnicodeTests(unittest.TestCase):
    def test_accepts_nfc(self) -> None:
        self.assertEqual([], GUARD.normalization_errors("Čeština — 中文", Path("target.txt")))

    def test_rejects_decomposed_text(self) -> None:
        decomposed = unicodedata.normalize("NFD", "Café")
        errors = GUARD.normalization_errors(decomposed, Path("target.txt"))
        self.assertEqual(1, len(errors))
        self.assertIn("not Unicode NFC-normalized", errors[0])


if __name__ == "__main__":
    unittest.main()
