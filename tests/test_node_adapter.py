from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NodeAdapterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_node_adapter_blocks_bypass_and_guards_telegram(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "node_language_guard_test.js")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_blun_code_adapter_bootstraps_buffers_and_releases(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "blun_code_language_guard_test.js")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
