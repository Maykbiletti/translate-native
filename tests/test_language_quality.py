from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "translate-native" / "scripts" / "language_quality.py"
SPEC = importlib.util.spec_from_file_location("language_quality", PATH)
assert SPEC and SPEC.loader
QUALITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = QUALITY
SPEC.loader.exec_module(QUALITY)


class ReceiptTests(unittest.TestCase):
    KEY = b"k" * 32

    def test_signing_key_is_created_once_and_rejects_invalid_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "signing.key"
            created = QUALITY.load_or_create_key(key_path)
            self.assertEqual(len(created), 32)
            self.assertEqual(QUALITY.load_or_create_key(key_path), created)

            key_path.write_bytes(b"short")
            with self.assertRaisesRegex(ValueError, "invalid size"):
                QUALITY.load_or_create_key(key_path)

            key_path.write_bytes(b"x" * (QUALITY.MAX_SIGNING_KEY_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "invalid size"):
                QUALITY.load_or_create_key(key_path)

    def test_missing_signing_key_lookup_does_not_create_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "missing" / "nested" / "signing.key"

            with self.assertRaises(FileNotFoundError):
                QUALITY.load_existing_key(key_path)

            self.assertFalse(key_path.parent.exists())

    def test_signing_key_rejects_identity_change_while_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "signing.key"
            key_path.write_bytes(b"k" * 32)
            key_path.chmod(0o600)
            details = key_path.stat()
            opened = SimpleNamespace(
                st_mode=details.st_mode,
                st_nlink=details.st_nlink,
                st_size=details.st_size,
                st_uid=details.st_uid,
                st_dev=details.st_dev,
                st_ino=details.st_ino,
                st_ctime_ns=details.st_ctime_ns,
                st_mtime_ns=details.st_mtime_ns,
            )
            changed = SimpleNamespace(**vars(opened))
            changed.st_mtime_ns += 1
            with mock.patch.object(
                QUALITY, "_signing_key_fstat", side_effect=(opened, changed),
            ):
                with self.assertRaisesRegex(ValueError, "changed while reading"):
                    QUALITY.load_existing_key(key_path)

    @unittest.skipIf(os.name == "nt", "POSIX link and permission test")
    def test_signing_key_rejects_links_and_does_not_follow_legacy_temp_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path = root / "signing.key"
            sentinel = root / "sentinel"
            sentinel.write_bytes(b"s" * 32)
            legacy_temporary = key_path.with_suffix(".tmp")
            legacy_temporary.symlink_to(sentinel)

            QUALITY.load_or_create_key(key_path)
            self.assertEqual(sentinel.read_bytes(), b"s" * 32)
            self.assertTrue(legacy_temporary.is_symlink())

            key_path.unlink()
            key_path.symlink_to(sentinel)
            with self.assertRaisesRegex(ValueError, "regular file"):
                QUALITY.load_or_create_key(key_path)
            self.assertEqual(sentinel.read_bytes(), b"s" * 32)

            key_path.unlink()
            key_path.write_bytes(b"k" * 32)
            key_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                QUALITY.load_or_create_key(key_path)

    @unittest.skipIf(os.name == "nt", "POSIX hard-link test")
    def test_signing_key_rejects_hard_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path = root / "signing.key"
            alias = root / "signing-key-alias"
            key_path.write_bytes(b"k" * 32)
            key_path.chmod(0o600)
            os.link(key_path, alias)

            with self.assertRaisesRegex(ValueError, "exactly one hard link"):
                QUALITY.load_existing_key(key_path)
            self.assertEqual(alias.read_bytes(), b"k" * 32)

            key_path.unlink()
            alias.unlink()

            def link_during_creation(_descriptor: int) -> None:
                os.link(key_path, alias)

            with mock.patch.object(QUALITY.os, "fsync", side_effect=link_during_creation):
                with self.assertRaisesRegex(ValueError, "exactly one hard link"):
                    QUALITY.load_or_create_key(key_path)
            self.assertEqual(alias.read_bytes(), key_path.read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX directory safety test")
    def test_signing_key_loader_rejects_unsafe_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            target_key = target / "signing.key"
            target_key.write_bytes(b"a" * 32)
            target_key.chmod(0o600)
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "safely open"):
                QUALITY.load_existing_key(linked / "signing.key")
            self.assertEqual(target_key.read_bytes(), b"a" * 32)

            creation_target = root / "creation-target"
            creation_target.mkdir()
            creation_link = root / "creation-link"
            creation_link.symlink_to(creation_target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "safely open"):
                QUALITY.load_or_create_key(creation_link / "signing.key")
            self.assertFalse((creation_target / "signing.key").exists())

            writable = root / "writable"
            writable.mkdir()
            writable_key = writable / "signing.key"
            writable_key.write_bytes(b"b" * 32)
            writable_key.chmod(0o600)
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(ValueError, "writable outside its owner"):
                    QUALITY.load_existing_key(writable_key)
            finally:
                writable.chmod(0o700)

            creation_writable = root / "creation-writable"
            creation_writable.mkdir()
            creation_writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(ValueError, "writable outside its owner"):
                    QUALITY.load_or_create_key(creation_writable / "signing.key")
                self.assertFalse((creation_writable / "signing.key").exists())
            finally:
                creation_writable.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_signing_key_loader_detects_parent_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "trusted"
            trusted.mkdir()
            key_path = trusted / "signing.key"
            key_path.write_bytes(b"a" * 32)
            key_path.chmod(0o600)
            replacement = root / "replacement"
            replacement.mkdir()
            replacement_key = replacement / "signing.key"
            replacement_key.write_bytes(b"b" * 32)
            replacement_key.chmod(0o600)
            real_open = QUALITY.os.open
            exchanged = False

            def exchange_parent(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal exchanged
                if Path(path).name == "signing.key" and not exchanged:
                    exchanged = True
                    trusted.rename(root / "trusted-old")
                    replacement.rename(trusted)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(QUALITY.os, "open", side_effect=exchange_parent):
                with self.assertRaisesRegex(ValueError, "directory changed"):
                    QUALITY.load_existing_key(key_path)

            self.assertEqual(key_path.read_bytes(), b"b" * 32)
            self.assertEqual((root / "trusted-old" / "signing.key").read_bytes(), b"a" * 32)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_signing_key_creator_detects_parent_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "trusted"
            trusted.mkdir()
            key_path = trusted / "signing.key"
            replacement = root / "replacement"
            replacement.mkdir()
            real_open = QUALITY.os.open
            exchanged = False

            def exchange_parent(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal exchanged
                if Path(path).name == "signing.key" and not exchanged:
                    exchanged = True
                    trusted.rename(root / "trusted-old")
                    replacement.rename(trusted)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(QUALITY.os, "open", side_effect=exchange_parent):
                with self.assertRaisesRegex(ValueError, "directory changed"):
                    QUALITY.load_or_create_key(key_path)

            self.assertFalse(key_path.exists())
            self.assertEqual((root / "trusted-old" / "signing.key").stat().st_size, 32)

    def test_receipt_is_bound_to_every_input(self) -> None:
        token = QUALITY.issue_receipt("Hello", "Hej", "sv-SE", self.KEY)
        self.assertTrue(QUALITY.verify_receipt(token, "Hello", "Hej", "sv-SE", self.KEY)["valid"])
        for source, target, language in (("Changed", "Hej", "sv-SE"), ("Hello", "Hallå", "sv-SE"), ("Hello", "Hej", "da-DK")):
            self.assertFalse(QUALITY.verify_receipt(token, source, target, language, self.KEY)["valid"])

    def test_forged_receipt_fails(self) -> None:
        token = QUALITY.issue_receipt("Hello", "Hej", "sv-SE", self.KEY)
        forged = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertFalse(QUALITY.verify_receipt(forged, "Hello", "Hej", "sv-SE", self.KEY)["valid"])

    def test_transport_normalization_ignores_bom_and_line_endings(self) -> None:
        token = QUALITY.issue_receipt("First\nSecond", "Första\nAndra", "sv-SE", self.KEY)
        self.assertTrue(QUALITY.verify_receipt(token, "\ufeffFirst\r\nSecond", "Första\r\nAndra", "sv-SE", self.KEY)["valid"])

    def test_mojibake_is_not_normalized_away(self) -> None:
        token = QUALITY.issue_receipt("Hello", "Förstå", "sv-SE", self.KEY)
        self.assertFalse(QUALITY.verify_receipt(token, "Hello", "FÃ¶rstÃ¥", "sv-SE", self.KEY)["valid"])

    def test_receipt_is_bound_to_short_text_review_policy(self) -> None:
        token = QUALITY.issue_receipt(
            "Build faster", "Bygg snabbare", "sv-SE", self.KEY,
            content_type="title", short_text_reviewed=True,
        )
        self.assertTrue(QUALITY.verify_receipt(
            token, "Build faster", "Bygg snabbare", "sv-SE", self.KEY,
            "title", True,
        )["valid"])
        self.assertFalse(QUALITY.verify_receipt(
            token, "Build faster", "Bygg snabbare", "sv-SE", self.KEY,
            "prose", False,
        )["valid"])


class LanguageSafetyTests(unittest.TestCase):
    def test_balanced_isolates_pass_but_overrides_and_unpaired_fail(self) -> None:
        self.assertEqual([], QUALITY.bidi_findings("مرحبًا \u2066https://example.com\u2069"))
        self.assertTrue(QUALITY.bidi_findings("safe\u202eevil"))
        self.assertTrue(QUALITY.bidi_findings("text\u2069"))

    def test_script_identity(self) -> None:
        self.assertEqual("pass", QUALITY.script_report("Привіт, Україно!", "uk-UA")["status"])
        self.assertEqual("fail", QUALITY.script_report("Pryvit Ukraino", "uk-UA")["status"])
        self.assertEqual("not-evaluated", QUALITY.script_report("Kaixo", "eu-ES")["status"])

    def test_glossary_exact_and_regex(self) -> None:
        glossary = {"workspace": "Arbeitsbereich", "invoice": {"target": r"Rechnung(?:en)?", "regex": True}}
        self.assertEqual([], QUALITY.glossary_findings("Arbeitsbereich mit Rechnungen", glossary))
        self.assertEqual(2, len(QUALITY.glossary_findings("Bereich", glossary)))
