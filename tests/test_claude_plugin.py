from __future__ import annotations

import html.entities
import json
import re
import shutil
import subprocess
import unittest
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_ENTITY_MARKS = frozenset(
    {
        0x20E3,
        *range(0xFE00, 0xFE10),
        *range(0xE0100, 0xE01F0),
    }
)


def rendered_entity_has_language(rendered: str) -> bool:
    return any(
        ord(character) not in IGNORED_ENTITY_MARKS
        and unicodedata.category(character)[0] in {"L", "M"}
        for character in rendered
    )


class ClaudePluginTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for entity-table validation")
    def test_non_language_html_entities_match_the_whatwg_table(self) -> None:
        result = subprocess.run(
            [
                "node",
                "-e",
                "process.stdout.write(JSON.stringify(require('./integrations/non_language_html_entities.js')))",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        bundled = json.loads(result.stdout)
        expected = sorted(
            raw_name[:-1]
            for raw_name, rendered in html.entities.html5.items()
            if raw_name.endswith(";")
            and not rendered_entity_has_language(rendered)
        )

        self.assertEqual(len(expected), 1478)
        self.assertEqual(bundled, expected)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for entity classification")
    def test_every_whatwg_named_entity_has_the_expected_language_classification(self) -> None:
        semicolon_names = sorted(
            raw_name[:-1]
            for raw_name in html.entities.html5
            if raw_name.endswith(";")
        )
        safe_semicolon_names = [
            name
            for name in semicolon_names
            if not rendered_entity_has_language(html.entities.html5[f"{name};"])
        ]
        cases = [f"&{name};" for name in semicolon_names]
        expected = [
            rendered_entity_has_language(html.entities.html5[f"{name};"])
            for name in semicolon_names
        ]

        semicolonless_start = len(cases)
        cases.extend(f"&{name}" for name in semicolon_names)
        expected.extend(
            name not in html.entities.html5
            or rendered_entity_has_language(html.entities.html5[name])
            for name in semicolon_names
        )

        mixed_suffix_start = len(cases)
        cases.extend(f"&{name};Text" for name in safe_semicolon_names)
        expected.extend(True for _ in safe_semicolon_names)

        script = """
const fs = require("fs");
const { hasNaturalLanguage } = require("./integrations/claude_language_hook.js");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map((value) => hasNaturalLanguage(value))));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            input=json.dumps(cases),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        actual = json.loads(result.stdout)
        mismatches = [
            {"input": value, "expected": wanted, "actual": received}
            for value, wanted, received in zip(cases, expected, actual)
            if wanted != received
        ]

        self.assertEqual(len(semicolon_names), 2125)
        self.assertEqual(len(safe_semicolon_names), 1478)
        self.assertEqual(
            expected[semicolonless_start:mixed_suffix_start].count(False),
            41,
        )
        self.assertEqual(len(actual), len(cases))
        self.assertEqual(mismatches[:20], [])

    def test_active_plugin_version_is_synchronized(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        skill = (ROOT / "translate-native" / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(plugin["version"], version)
        self.assertEqual(
            set(re.findall(r"Version (\d+\.\d+\.\d+) `translate-native` plugin", skill)),
            {version},
        )
        self.assertIn(f"### Version {version}:", readme)
        self.assertIn(f"current Version {version} plugin", readme)

    def test_manifests_expose_skill_mcp_and_mandatory_hooks(self) -> None:
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

        self.assertEqual(plugin["name"], "translate-native")
        self.assertEqual(plugin["version"], (ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertIn("./translate-native", plugin["skills"])
        for skill_path in plugin["skills"]:
            self.assertTrue(skill_path.startswith("./"), skill_path)
            skill_directory = ROOT / skill_path
            self.assertTrue(skill_directory.is_dir(), skill_path)
            self.assertTrue((skill_directory / "SKILL.md").is_file(), skill_path)

        marketplace_source = marketplace["plugins"][0]["source"]
        self.assertTrue(marketplace_source.startswith("./"), marketplace_source)
        self.assertEqual((ROOT / marketplace_source).resolve(), ROOT.resolve())
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
        self.assertIn("SessionEnd", hooks)

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
