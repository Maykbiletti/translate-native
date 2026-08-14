from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ClaudePluginTests(unittest.TestCase):
    def test_manifests_expose_skill_mcp_and_mandatory_hooks(self) -> None:
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

        self.assertEqual(plugin["name"], "translate-native")
        self.assertEqual(plugin["version"], (ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertIn("./translate-native/SKILL.md", plugin["skills"])
        self.assertEqual(marketplace["plugins"][0]["source"], ".")
        self.assertNotIn("version", marketplace["plugins"][0])
        self.assertIn("guard", mcp["mcpServers"])
        self.assertEqual(mcp["mcpServers"]["guard"]["type"], "http")
        self.assertIn("SessionStart", hooks)
        self.assertIn("SubagentStart", hooks)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("PostToolUse", hooks)
        self.assertIn("PostToolUseFailure", hooks)
        self.assertIn("StopFailure", hooks)
        self.assertIn("Stop", hooks)
        self.assertIn("SubagentStop", hooks)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Claude hook tests")
    def test_hook_requires_exact_verified_final_text(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "claude_language_hook_test.js")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
