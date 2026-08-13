from __future__ import annotations

import importlib.util
import contextlib
import io
import sys
import tempfile
import unittest
import os
import stat
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "installer" / "blun_language_guard.py"
SPEC = importlib.util.spec_from_file_location("blun_installer", PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INSTALLER
SPEC.loader.exec_module(INSTALLER)


class InstallerTests(unittest.TestCase):
    def _fake_claude(self, root: Path, *, old_version: str = "6.5.0", new_version: str = "6.6.0", fail_update: bool = False) -> tuple[Path, Path]:
        state = root / "plugin-version.txt"
        state.write_text(old_version, encoding="utf-8")
        executable = root / "claude"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"state = pathlib.Path({str(state)!r})\n"
            "args = sys.argv[1:]\n"
            "if args == ['plugin', 'list', '--json']:\n"
            "    print(json.dumps([{'name': 'translate-native', 'marketplace': 'blun-language-tools', "
            "'version': state.read_text().strip(), 'enabled': True, 'errors': []}]))\n"
            "    raise SystemExit(0)\n"
            "if args == ['plugin', 'update', 'translate-native@blun-language-tools', '--scope', 'user']:\n"
            + ("    raise SystemExit(1)\n" if fail_update else f"    state.write_text({new_version!r})\n    raise SystemExit(0)\n")
            + "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable, state

    def test_atomic_symlink_is_idempotent_and_refuses_real_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            destination = root / "nested" / "skill"
            INSTALLER.atomic_symlink(source, destination)
            INSTALLER.atomic_symlink(source, destination)
            self.assertTrue(destination.is_symlink())
            destination.unlink()
            destination.mkdir()
            with self.assertRaises(RuntimeError):
                INSTALLER.atomic_symlink(source, destination)

    def test_update_refuses_non_git_installation(self) -> None:
        original = INSTALLER.repository_root
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.repository_root = lambda: Path(directory)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(2, INSTALLER.update())
            finally:
                INSTALLER.repository_root = original

    def test_auto_update_policy_can_be_enabled_without_scheduler(self) -> None:
        original_config = INSTALLER.UPDATE_CONFIG
        original_state = INSTALLER.UPDATE_STATE
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.UPDATE_CONFIG = Path(directory) / "updater.json"
            INSTALLER.UPDATE_STATE = Path(directory) / "state.json"
            try:
                self.assertEqual(0, INSTALLER.auto_update("enable", 12, True, scheduler=False))
                policy = INSTALLER.json.loads(INSTALLER.UPDATE_CONFIG.read_text(encoding="utf-8"))
                self.assertEqual(policy["interval_hours"], 12)
                self.assertTrue(policy["require_signed_commits"])
                self.assertIn("claude_command", policy)
            finally:
                INSTALLER.UPDATE_CONFIG = original_config
                INSTALLER.UPDATE_STATE = original_state

    def test_claude_plugin_update_reaches_exact_runtime_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(Path(directory), old_version="6.5.0", new_version="6.6.0")
            result = INSTALLER.update_claude_plugin("6.6.0", str(executable))
            self.assertTrue(result["attempted"])
            self.assertTrue(result["updated"], result)
            self.assertTrue(result["reload_required"])
            self.assertEqual(state.read_text(encoding="utf-8"), "6.6.0")
            self.assertEqual(result["status"]["version"], "6.6.0")

    def test_claude_plugin_update_failure_is_reported_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(
                Path(directory), old_version="6.5.0", new_version="6.6.0", fail_update=True
            )
            result = INSTALLER.update_claude_plugin("6.6.0", str(executable))
            self.assertTrue(result["attempted"])
            self.assertFalse(result["updated"])
            self.assertEqual(state.read_text(encoding="utf-8"), "6.5.0")
            self.assertFalse(result["status"]["healthy"])

    def test_current_claude_plugin_is_not_updated_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(Path(directory), old_version="6.6.0", new_version="broken")
            result = INSTALLER.update_claude_plugin("6.6.0", str(executable))
            self.assertFalse(result["attempted"])
            self.assertTrue(result["updated"])
            self.assertFalse(result["reload_required"])
            self.assertEqual(state.read_text(encoding="utf-8"), "6.6.0")

    def test_degraded_updater_state_is_due_immediately(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_STATE = root / "state.json"
            INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                "enabled": True,
                "interval_hours": 24,
                "require_signed_commits": False,
                "repository": INSTALLER.REPO_URL,
            })
            INSTALLER._atomic_json(INSTALLER.UPDATE_STATE, {
                "status": "degraded",
                "checked_at": int(INSTALLER.time.time()),
            })
            try:
                with mock.patch.object(INSTALLER, "update", return_value=7) as updater:
                    self.assertEqual(INSTALLER.auto_update("run"), 7)
                    updater.assert_called_once_with(False, None)
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_STATE = originals

    def test_missing_or_uninstalled_claude_plugin_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unavailable = INSTALLER.claude_plugin_status("6.6.0", str(root / "missing-claude"))
            self.assertEqual(unavailable["reason"], "claude-command-unavailable")
            executable = root / "claude"
            executable.write_text(
                "#!/usr/bin/env python3\nimport json\nprint(json.dumps([]))\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            result = INSTALLER.update_claude_plugin("6.6.0", str(executable))
            self.assertFalse(result["attempted"])
            self.assertEqual(result["status"]["reason"], "plugin-not-installed")

    def test_signing_key_is_created_once_with_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "guard" / "signing.key"
            INSTALLER.ensure_signing_key(key)
            first = key.read_bytes()
            INSTALLER.ensure_signing_key(key)
            self.assertEqual(key.read_bytes(), first)
            self.assertEqual(len(first), 32)
            if INSTALLER.os.name != "nt":
                self.assertEqual(key.stat().st_mode & 0o077, 0)

    def test_install_delivery_boundary_creates_command_policy_and_key(self) -> None:
        originals = (
            INSTALLER.DELIVERY_COMMAND,
            INSTALLER.DELIVERY_POLICY,
            INSTALLER.SIGNING_KEY,
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            INSTALLER.DELIVERY_COMMAND = temporary / "bin" / "blun-language-deliver"
            INSTALLER.DELIVERY_POLICY = temporary / "config" / "delivery-policy.json"
            INSTALLER.SIGNING_KEY = temporary / "config" / "signing.key"
            try:
                INSTALLER.install_delivery_boundary(ROOT)
                self.assertTrue(INSTALLER.DELIVERY_COMMAND.is_symlink())
                self.assertTrue(INSTALLER.SIGNING_KEY.is_file())
                policy = INSTALLER.json.loads(INSTALLER.DELIVERY_POLICY.read_text(encoding="utf-8"))
                self.assertTrue(policy["mandatory"])
                self.assertFalse(policy["direct_delivery_allowed"])
                self.assertTrue(policy["isolated_service"]["required"])
            finally:
                (
                    INSTALLER.DELIVERY_COMMAND,
                    INSTALLER.DELIVERY_POLICY,
                    INSTALLER.SIGNING_KEY,
                ) = originals

    def test_guard_runtime_installs_command_and_owner_only_token(self) -> None:
        originals = (
            INSTALLER.SERVICE_COMMAND,
            INSTALLER.SERVICE_TOKEN,
            INSTALLER.SIGNING_KEY,
            INSTALLER.AUDIT_LOG,
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            INSTALLER.SERVICE_COMMAND = temporary / "bin" / "blun-language-guard-service"
            INSTALLER.SERVICE_TOKEN = temporary / "config" / "service.token"
            INSTALLER.SIGNING_KEY = temporary / "config" / "signing.key"
            INSTALLER.AUDIT_LOG = temporary / "config" / "audit.jsonl"
            try:
                INSTALLER.install_guard_runtime(ROOT)
                token = INSTALLER.SERVICE_TOKEN.read_text(encoding="ascii").strip()
                self.assertTrue(INSTALLER.SERVICE_COMMAND.is_symlink())
                self.assertGreaterEqual(len(token), 32)
                if INSTALLER.os.name != "nt":
                    self.assertEqual(INSTALLER.SERVICE_TOKEN.stat().st_mode & 0o077, 0)
            finally:
                (
                    INSTALLER.SERVICE_COMMAND,
                    INSTALLER.SERVICE_TOKEN,
                    INSTALLER.SIGNING_KEY,
                    INSTALLER.AUDIT_LOG,
                ) = originals

    def test_mcp_http_runtime_installs_commands_and_owner_only_token(self) -> None:
        originals = (
            INSTALLER.MCP_HTTP_COMMAND,
            INSTALLER.MCP_HEADERS_COMMAND,
            INSTALLER.MCP_HTTP_TOKEN,
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            INSTALLER.MCP_HTTP_COMMAND = temporary / "bin" / "blun-language-guard-mcp"
            INSTALLER.MCP_HEADERS_COMMAND = temporary / "bin" / "blun-language-guard-mcp-headers"
            INSTALLER.MCP_HTTP_TOKEN = temporary / "config" / "mcp-http.token"
            try:
                INSTALLER.install_mcp_http_runtime(ROOT)
                self.assertTrue(INSTALLER.MCP_HTTP_COMMAND.is_symlink())
                self.assertTrue(INSTALLER.MCP_HEADERS_COMMAND.is_symlink())
                token = INSTALLER.MCP_HTTP_TOKEN.read_text(encoding="ascii").strip()
                self.assertGreaterEqual(len(token), 32)
                if os.name != "nt":
                    self.assertEqual(INSTALLER.MCP_HTTP_TOKEN.stat().st_mode & 0o077, 0)
            finally:
                (
                    INSTALLER.MCP_HTTP_COMMAND,
                    INSTALLER.MCP_HEADERS_COMMAND,
                    INSTALLER.MCP_HTTP_TOKEN,
                ) = originals

    def test_claude_configuration_uses_http_helper_and_removes_local_shadows(self) -> None:
        original_headers = INSTALLER.MCP_HEADERS_COMMAND
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".claude.json"
            INSTALLER.MCP_HEADERS_COMMAND = root / "bin" / "headers"
            config.write_text(INSTALLER.json.dumps({
                "theme": "dark",
                "mcpServers": {"another": {"type": "http", "url": "https://example.test/mcp"}},
                "projects": {
                    "/work/one": {
                        "mcpServers": {
                            "blun-language-guard": {
                                "type": "stdio",
                                "command": "python",
                                "args": ["old.py", "serve"],
                            },
                            "keep": {"type": "stdio", "command": "keep"},
                        }
                    }
                },
            }), encoding="utf-8")
            try:
                backup, removed = INSTALLER.configure_claude_mcp(config)
                self.assertEqual(removed, 1)
                self.assertIsNotNone(backup)
                assert backup is not None
                self.assertTrue(backup.is_file())
                result = INSTALLER.json.loads(config.read_text(encoding="utf-8"))
                self.assertEqual(result["theme"], "dark")
                self.assertIn("another", result["mcpServers"])
                self.assertEqual(
                    result["mcpServers"]["blun-language-guard"],
                    {
                        "type": "http",
                        "url": INSTALLER.MCP_HTTP_URL,
                        "headersHelper": str(INSTALLER.MCP_HEADERS_COMMAND),
                    },
                )
                self.assertNotIn(
                    "blun-language-guard",
                    result["projects"]["/work/one"]["mcpServers"],
                )
                self.assertIn("keep", result["projects"]["/work/one"]["mcpServers"])
                if os.name != "nt":
                    self.assertEqual(config.stat().st_mode & 0o077, 0)
            finally:
                INSTALLER.MCP_HEADERS_COMMAND = original_headers

    def test_claude_configuration_refuses_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".claude.json"
            config.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Refusing to modify"):
                INSTALLER.configure_claude_mcp(config)
            self.assertEqual(config.read_text(encoding="utf-8"), "{broken")

    def test_windows_task_requests_restart_on_failure(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
            result = INSTALLER._install_windows_restartable_task(
                "BLUN Language Guard MCP",
                ["C:\\Python\\python.exe", "C:\\BLUN\\mcp_http_gateway.py", "--port", "47632"],
            )
        self.assertEqual(result.returncode, 0)
        command = runner.call_args.args[0]
        self.assertEqual(command[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        script = command[4]
        self.assertIn("RestartCount 999", script)
        self.assertIn("RestartInterval", script)
        self.assertIn("MultipleInstances IgnoreNew", script)

    def test_project_mcp_shadow_is_detected_from_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "one" / "two"
            nested.mkdir(parents=True)
            (root / ".mcp.json").write_text(INSTALLER.json.dumps({
                "mcpServers": {
                    "blun-language-guard": {
                        "type": "stdio",
                        "command": "python",
                        "args": ["old.py", "serve"],
                    }
                }
            }), encoding="utf-8")
            self.assertEqual(INSTALLER.project_mcp_shadows(nested), [root / ".mcp.json"])


if __name__ == "__main__":
    unittest.main()
