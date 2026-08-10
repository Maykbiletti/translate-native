from __future__ import annotations

import importlib.util
import json
import subprocess
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

    def test_reordered_json_keys_keep_segment_identity_alignment(self) -> None:
        source = {
            "title": "Wichtige Informationen für Ihren Projektantrag",
            "hinweis": "Bitte beachten Sie die Fristen …",
        }
        target = {
            "hinweis": "Bitte beachten Sie die Fristen …",
            "title": "Viktig information för din projektansökan",
        }
        errors = GUARD.identity_errors(
            GUARD.json_segments(source),
            GUARD.json_segments(target),
            "$segments",
        )
        self.assertEqual(1, len(errors))
        self.assertIn("linguistic segment is unchanged", errors[0])


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

    def test_jsonld_volume_counts_copy_but_not_urls_or_schema_types(self) -> None:
        document = {
            "@type": "Article",
            "headline": "A complete human-language headline",
            "url": "https://example.com/a",
            "publisher": {"@type": "Organization", "name": "BLUN"},
        }
        self.assertEqual(
            ["A complete human-language headline", "BLUN"],
            GUARD.jsonld_segments(document),
        )

    def test_reordered_unchanged_html_segments_are_blocked(self) -> None:
        first = "Please observe every deadline before submitting your complete application."
        second = "Contact the project office if you need additional information or assistance."
        source = f"<main><p>{first}</p><p>{second}</p></main>"
        target = f"<main><p>{second}</p><p>{first}</p></main>"
        errors = GUARD.structured_identity_errors(source, target, "html")
        self.assertEqual(2, len(errors))
        self.assertTrue(all("$html/main[0]/p" in error for error in errors))


class UnicodeTests(unittest.TestCase):
    def test_accepts_nfc(self) -> None:
        self.assertEqual([], GUARD.normalization_errors("Čeština — 中文", Path("target.txt")))

    def test_rejects_decomposed_text(self) -> None:
        decomposed = unicodedata.normalize("NFD", "Café")
        errors = GUARD.normalization_errors(decomposed, Path("target.txt"))
        self.assertEqual(1, len(errors))
        self.assertIn("not Unicode NFC-normalized", errors[0])


class VolumeIntegrityTests(unittest.TestCase):
    SOURCE = (
        "BLUN verbindet leistungsstarke KI-Modelle mit vollständigen Arbeitsbereichen für Apps, "
        "Websites, Softwareentwicklung, Sprachen, Bilder und automatisierte Abläufe. Statt für "
        "jeden Arbeitsschritt ein neues Werkzeug zu öffnen, arbeiten alle Komponenten zusammen."
    )

    def test_major_plain_text_truncation_is_blocked(self) -> None:
        truncated = self.SOURCE[:64]
        errors = GUARD.volume_errors([self.SOURCE], [truncated])
        self.assertTrue(errors)
        self.assertIn("target linguistic volume", "\n".join(errors))

    def test_substantial_unchanged_source_is_blocked(self) -> None:
        self.assertTrue(GUARD.identity_errors(self.SOURCE, self.SOURCE))

    def test_transport_normalization_cannot_bypass_identity(self) -> None:
        source = (self.SOURCE + "\r\n") * 2
        target = "\ufeff" + source.replace("\r\n", "\n")
        self.assertTrue(GUARD.identity_errors(source, target))

    def test_short_shared_terms_do_not_trigger_identity(self) -> None:
        for term in ("BLUN King", "E-Mail"):
            with self.subTest(term=term):
                self.assertEqual([], GUARD.identity_errors(term, term))

    def test_short_brand_and_copyright_segments_can_remain_unchanged(self) -> None:
        shared = ["BLUN King", "Copyright © 2026 BLUN. All rights reserved."]
        self.assertEqual([], GUARD.identity_errors(shared, shared, "$segments"))

    def test_prose_mentioning_copyright_is_not_a_fixed_legal_line(self) -> None:
        prose = "Copyright © 2026 BLUN provides software for every customer"
        errors = GUARD.identity_errors([prose], [prose], "$segments")
        self.assertEqual(1, len(errors))
        self.assertIn("unchanged from the source", errors[0])

    def test_short_title_case_copyright_prose_is_not_a_fixed_legal_line(self) -> None:
        prose = "Copyright © 2026 BLUN Product Updates Announce Changes"
        self.assertEqual(54, len(prose))
        errors = GUARD.identity_errors([prose], [prose], "$segments")
        self.assertEqual(1, len(errors))
        self.assertIn("unchanged from the source", errors[0])

    def test_multiword_legal_owner_remains_valid_fixed_content(self) -> None:
        notice = "Copyright © 2026 The Walt Disney Company. All rights reserved."
        self.assertEqual([], GUARD.identity_errors([notice], [notice], "$segments"))

    def test_copyright_notice_without_year_is_not_fixed_content(self) -> None:
        notice = "Copyright © BLUN International Software Company"
        errors = GUARD.identity_errors([notice], [notice], "$segments")
        self.assertEqual(1, len(errors))
        self.assertIn("unchanged from the source", errors[0])

    def test_one_unchanged_structured_segment_is_blocked(self) -> None:
        source = {
            "title": "Wichtige Informationen für Ihren Projektantrag",
            "hinweis": "Bitte beachten Sie die Fristen …",
        }
        target = {
            "title": "Viktig information för din projektansökan",
            "hinweis": "Bitte beachten Sie die Fristen …",
        }
        errors = GUARD.structured_identity_errors(
            json.dumps(source, ensure_ascii=False),
            json.dumps(target, ensure_ascii=False),
            "json",
        )
        self.assertEqual(1, len(errors))
        self.assertIn("$.hinweis", errors[0])
        self.assertIn("untranslated segment is blocked", errors[0])

    def test_equal_volume_literal_text_is_not_misrepresented_as_semantic_proof(self) -> None:
        literal = (
            "BLUN connects powerful AI models with complete workspaces for apps, websites, "
            "software development, languages, images, and automated workflows. Instead of "
            "opening a new tool for each work step, all components work together."
        )
        self.assertEqual([], GUARD.volume_errors([self.SOURCE], [literal]))

    def test_compact_cjk_translation_is_not_rejected_by_latin_ratio(self) -> None:
        chinese = (
            "BLUN将强大的人工智能模型与完整工作空间整合到一个欧洲平台。"
            "模型、智能代理、接口和服务器可以共同处理应用、网站、代码、语言、图像与自动化流程。"
        )
        self.assertEqual([], GUARD.volume_errors([self.SOURCE], [chinese]))

    def test_html_segment_and_volume_loss_is_blocked(self) -> None:
        source = "<main><h1>Vollständige Überschrift für das Produkt</h1><p>" + self.SOURCE + "</p></main>"
        target = "<main><h1>Complete product headline</h1><p>Short fragment.</p></main>"
        errors = GUARD.volume_errors(GUARD.html_segments(source), GUARD.html_segments(target))
        self.assertTrue(errors)

    def test_format_detection_is_measured_not_caller_declared(self) -> None:
        self.assertEqual("json", GUARD.detect_content_format('{"copy":"Hello"}'))
        self.assertEqual("html", GUARD.detect_content_format("<main><p>Hello</p></main>"))
        self.assertEqual("xml", GUARD.detect_content_format("<resources><string>Hello</string></resources>"))
        self.assertEqual("po", GUARD.detect_content_format('msgid "Hello"\nmsgstr "Hallo"\n'))
        self.assertEqual("subtitle", GUARD.detect_content_format("00:00:01,000 --> 00:00:03,000\nHello"))

    def test_cli_returns_nonzero_for_seventy_percent_omission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            target = Path(directory) / "target.txt"
            source.write_text(self.SOURCE, encoding="utf-8")
            target.write_text(self.SOURCE[:64], encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), source, target],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("linguistic volume", result.stderr)

    def test_cli_returns_nonzero_for_unchanged_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            target = Path(directory) / "target.txt"
            source.write_text(self.SOURCE, encoding="utf-8")
            target.write_text(self.SOURCE, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), source, target],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("target is unchanged from the source", result.stderr)

    def test_cli_blocks_one_unchanged_json_value(self) -> None:
        source_data = {
            "title": "Wichtige Informationen für Ihren Projektantrag",
            "hinweis": "Bitte beachten Sie die Fristen …",
        }
        target_data = {
            "title": "Viktig information för din projektansökan",
            "hinweis": "Bitte beachten Sie die Fristen …",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "i_q.json"
            target = Path(directory) / "i_z.json"
            source.write_text(json.dumps(source_data, ensure_ascii=False), encoding="utf-8")
            target.write_text(json.dumps(target_data, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), source, target],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(1, result.returncode)
        self.assertIn("$.hinweis", result.stderr)
        self.assertIn("linguistic segment is unchanged", result.stderr)

    def test_cli_blocks_short_title_case_copyright_prose(self) -> None:
        prose = "Copyright © 2026 BLUN Product Updates Announce Changes"
        source_data = {
            "title": "Important information about product updates",
            "notice": prose,
        }
        target_data = {
            "title": "Viktig information om produktuppdateringar",
            "notice": prose,
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            target = Path(directory) / "target.json"
            source.write_text(json.dumps(source_data, ensure_ascii=False), encoding="utf-8")
            target.write_text(json.dumps(target_data, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), source, target],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(1, result.returncode)
        self.assertIn("$.notice", result.stderr)
        self.assertIn("linguistic segment is unchanged", result.stderr)

    def test_cli_missing_paths_are_not_misreported_as_identity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing-source.txt"
            target = Path(directory) / "missing-target.txt"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), source, target],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(2, result.returncode)
        self.assertIn("cannot read file", result.stderr)
        self.assertNotIn("unchanged from the source", result.stderr)


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

    def test_ass_dialogue_text_is_included_in_volume_check(self) -> None:
        source = (
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,This complete subtitle must be measured.\n"
        )
        self.assertEqual(
            ["This complete subtitle must be measured."],
            GUARD.subtitle_segments(source),
        )


if __name__ == "__main__":
    unittest.main()
