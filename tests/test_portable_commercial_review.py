from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_commercial_localization import PROFILE, SCHEMA, SOURCE, TARGET, evidence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "translate-native" / "scripts"


class PortableCommercialReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Deliberately copy ONLY the skill helpers, not integrations or the guard.
        for name in ("check_commercial_review.py", "commercial_localization_profile.py"):
            shutil.copy2(SCRIPTS / name, self.root / name)

    def run_cli(self, source=SOURCE, target=TARGET, report=None, raw_review=None):
        (self.root / "source.txt").write_bytes(source.encode("utf-8"))
        (self.root / "target.txt").write_bytes(target.encode("utf-8"))
        raw = raw_review if raw_review is not None else json.dumps(
            evidence(source, target) if report is None else report, ensure_ascii=False,
        )
        (self.root / "review.json").write_bytes(raw.encode("utf-8"))
        process = self.command("--source", "source.txt", "--target", "target.txt", "--review", "review.json")
        result = json.loads(process.stdout)
        self.assertFalse(result["release_allowed"])
        self.assertNotIn("release_token", result)
        self.assertEqual(process.stderr, "")
        return process.returncode, result

    def command(self, *args):
        return subprocess.run([sys.executable, "check_commercial_review.py", *args], cwd=self.root,
                              capture_output=True, text=True, timeout=10)

    def test_skill_only_cli_exposes_current_contract_without_claiming_approval(self):
        process = self.command("--contract")
        self.assertEqual(process.returncode, 0)
        payload = json.loads(process.stdout)
        self.assertFalse(payload["release_allowed"])
        self.assertEqual(payload["commercial_review"], PROFILE.review_contract(SCHEMA))
        code, payload = self.run_cli(report=payload["commercial_review"])
        self.assertEqual(code, 1)

    def test_valid_evidence_is_unsigned_and_hashes_exact_inputs(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "EVIDENCE_VALID")
        for name in ("source", "target", "review"):
            suffix = "json" if name == "review" else "txt"
            self.assertEqual(payload[name + "_sha256"], hashlib.sha256(
                (self.root / (name + "." + suffix)).read_bytes()).hexdigest())

    def test_cli_and_worker_report_identical_blocking_codes(self):
        for dimension in PROFILE.DIMENSIONS:
            for status in ("changed", "uncertain"):
                report = evidence()
                report["checks"][dimension]["status"] = status
                with self.subTest(dimension=dimension, status=status):
                    with self.assertRaises(PROFILE.CommercialReviewBlocked) as blocked:
                        PROFILE.validate_review(report, SOURCE, TARGET, SCHEMA)
                    code, payload = self.run_cli(report=report)
                    self.assertEqual(code, 1)
                    self.assertEqual(payload["reason"], blocked.exception.code)

    def test_invalid_evidence_offsets_and_versions_remain_blocked(self):
        reports = []
        for span in ([False, 1], [0, len(SOURCE) + 1], [1, 0], [0, 0]):
            report = evidence()
            report["checks"]["amount_currency"]["items"][0]["source_span"] = span
            reports.append(report)
        report = evidence()
        report["schema"] = "translate-native.commercial.stale"
        reports.append(report)
        reports.append({})
        for report in reports:
            code, payload = self.run_cli(report=report)
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "BLOCK")

    def test_native_digits_unicode_and_crlf_are_not_normalized(self):
        # Protocol-only fixtures, not linguistic quality judgments.
        for text in ("١٢ €", "għaxra €", "kymmenen €", "e\u0301 €\r\n😀", "十二 €"):
            code, payload = self.run_cli(source=text, target=text)
            self.assertEqual(code, 0)
            self.assertEqual(payload["target_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_bad_json_and_limits_fail_closed_without_echoing_customer_data(self):
        for raw in ('{"private-customer-data":', '{"schema":1,"schema":2}',
                    '{"private-customer-data":NaN}', '[' * 2000, 'x' * 2_000_001):
            code, payload = self.run_cli(raw_review=raw)
            self.assertEqual(code, 1)
            self.assertEqual(payload["reason"], "review.commercial.input_invalid")
            self.assertNotIn("private-customer-data", json.dumps(payload))
        for text in ("", " \n", "\ufefftext", "x" * 2_000_001):
            code, payload = self.run_cli(source=text)
            self.assertEqual(code, 1)

    def test_missing_file_and_invalid_utf8_have_content_free_errors(self):
        self.run_cli()
        for raw in (b"\xff", b"\xef\xbb\xbftext"):
            (self.root / "source.txt").write_bytes(raw)
            process = self.command("--source", "source.txt", "--target", "target.txt", "--review", "review.json")
            self.assertEqual(process.returncode, 1)
            self.assertEqual(json.loads(process.stdout)["reason"], "review.commercial.input_invalid")
        process = self.command("--source", "private-missing.txt", "--target", "target.txt", "--review", "review.json")
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("private-missing", process.stdout + process.stderr)

    def test_incomplete_arguments_cannot_return_success(self):
        for args in ((), ("--source", "source.txt"), ("--contract", "--source", "source.txt")):
            self.assertEqual(self.command(*args).returncode, 2)

    def test_compatibility_module_executes_the_bundled_implementation(self):
        self.assertEqual(Path(PROFILE.validate_review.__code__.co_filename).resolve(),
                         (SCRIPTS / "commercial_localization_profile.py").resolve())


if __name__ == "__main__":
    unittest.main()
