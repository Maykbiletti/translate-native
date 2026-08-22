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
    def _update_repository_pair(self, root: Path) -> tuple[Path, Path, str, str]:
        upstream = root / "upstream"
        upstream.mkdir()
        self.assertEqual(INSTALLER._run(["git", "init", "-b", "main"], upstream).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "config", "user.name", "Update Test"], upstream).returncode, 0)
        self.assertEqual(
            INSTALLER._run(["git", "config", "user.email", "update@example.invalid"], upstream).returncode,
            0,
        )
        (upstream / "VERSION").write_text("6.25.0\n", encoding="utf-8")
        (upstream / "translate-native").mkdir()
        tests = upstream / "tests"
        tests.mkdir()
        (tests / "test_smoke.py").write_text(
            "import unittest\n\nclass SmokeTest(unittest.TestCase):\n"
            "    def test_true(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.assertEqual(INSTALLER._run(["git", "add", "."], upstream).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "commit", "-m", "old"], upstream).returncode, 0)
        old = INSTALLER._run(["git", "rev-parse", "HEAD"], upstream).stdout.strip()
        active = root / "active"
        self.assertEqual(INSTALLER._run(["git", "clone", str(upstream), str(active)]).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "config", "user.name", "Update Test"], active).returncode, 0)
        self.assertEqual(
            INSTALLER._run(["git", "config", "user.email", "update@example.invalid"], active).returncode,
            0,
        )
        (upstream / "VERSION").write_text("6.26.0\n", encoding="utf-8")
        (upstream / "new-runtime.txt").write_text("new\n", encoding="utf-8")
        self.assertEqual(INSTALLER._run(["git", "add", "."], upstream).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "commit", "-m", "candidate"], upstream).returncode, 0)
        new = INSTALLER._run(["git", "rev-parse", "HEAD"], upstream).stdout.strip()
        return upstream, active, old, new

    def _add_candidate_import_marker(self, upstream: Path, marker: Path) -> None:
        (upstream / "tests" / "test_smoke.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
            "import unittest\n\nclass SmokeTest(unittest.TestCase):\n"
            "    def test_true(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.assertEqual(INSTALLER._run(["git", "add", "tests/test_smoke.py"], upstream).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "commit", "-m", "candidate marker"], upstream).returncode, 0)

    def _rollback_repository(
        self, root: Path, *, broken_target: bool = False, target_test_marker: Path | None = None,
    ) -> tuple[Path, str, str]:
        repository = root / "repo"
        repository.mkdir()
        self.assertEqual(INSTALLER._run(["git", "init", "-b", "main"], repository).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "config", "user.name", "Rollback Test"], repository).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "config", "user.email", "rollback@example.invalid"], repository).returncode, 0)
        (repository / "VERSION").write_text("6.8.0\n", encoding="utf-8")
        tests = repository / "tests"
        tests.mkdir()
        marker_setup = (
            "from pathlib import Path\n"
            f"Path({str(target_test_marker)!r}).write_text('executed', encoding='utf-8')\n"
            if target_test_marker is not None else ""
        )
        (tests / "test_smoke.py").write_text(
            marker_setup + "import unittest\n\nclass SmokeTest(unittest.TestCase):\n"
            "    def test_true(self):\n        self.assertTrue(" + ("False" if broken_target else "True") + ")\n",
            encoding="utf-8",
        )
        self.assertEqual(INSTALLER._run(["git", "add", "."], repository).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "commit", "-m", "old"], repository).returncode, 0)
        old = INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip()
        (repository / "VERSION").write_text("6.9.0\n", encoding="utf-8")
        (repository / "new-runtime.txt").write_text("new\n", encoding="utf-8")
        if broken_target:
            (tests / "test_smoke.py").write_text(
                "import unittest\n\nclass SmokeTest(unittest.TestCase):\n"
                "    def test_true(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
        self.assertEqual(INSTALLER._run(["git", "add", "."], repository).returncode, 0)
        self.assertEqual(INSTALLER._run(["git", "commit", "-m", "new"], repository).returncode, 0)
        current = INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip()
        return repository, old, current

    def _fake_claude(
        self, root: Path, *, old_version: str = "6.7.1", new_version: str = "6.8.0",
        advertised_version: str | None = None, fail_marketplace_update: bool = False,
        fail_update: bool = False, fail_validation: bool = False,
    ) -> tuple[Path, Path]:
        state = root / "plugin-version.txt"
        state.write_text(old_version, encoding="utf-8")
        marketplace_ready = root / "marketplace-ready"
        calls = root / "claude-calls.jsonl"
        advertised_version = new_version if advertised_version is None else advertised_version
        executable = root / "claude"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"state = pathlib.Path({str(state)!r})\n"
            f"calls = pathlib.Path({str(calls)!r})\n"
            "args = sys.argv[1:]\n"
            "with calls.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(args) + '\\n')\n"
            "if args == ['plugin', 'list', '--json']:\n"
            "    print(json.dumps([{'name': 'translate-native', 'marketplace': 'blun-language-tools', "
            "'version': state.read_text().strip(), 'enabled': True, 'errors': []}]))\n"
            "    raise SystemExit(0)\n"
            "if len(args) == 4 and args[:2] == ['plugin', 'validate'] and args[-1] == '--strict':\n"
            + ("    raise SystemExit(1)\n" if fail_validation else "    raise SystemExit(0)\n")
            + "if args == ['plugin', 'marketplace', 'update', 'blun-language-tools']:\n"
            + ("    raise SystemExit(1)\n" if fail_marketplace_update else f"    pathlib.Path({str(marketplace_ready)!r}).write_text('ready')\n    raise SystemExit(0)\n")
            + "if args == ['plugin', 'list', '--available', '--json']:\n"
            f"    if not pathlib.Path({str(marketplace_ready)!r}).exists(): raise SystemExit(3)\n"
            "    print(json.dumps([{'name': 'translate-native', 'marketplace': 'blun-language-tools', "
            f"'version': {advertised_version!r}}}]))\n"
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

    def test_update_refuses_tracked_and_untracked_changes_before_candidate_execution(self) -> None:
        for change_kind in ("tracked", "untracked"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                upstream, active, old, _candidate = self._update_repository_pair(root)
                marker = root / "candidate-test-executed"
                self._add_candidate_import_marker(upstream, marker)
                changed = active / ("VERSION" if change_kind == "tracked" else "local-note.txt")
                changed.write_text("local work\n", encoding="utf-8")
                with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                     mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                     mock.patch.object(
                         INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}
                     ), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER._update_unlocked(), 2)
                self.assertFalse(marker.exists())
                self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip(), old)
                self.assertEqual(changed.read_text(encoding="utf-8"), "local work\n")
                self.assertFalse((active / "new-runtime.txt").exists())

    def test_update_rechecks_dirty_or_moved_checkout_after_candidate_preflight(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                upstream, active, old, candidate = self._update_repository_pair(root)
                claude_target = root / "claude-skill"
                claude_target.symlink_to(active / "translate-native", target_is_directory=True)
                local_file = active / "parallel-work.txt"

                def mutate_checkout(*_args: object, **_kwargs: object) -> dict:
                    local_file.write_text("parallel work\n", encoding="utf-8")
                    if change_kind == "new-head":
                        self.assertEqual(INSTALLER._run(["git", "add", "parallel-work.txt"], active).returncode, 0)
                        self.assertEqual(INSTALLER._run(["git", "commit", "-m", "parallel work"], active).returncode, 0)
                    return {"ready": True}

                with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                     mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                     mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": claude_target}), \
                     mock.patch.object(INSTALLER, "preflight_claude_plugin_update", side_effect=mutate_checkout), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER._update_unlocked(), 2)
                current = INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip()
                self.assertEqual(current == old, change_kind == "dirty")
                self.assertNotEqual(current, candidate)
                self.assertEqual(local_file.read_text(encoding="utf-8"), "parallel work\n")
                self.assertFalse((active / "new-runtime.txt").exists())

    def test_update_rechecks_checkout_after_fetch_before_fast_forward(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                upstream, active, old, candidate = self._update_repository_pair(root)
                local_file = active / "during-fetch.txt"
                real_run = INSTALLER._run

                def mutate_after_fetch(command: list[str], cwd: Path | None = None):
                    result = real_run(command, cwd)
                    if command[:2] == ["git", "fetch"] and cwd == active and result.returncode == 0:
                        local_file.write_text("operator work during fetch\n", encoding="utf-8")
                        if change_kind == "new-head":
                            self.assertEqual(real_run(["git", "add", local_file.name], active).returncode, 0)
                            self.assertEqual(real_run(["git", "commit", "-m", "operator work"], active).returncode, 0)
                    return result

                errors = io.StringIO()
                with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                     mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                     mock.patch.object(
                         INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}
                     ), \
                     mock.patch.object(INSTALLER, "_run", side_effect=mutate_after_fetch), \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(INSTALLER._update_unlocked(), 2)
                self.assertIn("changed while fetching", errors.getvalue())
                current = real_run(["git", "rev-parse", "HEAD"], active).stdout.strip()
                self.assertEqual(current == old, change_kind == "dirty")
                self.assertNotEqual(current, candidate)
                self.assertEqual(local_file.read_text(encoding="utf-8"), "operator work during fetch\n")
                self.assertFalse((active / "new-runtime.txt").exists())

    def test_update_cutover_guard_preserves_uncommitted_and_committed_work(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                upstream, active, old, candidate = self._update_repository_pair(root)
                local_file = active / "during-cutover.txt"
                real_run = INSTALLER._run

                def mutate_after_merge(command: list[str], cwd: Path | None = None):
                    result = real_run(command, cwd)
                    if command[:2] == ["git", "merge"] and cwd == active and result.returncode == 0:
                        local_file.write_text("operator work during cutover\n", encoding="utf-8")
                        if change_kind == "new-head":
                            self.assertEqual(real_run(["git", "add", local_file.name], active).returncode, 0)
                            self.assertEqual(real_run(["git", "commit", "-m", "operator work"], active).returncode, 0)
                    return result

                errors = io.StringIO()
                with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                     mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                     mock.patch.object(
                         INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}
                     ), \
                     mock.patch.object(INSTALLER, "_run", side_effect=mutate_after_merge), \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(INSTALLER._update_unlocked(), 2)
                current = real_run(["git", "rev-parse", "HEAD"], active).stdout.strip()
                self.assertEqual(current == old, change_kind == "dirty")
                if change_kind == "new-head":
                    self.assertIn("HEAD changed independently", errors.getvalue())
                    self.assertEqual(real_run(["git", "merge-base", "--is-ancestor", candidate, current], active).returncode, 0)
                else:
                    self.assertIn("rolled back without discarding local work", errors.getvalue())
                    self.assertFalse((active / "new-runtime.txt").exists())
                self.assertEqual(local_file.read_text(encoding="utf-8"), "operator work during cutover\n")

    def test_update_post_tests_guard_preserves_uncommitted_and_committed_work(self) -> None:
        for test_result in ("pass", "fail"):
            for change_kind in ("dirty", "new-head"):
                with self.subTest(test_result=test_result, change_kind=change_kind), \
                     tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    upstream, active, old, candidate = self._update_repository_pair(root)
                    local_file = active / "during-post-update-tests.txt"
                    service = root / "installed-service"
                    mcp = root / "installed-mcp"
                    service.write_text("installed\n", encoding="utf-8")
                    mcp.write_text("installed\n", encoding="utf-8")
                    real_run = INSTALLER._run
                    mutated = False

                    def mutate_after_post_tests(command: list[str], cwd: Path | None = None):
                        nonlocal mutated
                        result = real_run(command, cwd)
                        if (
                            not mutated
                            and command[:4] == [sys.executable, "-B", "-m", "unittest"]
                            and cwd == active
                        ):
                            mutated = True
                            local_file.write_text(
                                "operator work during post-update tests\n",
                                encoding="utf-8",
                            )
                            if change_kind == "new-head":
                                self.assertEqual(
                                    real_run(["git", "add", local_file.name], active).returncode,
                                    0,
                                )
                                self.assertEqual(
                                    real_run(["git", "commit", "-m", "operator work"], active).returncode,
                                    0,
                                )
                            if test_result == "fail":
                                return INSTALLER.subprocess.CompletedProcess(
                                    command, 1, result.stdout, "injected post-test failure"
                                )
                        return result

                    errors = io.StringIO()
                    with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                         mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                         mock.patch.object(
                             INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}
                         ), \
                         mock.patch.object(INSTALLER, "SERVICE_COMMAND", service), \
                         mock.patch.object(INSTALLER, "MCP_HTTP_COMMAND", mcp), \
                         mock.patch.object(INSTALLER, "_run", side_effect=mutate_after_post_tests), \
                         mock.patch.object(INSTALLER, "restart_guard_runtime") as guard_restart, \
                         mock.patch.object(INSTALLER, "restart_mcp_http_runtime") as mcp_restart, \
                         contextlib.redirect_stderr(errors):
                        expected = 1 if test_result == "fail" else 2
                        self.assertEqual(INSTALLER._update_unlocked(), expected)
                    observed = real_run(["git", "rev-parse", "HEAD"], active).stdout.strip()
                    self.assertEqual(observed == old, change_kind == "dirty")
                    if change_kind == "new-head":
                        self.assertIn("independent commit was not reset", errors.getvalue())
                        self.assertEqual(
                            real_run(
                                ["git", "merge-base", "--is-ancestor", candidate, observed],
                                active,
                            ).returncode,
                            0,
                        )
                    else:
                        self.assertIn("without discarding local work", errors.getvalue())
                        self.assertFalse((active / "new-runtime.txt").exists())
                    self.assertEqual(
                        local_file.read_text(encoding="utf-8"),
                        "operator work during post-update tests\n",
                    )
                    guard_restart.assert_not_called()
                    mcp_restart.assert_not_called()

    def test_rollback_tests_target_pauses_updater_and_records_exact_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root)
            state_path = root / "update-state.json"
            config_path = root / "updater.json"
            paused_config_path = root / "updater.rollback-paused.json"
            INSTALLER._atomic_json(state_path, {
                "status": "ok", "revision": current, "previous": target, "checked_at": 1,
            })
            INSTALLER._atomic_json(config_path, {
                "enabled": True, "interval_hours": 1, "require_signed_commits": False,
            })
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", config_path), \
                 mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", paused_config_path), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 mock.patch.object(INSTALLER, "SERVICE_COMMAND", root / "missing-service"), \
                 mock.patch.object(INSTALLER, "MCP_HTTP_COMMAND", root / "missing-mcp"), \
                 mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                 mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(), 0)
                self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), target)
                self.assertFalse((repository / "new-runtime.txt").exists())
                state = INSTALLER.json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "rolled_back")
                self.assertEqual(state["revision"], target)
                self.assertEqual(state["rolled_back_from"], current)
                self.assertTrue(state["auto_update_paused"])
                self.assertFalse(config_path.exists())
                self.assertTrue(paused_config_path.exists())
                scheduler_removal.assert_called_once_with()
                with mock.patch.object(INSTALLER, "update") as updater:
                    self.assertEqual(INSTALLER.auto_update("run"), 0)
                    updater.assert_not_called()

    def test_rollback_refuses_dirty_worktree_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root)
            state_path = root / "update-state.json"
            INSTALLER._atomic_json(state_path, {"status": "ok", "revision": current, "previous": target})
            (repository / "VERSION").write_text("dirty\n", encoding="utf-8")
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(), 2)
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), current)
            self.assertEqual((repository / "VERSION").read_text(encoding="utf-8"), "dirty\n")

    def test_rollback_refuses_stale_state_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root)
            state_path = root / "update-state.json"
            INSTALLER._atomic_json(state_path, {"status": "ok", "revision": target, "previous": current})
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(), 2)
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), current)

    def test_rollback_refuses_mismatched_claude_plugin_before_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root)
            state_path = root / "update-state.json"
            INSTALLER._atomic_json(state_path, {"status": "ok", "revision": current, "previous": target})
            skill = root / "skill"
            skill.mkdir()
            claude_target = root / "claude-skill"
            claude_target.symlink_to(skill, target_is_directory=True)
            mismatch = {"installed": True, "healthy": False, "version": "6.9.0", "expected_version": "6.8.0"}
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": claude_target}), \
                 mock.patch.object(INSTALLER, "claude_plugin_status", return_value=mismatch) as plugin_check, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(), 1)
            plugin_check.assert_called_once()
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), current)

    def test_rollback_refuses_failing_target_before_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root, broken_target=True)
            state_path = root / "update-state.json"
            INSTALLER._atomic_json(state_path, {"status": "ok", "revision": current, "previous": target})
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(), 1)
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), current)

    def test_rollback_rechecks_dirty_or_moved_checkout_after_candidate_preflight(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, target, current = self._rollback_repository(root)
                state_path = root / "update-state.json"
                INSTALLER._atomic_json(
                    state_path, {"status": "ok", "revision": current, "previous": target}
                )
                local_file = repository / "during-rollback-preflight.txt"
                real_run = INSTALLER._run
                mutated = False

                def mutate_after_candidate_tests(command: list[str], cwd: Path | None = None):
                    nonlocal mutated
                    result = real_run(command, cwd)
                    if (
                        not mutated
                        and command[:3] == [sys.executable, "-m", "unittest"]
                        and cwd != repository
                        and result.returncode == 0
                    ):
                        mutated = True
                        local_file.write_text("operator work during rollback preflight\n", encoding="utf-8")
                        if change_kind == "new-head":
                            self.assertEqual(real_run(["git", "add", local_file.name], repository).returncode, 0)
                            self.assertEqual(real_run(["git", "commit", "-m", "operator work"], repository).returncode, 0)
                    return result

                errors = io.StringIO()
                with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                     mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                     mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                     mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                     mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                     mock.patch.object(INSTALLER, "_run", side_effect=mutate_after_candidate_tests), \
                     mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(INSTALLER.rollback(), 2)
                self.assertIn("changed during rollback preflight", errors.getvalue())
                observed = real_run(["git", "rev-parse", "HEAD"], repository).stdout.strip()
                self.assertEqual(observed == current, change_kind == "dirty")
                self.assertNotEqual(observed, target)
                self.assertEqual(
                    local_file.read_text(encoding="utf-8"),
                    "operator work during rollback preflight\n",
                )
                self.assertTrue((repository / "new-runtime.txt").exists())
                scheduler_removal.assert_not_called()

    def test_rollback_cutover_guard_preserves_uncommitted_and_committed_work(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, target, current = self._rollback_repository(root)
                state_path = root / "update-state.json"
                INSTALLER._atomic_json(
                    state_path, {"status": "ok", "revision": current, "previous": target}
                )
                local_file = repository / "during-rollback-cutover.txt"
                real_run = INSTALLER._run
                mutated = False

                def mutate_after_reset(command: list[str], cwd: Path | None = None):
                    nonlocal mutated
                    result = real_run(command, cwd)
                    if (
                        not mutated
                        and command == ["git", "reset", "--keep", target]
                        and cwd == repository
                        and result.returncode == 0
                    ):
                        mutated = True
                        local_file.write_text("operator work during rollback cutover\n", encoding="utf-8")
                        if change_kind == "new-head":
                            self.assertEqual(real_run(["git", "add", local_file.name], repository).returncode, 0)
                            self.assertEqual(real_run(["git", "commit", "-m", "operator work"], repository).returncode, 0)
                    return result

                errors = io.StringIO()
                with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                     mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                     mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                     mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                     mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                     mock.patch.object(INSTALLER, "_run", side_effect=mutate_after_reset), \
                     mock.patch.object(INSTALLER, "_restart_installed_runtimes") as restarter, \
                     mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(INSTALLER.rollback(), 2)
                observed = real_run(["git", "rev-parse", "HEAD"], repository).stdout.strip()
                self.assertEqual(observed == current, change_kind == "dirty")
                if change_kind == "new-head":
                    self.assertIn("HEAD changed independently", errors.getvalue())
                    self.assertEqual(
                        real_run(["git", "merge-base", "--is-ancestor", target, observed], repository).returncode,
                        0,
                    )
                else:
                    self.assertIn("restored without discarding local work", errors.getvalue())
                    self.assertTrue((repository / "new-runtime.txt").exists())
                self.assertEqual(
                    local_file.read_text(encoding="utf-8"),
                    "operator work during rollback cutover\n",
                )
                restarter.assert_not_called()
                scheduler_removal.assert_not_called()

    def test_rollback_post_tests_guard_preserves_uncommitted_and_committed_work(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, target, current = self._rollback_repository(root)
                state_path = root / "update-state.json"
                INSTALLER._atomic_json(
                    state_path, {"status": "ok", "revision": current, "previous": target}
                )
                local_file = repository / "during-post-rollback-tests.txt"
                real_run = INSTALLER._run
                mutated = False

                def mutate_after_post_tests(command: list[str], cwd: Path | None = None):
                    nonlocal mutated
                    result = real_run(command, cwd)
                    if (
                        not mutated
                        and command[:4] == [sys.executable, "-B", "-m", "unittest"]
                        and cwd == repository
                        and result.returncode == 0
                    ):
                        mutated = True
                        local_file.write_text("operator work during post-rollback tests\n", encoding="utf-8")
                        if change_kind == "new-head":
                            self.assertEqual(real_run(["git", "add", local_file.name], repository).returncode, 0)
                            self.assertEqual(real_run(["git", "commit", "-m", "operator work"], repository).returncode, 0)
                    return result

                errors = io.StringIO()
                with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                     mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                     mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                     mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                     mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                     mock.patch.object(INSTALLER, "_run", side_effect=mutate_after_post_tests), \
                     mock.patch.object(INSTALLER, "_restart_installed_runtimes") as restarter, \
                     mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(INSTALLER.rollback(), 2)
                self.assertIn("while running post-rollback tests", errors.getvalue())
                observed = real_run(["git", "rev-parse", "HEAD"], repository).stdout.strip()
                self.assertEqual(observed == current, change_kind == "dirty")
                if change_kind == "new-head":
                    self.assertIn("HEAD changed independently", errors.getvalue())
                    self.assertEqual(
                        real_run(["git", "merge-base", "--is-ancestor", target, observed], repository).returncode,
                        0,
                    )
                else:
                    self.assertIn("restored without discarding local work", errors.getvalue())
                    self.assertTrue((repository / "new-runtime.txt").exists())
                self.assertEqual(
                    local_file.read_text(encoding="utf-8"),
                    "operator work during post-rollback tests\n",
                )
                restarter.assert_not_called()
                scheduler_removal.assert_not_called()

    def test_rollback_runtime_guard_preserves_uncommitted_and_committed_work(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, target, current = self._rollback_repository(root)
                state_path = root / "update-state.json"
                INSTALLER._atomic_json(
                    state_path, {"status": "ok", "revision": current, "previous": target}
                )
                local_file = repository / "during-rollback-runtime-verification.txt"
                real_run = INSTALLER._run
                restart_calls = 0

                def mutate_during_runtime_restart() -> tuple[bool, str]:
                    nonlocal restart_calls
                    restart_calls += 1
                    if restart_calls == 1:
                        local_file.write_text(
                            "operator work during rollback runtime verification\n",
                            encoding="utf-8",
                        )
                        if change_kind == "new-head":
                            self.assertEqual(real_run(["git", "add", local_file.name], repository).returncode, 0)
                            self.assertEqual(real_run(["git", "commit", "-m", "operator work"], repository).returncode, 0)
                    return True, "runtime healthy"

                errors = io.StringIO()
                with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                     mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                     mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                     mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                     mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                     mock.patch.object(
                         INSTALLER, "_restart_installed_runtimes", side_effect=mutate_during_runtime_restart
                     ) as restarter, \
                     mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(INSTALLER.rollback(), 2)
                self.assertIn("during rollback runtime verification", errors.getvalue())
                observed = real_run(["git", "rev-parse", "HEAD"], repository).stdout.strip()
                self.assertEqual(observed == current, change_kind == "dirty")
                if change_kind == "new-head":
                    self.assertIn("HEAD changed independently", errors.getvalue())
                    self.assertEqual(restarter.call_count, 1)
                    self.assertEqual(
                        real_run(["git", "merge-base", "--is-ancestor", target, observed], repository).returncode,
                        0,
                    )
                else:
                    self.assertIn("revision and runtimes were restored", errors.getvalue())
                    self.assertEqual(restarter.call_count, 2)
                    self.assertTrue((repository / "new-runtime.txt").exists())
                self.assertEqual(
                    local_file.read_text(encoding="utf-8"),
                    "operator work during rollback runtime verification\n",
                )
                scheduler_removal.assert_not_called()

    def test_rollback_preserves_saved_signed_commit_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root)
            state_path = root / "update-state.json"
            config_path = root / "updater.json"
            INSTALLER._atomic_json(state_path, {"status": "ok", "revision": current, "previous": target})
            INSTALLER._atomic_json(config_path, {"require_signed_commits": True})
            real_run = INSTALLER._run

            def reject_signature(command: list[str], cwd: Path | None = None):
                if command[:2] == ["git", "verify-commit"]:
                    return INSTALLER.subprocess.CompletedProcess(command, 1, "", "untrusted")
                return real_run(command, cwd)

            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", config_path), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 mock.patch.object(INSTALLER, "_run", side_effect=reject_signature) as runner, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(), 1)
            self.assertTrue(any(call.args[0][:2] == ["git", "verify-commit"] for call in runner.call_args_list))
            self.assertEqual(real_run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), current)

    def test_rollback_verifies_required_signature_before_executing_target_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "rollback-test-executed"
            repository, target, current = self._rollback_repository(root, target_test_marker=marker)
            state_path = root / "update-state.json"
            INSTALLER._atomic_json(state_path, {"status": "ok", "revision": current, "previous": target})
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(require_signed_commits=True), 1)
            self.assertFalse(marker.exists())
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), current)

    def test_rollback_runtime_failure_restores_forward_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root)
            state_path = root / "update-state.json"
            INSTALLER._atomic_json(state_path, {"status": "ok", "revision": current, "previous": target})
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                 mock.patch.object(
                     INSTALLER, "_restart_installed_runtimes",
                     side_effect=[(False, "probe failed"), (True, "forward restored")],
                 ) as restarter, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.rollback(), 1)
            self.assertEqual(restarter.call_count, 2)
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(), current)
            self.assertEqual(INSTALLER.json.loads(state_path.read_text(encoding="utf-8"))["status"], "ok")

    def test_auto_update_policy_can_be_enabled_without_scheduler(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE)
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.UPDATE_CONFIG = Path(directory) / "updater.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = Path(directory) / "updater.rollback-paused.json"
            INSTALLER.UPDATE_STATE = Path(directory) / "state.json"
            try:
                self.assertEqual(0, INSTALLER.auto_update("enable", 12, True, scheduler=False))
                policy = INSTALLER.json.loads(INSTALLER.UPDATE_CONFIG.read_text(encoding="utf-8"))
                self.assertEqual(policy["interval_hours"], 12)
                self.assertTrue(policy["require_signed_commits"])
                self.assertIn("claude_command", policy)
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE = originals

    def test_atomic_json_does_not_follow_predictable_temporary_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "updater.json"
            sentinel = root / "sentinel.txt"
            sentinel.write_text("do not replace\n", encoding="utf-8")
            legacy_temporary = policy.with_suffix(policy.suffix + ".tmp")
            try:
                legacy_temporary.symlink_to(sentinel)
            except OSError as error:
                self.skipTest(f"symbolic links are unavailable: {error}")

            INSTALLER._atomic_json(policy, {"enabled": True})

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not replace\n")
            self.assertTrue(legacy_temporary.is_symlink())
            self.assertFalse(policy.is_symlink())
            self.assertEqual(INSTALLER.json.loads(policy.read_text(encoding="utf-8")), {"enabled": True})
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o600)

    def test_update_policy_loader_rejects_links_special_files_size_and_invalid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text('{"enabled": true, "require_signed_commits": true}\n', encoding="utf-8")
            linked = root / "linked.json"
            try:
                linked.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links are unavailable: {error}")
            with self.assertRaisesRegex(RuntimeError, "Unsafe updater policy file type"):
                INSTALLER._load_update_policy(linked)

            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (INSTALLER.MAX_UPDATE_POLICY_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, "exceeds size limit"):
                INSTALLER._load_update_policy(oversized)

            invalid_values = (
                {"enabled": "true"},
                {"require_signed_commits": 1},
                {"interval_hours": True},
                {"interval_hours": 0},
                {"repository": []},
                {"claude_command": 7},
            )
            invalid = root / "invalid.json"
            for payload in invalid_values:
                with self.subTest(payload=payload):
                    invalid.write_text(INSTALLER.json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "Invalid updater policy field"):
                        INSTALLER._load_update_policy(invalid)

            if os.name != "nt" and hasattr(os, "mkfifo"):
                fifo = root / "policy.fifo"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(RuntimeError, "Unsafe updater policy file type"):
                    INSTALLER._load_update_policy(fifo)

    def test_auto_update_status_and_run_reject_linked_policy_without_worker_call(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            INSTALLER._atomic_json(target, {
                "enabled": True,
                "interval_hours": 1,
                "require_signed_commits": True,
            })
            linked = root / "updater.json"
            try:
                linked.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links are unavailable: {error}")
            INSTALLER.UPDATE_CONFIG = linked
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "missing-paused.json"
            INSTALLER.UPDATE_STATE = root / "missing-state.json"
            try:
                with mock.patch.object(INSTALLER, "update") as updater, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("run"), 2)
                    self.assertEqual(INSTALLER.auto_update("status"), 2)
                updater.assert_not_called()
                self.assertTrue(linked.is_symlink())
                self.assertTrue(INSTALLER.json.loads(target.read_text(encoding="utf-8"))["require_signed_commits"])
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE = originals

    def test_reenable_cannot_downgrade_signed_policy_without_explicit_disable(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "updater.rollback-paused.json"
            INSTALLER.UPDATE_STATE = root / "state.json"
            try:
                INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                    "enabled": True, "interval_hours": 24, "require_signed_commits": True,
                })
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("enable", 12, False, scheduler=False), 0)
                active = INSTALLER.json.loads(INSTALLER.UPDATE_CONFIG.read_text(encoding="utf-8"))
                self.assertEqual(active["interval_hours"], 12)
                self.assertTrue(active["require_signed_commits"])

                INSTALLER.UPDATE_CONFIG.unlink()
                INSTALLER._atomic_json(INSTALLER.UPDATE_PAUSED_CONFIG, {"require_signed_commits": True})
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("enable", 8, False, scheduler=False), 0)
                restored = INSTALLER.json.loads(INSTALLER.UPDATE_CONFIG.read_text(encoding="utf-8"))
                self.assertEqual(restored["interval_hours"], 8)
                self.assertTrue(restored["require_signed_commits"])
                self.assertFalse(INSTALLER.UPDATE_PAUSED_CONFIG.exists())

                with mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("disable"), 0)
                scheduler_removal.assert_called_once_with()
                self.assertFalse(INSTALLER.UPDATE_CONFIG.exists())
                self.assertFalse(INSTALLER.UPDATE_PAUSED_CONFIG.exists())
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("enable", 6, False, scheduler=False), 0)
                reset = INSTALLER.json.loads(INSTALLER.UPDATE_CONFIG.read_text(encoding="utf-8"))
                self.assertFalse(reset["require_signed_commits"])
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE = originals

    def test_reenable_refuses_to_replace_invalid_saved_policy(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "updater.rollback-paused.json"
            INSTALLER.UPDATE_STATE = root / "state.json"
            original = b'{"require_signed_commits": "false"}\n'
            INSTALLER.UPDATE_CONFIG.write_bytes(original)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("enable", 12, False, scheduler=False), 2)
                self.assertEqual(INSTALLER.UPDATE_CONFIG.read_bytes(), original)
                self.assertFalse(INSTALLER.UPDATE_PAUSED_CONFIG.exists())
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG, INSTALLER.UPDATE_STATE = originals

    def test_claude_plugin_update_reaches_exact_runtime_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(Path(directory), old_version="6.7.1", new_version="6.8.0")
            result = INSTALLER.update_claude_plugin("6.8.0", str(executable))
            self.assertTrue(result["attempted"])
            self.assertTrue(result["updated"], result)
            self.assertTrue(result["reload_required"])
            self.assertEqual(state.read_text(encoding="utf-8"), "6.8.0")
            self.assertEqual(result["status"]["version"], "6.8.0")
            self.assertTrue(result["validation"]["healthy"])
            self.assertTrue(result["catalog"]["healthy"])
            calls = [
                INSTALLER.json.loads(line)
                for line in (Path(directory) / "claude-calls.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(calls[1][:2], ["plugin", "validate"])
            self.assertEqual(calls[1][-1], "--strict")
            self.assertEqual(calls[2], ["plugin", "marketplace", "update", "blun-language-tools"])

    def test_claude_plugin_preflight_does_not_mutate_the_installed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable, state = self._fake_claude(root, old_version="6.25.0", new_version="6.26.0")
            result = INSTALLER.preflight_claude_plugin_update("6.26.0", str(executable), ROOT)
            self.assertTrue(result["ready"], result)
            self.assertTrue(result["needs_update"])
            self.assertEqual(state.read_text(encoding="utf-8"), "6.25.0")
            calls = [
                INSTALLER.json.loads(line)
                for line in (root / "claude-calls.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(calls[0], ["plugin", "list", "--json"])
            self.assertEqual(calls[1][:2], ["plugin", "validate"])
            self.assertEqual(calls[2], ["plugin", "marketplace", "update", "blun-language-tools"])
            self.assertEqual(calls[3], ["plugin", "list", "--available", "--json"])
            self.assertFalse(any(call[:2] == ["plugin", "update"] for call in calls))

    def test_update_verifies_required_signature_before_executing_candidate_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, active, old, _candidate = self._update_repository_pair(root)
            marker = root / "candidate-test-executed"
            (upstream / "tests" / "test_smoke.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "import unittest\n\nclass SmokeTest(unittest.TestCase):\n"
                "    def test_true(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            self.assertEqual(INSTALLER._run(["git", "add", "tests/test_smoke.py"], upstream).returncode, 0)
            self.assertEqual(INSTALLER._run(["git", "commit", "-m", "untrusted candidate"], upstream).returncode, 0)
            with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                 mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                 mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER._update_unlocked(require_signed_commits=True), 1)
            self.assertFalse(marker.exists())
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip(), old)
            self.assertFalse((active / "new-runtime.txt").exists())

    def test_direct_update_cannot_downgrade_saved_signed_commit_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, active, old, _candidate = self._update_repository_pair(root)
            marker = root / "candidate-test-executed"
            (upstream / "tests" / "test_smoke.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "import unittest\n\nclass SmokeTest(unittest.TestCase):\n"
                "    def test_true(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            self.assertEqual(INSTALLER._run(["git", "add", "tests/test_smoke.py"], upstream).returncode, 0)
            self.assertEqual(INSTALLER._run(["git", "commit", "-m", "untrusted candidate"], upstream).returncode, 0)
            config_path = root / "updater.json"
            INSTALLER._atomic_json(config_path, {"require_signed_commits": True})
            with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                 mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", config_path), \
                 mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", root / "missing-paused-policy"), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": root / "missing-claude"}), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.update(), 1)
            self.assertFalse(marker.exists())
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip(), old)

    def test_paused_or_invalid_policy_cannot_be_downgraded_by_direct_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _target, _current = self._rollback_repository(root)
            paused_path = root / "updater.rollback-paused.json"
            INSTALLER._atomic_json(paused_path, {"require_signed_commits": True})
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                 mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", paused_path), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 mock.patch.object(INSTALLER, "_update_unlocked", return_value=1) as updater, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER.update(), 1)
            updater.assert_called_once_with(True, None)

            invalid_policies = (
                b'{"require_signed_commits": "false"}\n',
                b'{"require_signed_commits":',
                b'[]\n',
                b'\xff\xfe',
            )
            for payload in invalid_policies:
                with self.subTest(policy=payload):
                    paused_path.write_bytes(payload)
                    with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                         mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "missing-policy"), \
                         mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", paused_path), \
                         mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                         mock.patch.object(INSTALLER, "_update_unlocked") as updater, \
                         contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(INSTALLER.update(), 2)
                    updater.assert_not_called()

    def test_failed_candidate_preflight_preserves_repository_and_runtimes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, active, old, candidate = self._update_repository_pair(root)
            fake_root = root / "fake-claude"
            fake_root.mkdir()
            executable, state = self._fake_claude(
                fake_root,
                old_version="6.25.0",
                new_version="6.26.0",
                fail_validation=True,
            )
            claude_skill = root / "claude-skill"
            claude_skill.symlink_to(active / "translate-native", target_is_directory=True)
            update_state = root / "update-state.json"
            with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                 mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", update_state), \
                 mock.patch.object(
                     INSTALLER,
                     "TARGETS",
                     {**INSTALLER.TARGETS, "claude": claude_skill},
                 ), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(INSTALLER._update_unlocked(claude_command=str(executable)), 1)
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip(), old)
            self.assertFalse((active / "new-runtime.txt").exists())
            self.assertEqual(state.read_text(encoding="utf-8"), "6.25.0")
            recorded = INSTALLER.json.loads(update_state.read_text(encoding="utf-8"))
            self.assertEqual(recorded["status"], "degraded")
            self.assertEqual(recorded["revision"], old)
            self.assertEqual(recorded["candidate_revision"], candidate)
            self.assertTrue(recorded["runtime_unchanged"])
            calls = [
                INSTALLER.json.loads(line)
                for line in (fake_root / "claude-calls.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(calls), 2)
            self.assertFalse(any(call[:2] == ["plugin", "update"] for call in calls))

    def test_preflight_process_loss_is_structured_and_fail_closed(self) -> None:
        listed = INSTALLER.subprocess.CompletedProcess(
            [],
            0,
            '[{"name":"translate-native","marketplace":"blun-language-tools",'
            '"version":"6.25.0","enabled":true,"errors":[]}]',
            "",
        )
        validated = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(INSTALLER, "_run", side_effect=[listed, validated, OSError("gone")]) as runner:
            result = INSTALLER.preflight_claude_plugin_update("6.26.0", "/missing/claude", ROOT)
        self.assertFalse(result["ready"])
        self.assertEqual(result["catalog"]["reason"], "claude-command-unavailable")
        self.assertEqual(runner.call_count, 3)

    def test_claude_plugin_update_blocks_before_mutation_when_strict_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(Path(directory), fail_validation=True)
            result = INSTALLER.update_claude_plugin("6.8.0", str(executable))
            self.assertTrue(result["attempted"])
            self.assertFalse(result["updated"])
            self.assertEqual(result["validation"]["reason"], "strict-plugin-validation-failed")
            self.assertEqual(state.read_text(encoding="utf-8"), "6.7.1")
            calls = [
                INSTALLER.json.loads(line)
                for line in (Path(directory) / "claude-calls.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(calls[0], ["plugin", "list", "--json"])
            self.assertEqual(calls[1][:2], ["plugin", "validate"])
            self.assertEqual(len(calls), 2)

    def test_claude_plugin_update_blocks_if_validator_process_disappears(self) -> None:
        listed = INSTALLER.subprocess.CompletedProcess(
            [],
            0,
            '[{"name":"translate-native","marketplace":"blun-language-tools",'
            '"version":"6.7.1","enabled":true,"errors":[]}]',
            "",
        )
        with mock.patch.object(INSTALLER, "_run", side_effect=[listed, OSError("gone")]) as runner:
            result = INSTALLER.update_claude_plugin("6.8.0", "/missing/claude")
        self.assertTrue(result["attempted"])
        self.assertFalse(result["updated"])
        self.assertEqual(result["validation"]["reason"], "claude-command-unavailable")
        self.assertEqual(runner.call_count, 2)

    def test_claude_plugin_update_blocks_when_marketplace_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(
                Path(directory), fail_marketplace_update=True
            )
            result = INSTALLER.update_claude_plugin("6.8.0", str(executable))
            self.assertTrue(result["attempted"])
            self.assertFalse(result["updated"])
            self.assertEqual(result["catalog"]["reason"], "marketplace-update-failed")
            self.assertEqual(state.read_text(encoding="utf-8"), "6.7.1")

    def test_claude_plugin_update_blocks_catalog_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(
                Path(directory), advertised_version="6.8.1"
            )
            result = INSTALLER.update_claude_plugin("6.8.0", str(executable))
            self.assertTrue(result["attempted"])
            self.assertFalse(result["updated"])
            self.assertEqual(result["catalog"]["reason"], "catalog-version-mismatch")
            self.assertEqual(result["catalog"]["version"], "6.8.1")
            self.assertEqual(state.read_text(encoding="utf-8"), "6.7.1")

    def test_claude_plugin_update_failure_is_reported_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(
                Path(directory), old_version="6.7.1", new_version="6.8.0", fail_update=True
            )
            result = INSTALLER.update_claude_plugin("6.8.0", str(executable))
            self.assertTrue(result["attempted"])
            self.assertFalse(result["updated"])
            self.assertEqual(state.read_text(encoding="utf-8"), "6.7.1")
            self.assertFalse(result["status"]["healthy"])

    def test_current_claude_plugin_is_not_updated_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable, state = self._fake_claude(Path(directory), old_version="6.8.0", new_version="broken")
            result = INSTALLER.update_claude_plugin("6.8.0", str(executable))
            self.assertFalse(result["attempted"])
            self.assertTrue(result["updated"])
            self.assertFalse(result["reload_required"])
            self.assertEqual(state.read_text(encoding="utf-8"), "6.8.0")

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

    def test_missing_health_state_migrates_claude_installation_on_next_wake(self) -> None:
        originals = (
            INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_STATE, INSTALLER.HEALTH_STATE,
            INSTALLER.HEALTH_CONFIG, INSTALLER.TARGETS,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            claude_target = root / "claude-skill"
            claude_target.symlink_to(skill, target_is_directory=True)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_STATE = root / "update-state.json"
            INSTALLER.HEALTH_STATE = root / "missing-health-state.json"
            INSTALLER.HEALTH_CONFIG = root / "missing-health-config.json"
            INSTALLER.TARGETS = {**INSTALLER.TARGETS, "claude": claude_target}
            INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                "enabled": True,
                "interval_hours": 24,
                "require_signed_commits": False,
                "repository": INSTALLER.REPO_URL,
            })
            INSTALLER._atomic_json(INSTALLER.UPDATE_STATE, {
                "status": "ok",
                "checked_at": int(INSTALLER.time.time()),
            })
            try:
                with mock.patch.object(INSTALLER, "update", return_value=8) as updater:
                    self.assertEqual(INSTALLER.auto_update("run"), 8)
                    updater.assert_called_once_with(False, None)
            finally:
                (
                    INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_STATE, INSTALLER.HEALTH_STATE,
                    INSTALLER.HEALTH_CONFIG, INSTALLER.TARGETS,
                ) = originals

    def test_explicitly_disabled_health_monitor_is_not_reinstalled(self) -> None:
        originals = (
            INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_STATE, INSTALLER.HEALTH_STATE,
            INSTALLER.HEALTH_CONFIG, INSTALLER.TARGETS,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            claude_target = root / "claude-skill"
            claude_target.symlink_to(skill, target_is_directory=True)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_STATE = root / "update-state.json"
            INSTALLER.HEALTH_STATE = root / "missing-health-state.json"
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.TARGETS = {**INSTALLER.TARGETS, "claude": claude_target}
            INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                "enabled": True, "interval_hours": 24, "require_signed_commits": False,
            })
            INSTALLER._atomic_json(INSTALLER.UPDATE_STATE, {
                "status": "ok", "checked_at": int(INSTALLER.time.time()),
            })
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {"enabled": False})
            try:
                with mock.patch.object(INSTALLER, "update") as updater, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("run"), 0)
                updater.assert_not_called()
            finally:
                (
                    INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_STATE, INSTALLER.HEALTH_STATE,
                    INSTALLER.HEALTH_CONFIG, INSTALLER.TARGETS,
                ) = originals

    def test_health_monitor_remove_preserves_services_and_persists_opt_out(self) -> None:
        originals = (INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {"status": "ok"})
            try:
                with mock.patch.object(INSTALLER, "remove_health_monitor") as remover, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor("remove"), 0)
                    self.assertEqual(INSTALLER.health_monitor("status"), 0)
                remover.assert_called_once_with()
                self.assertFalse(INSTALLER.HEALTH_STATE.exists())
                policy = INSTALLER.json.loads(INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8"))
                self.assertFalse(policy["enabled"])
            finally:
                INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE = originals

    def test_missing_or_uninstalled_claude_plugin_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unavailable = INSTALLER.claude_plugin_status("6.8.0", str(root / "missing-claude"))
            self.assertEqual(unavailable["reason"], "claude-command-unavailable")
            executable = root / "claude"
            executable.write_text(
                "#!/usr/bin/env python3\nimport json\nprint(json.dumps([]))\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            result = INSTALLER.update_claude_plugin("6.8.0", str(executable))
            self.assertFalse(result["attempted"])
            self.assertEqual(result["status"]["reason"], "plugin-not-installed")

    def test_persistent_mcp_probe_requires_a_real_swedish_tool_call(self) -> None:
        responses = [
            (200, {"status": "ok", "isolated_key": True}),
            (200, {"result": {"protocolVersion": "2025-06-18"}}),
            (200, {"result": {"tools": [
                {"name": "release_response"},
                {"name": "release_translation"},
                {"name": "verify_release_token"},
            ]}}),
            (200, {"result": {"structuredContent": {
                "status": "PASS", "release_allowed": True, "language": "sv-SE",
            }}}),
        ]
        with mock.patch.object(INSTALLER, "_mcp_http_request", side_effect=responses) as request:
            result = INSTALLER.probe_mcp_http(timeout=0.1)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args_list[-1].args[1]["method"], "tools/call")
        self.assertEqual(
            request.call_args_list[-1].args[1]["params"]["arguments"]["text"],
            "Hälsokontrollen är aktiv.",
        )
        self.assertEqual(result["canary"], {"status": "PASS", "language": "sv-SE"})

    def test_persistent_mcp_probe_blocks_when_tool_dispatch_is_broken(self) -> None:
        responses = [
            (200, {"status": "ok", "isolated_key": True}),
            (200, {"result": {"protocolVersion": "2025-06-18"}}),
            (200, {"result": {"tools": [
                {"name": "release_response"},
                {"name": "release_translation"},
                {"name": "verify_release_token"},
            ]}}),
            (500, {"error": {"message": "Internal MCP failure"}}),
        ]
        with mock.patch.object(INSTALLER, "_mcp_http_request", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "tools/call"):
                INSTALLER.probe_mcp_http(timeout=0.1)

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

    def test_signing_key_rejects_invalid_existing_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "signing.key"
            key.write_bytes(b"short")
            with self.assertRaisesRegex(RuntimeError, "invalid size"):
                INSTALLER.ensure_signing_key(key)
            key.write_bytes(b"x" * (64 * 1024 + 1))
            with self.assertRaisesRegex(RuntimeError, "invalid size"):
                INSTALLER.ensure_signing_key(key)

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_signing_key_does_not_follow_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "signing.key"
            sentinel = root / "sentinel"
            sentinel.write_bytes(b"s" * 32)
            key.with_suffix(".tmp").symlink_to(sentinel)

            INSTALLER.ensure_signing_key(key)
            self.assertEqual(sentinel.read_bytes(), b"s" * 32)
            self.assertTrue(key.with_suffix(".tmp").is_symlink())

            key.unlink()
            key.symlink_to(sentinel)
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                INSTALLER.ensure_signing_key(key)
            self.assertEqual(sentinel.read_bytes(), b"s" * 32)

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

    def test_service_token_rejects_invalid_existing_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "service.token"
            token.write_bytes(b"short")
            with self.assertRaisesRegex(RuntimeError, "invalid size"):
                INSTALLER.ensure_service_token(token)
            token.write_bytes(b"x" * (64 * 1024 + 1))
            with self.assertRaisesRegex(RuntimeError, "invalid size"):
                INSTALLER.ensure_service_token(token)

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_service_token_does_not_follow_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "service.token"
            sentinel = root / "sentinel"
            sentinel.write_text("s" * 64 + "\n", encoding="ascii")
            token.with_suffix(".tmp").symlink_to(sentinel)

            INSTALLER.ensure_service_token(token)
            self.assertEqual(sentinel.read_text(encoding="ascii"), "s" * 64 + "\n")
            self.assertTrue(token.with_suffix(".tmp").is_symlink())

            token.unlink()
            token.symlink_to(sentinel)
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                INSTALLER.ensure_service_token(token)
            self.assertEqual(sentinel.read_text(encoding="ascii"), "s" * 64 + "\n")

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

    def test_operation_lock_excludes_overlap_and_releases_only_its_owner(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            try:
                first = INSTALLER._acquire_operation_lock("update", now=100)
                self.assertIsNotNone(first)
                self.assertIsNone(INSTALLER._acquire_operation_lock("health-monitor", now=101))
                INSTALLER._release_operation_lock("not-the-owner")
                self.assertTrue(INSTALLER.OPERATION_LOCK.exists())
                assert first is not None
                INSTALLER._release_operation_lock(first)
                self.assertFalse(INSTALLER.OPERATION_LOCK.exists())
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_new_operation_lock_records_process_generation_when_available(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        start_id = "linux:11111111-1111-1111-1111-111111111111:100"
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            try:
                with mock.patch.object(INSTALLER, "_process_start_identity", return_value=start_id):
                    token = INSTALLER._acquire_operation_lock("update", now=100)
                self.assertIsNotNone(token)
                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["process_start_id"], start_id)
                assert token is not None
                INSTALLER._release_operation_lock(token)
            finally:
                INSTALLER.OPERATION_LOCK = original

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux procfs-specific check")
    def test_linux_current_process_generation_is_boot_bound(self) -> None:
        identity = INSTALLER._linux_process_start_identity(os.getpid())
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertRegex(
            identity,
            r"^linux:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}:[0-9]+$",
        )

    def test_stale_operation_lock_is_recovered_once(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            INSTALLER.OPERATION_LOCK.write_text("stale", encoding="utf-8")
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))
            try:
                token = INSTALLER._acquire_operation_lock(
                    "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1
                )
                self.assertIsNotNone(token)
                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["operation"], "health-monitor")
                assert token is not None
                INSTALLER._release_operation_lock(token)
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_old_operation_lock_owned_by_living_process_is_never_stolen(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            try:
                token = INSTALLER._acquire_operation_lock("update", now=100)
                self.assertIsNotNone(token)
                os.utime(INSTALLER.OPERATION_LOCK, (100, 100))

                contender = INSTALLER._acquire_operation_lock(
                    "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 3600
                )

                self.assertIsNone(contender)
                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["pid"], os.getpid())
                self.assertEqual(value["token"], token)
                assert token is not None
                INSTALLER._release_operation_lock(token)
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_reused_live_pid_does_not_preserve_an_old_process_generation(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.OPERATION_LOCK, {
                "operation": "update", "pid": 424242, "started_at": 100, "token": "a" * 32,
                "process_start_id": "linux:11111111-1111-1111-1111-111111111111:100",
            })
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))
            try:
                with mock.patch.object(INSTALLER, "_process_is_alive", return_value=True), \
                     mock.patch.object(
                         INSTALLER, "_process_start_identity",
                         return_value="linux:11111111-1111-1111-1111-111111111111:200",
                     ):
                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1
                    )
                self.assertIsNotNone(token)
                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["operation"], "health-monitor")
                assert token is not None
                INSTALLER._release_operation_lock(token)
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_matching_process_generation_preserves_an_old_live_lock(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        start_id = "linux:11111111-1111-1111-1111-111111111111:100"
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.OPERATION_LOCK, {
                "operation": "update", "pid": 424242, "started_at": 100, "token": "a" * 32,
                "process_start_id": start_id,
            })
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))
            try:
                with mock.patch.object(INSTALLER, "_process_is_alive", return_value=True), \
                     mock.patch.object(INSTALLER, "_process_start_identity", return_value=start_id):
                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1
                    )
                self.assertIsNone(token)
                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["token"], "a" * 32)
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_ambiguous_process_generation_preserves_an_old_live_lock(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.OPERATION_LOCK, {
                "operation": "update", "pid": 424242, "started_at": 100, "token": "a" * 32,
                "process_start_id": "posix:" + "a" * 64,
            })
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))
            try:
                with mock.patch.object(INSTALLER, "_process_is_alive", return_value=True), \
                     mock.patch.object(INSTALLER, "_process_start_identity", return_value=None):
                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1
                    )
                self.assertIsNone(token)
                self.assertTrue(INSTALLER.OPERATION_LOCK.exists())
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_legacy_live_lock_without_process_generation_remains_compatible(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.OPERATION_LOCK, {
                "operation": "update", "pid": 424242, "started_at": 100, "token": "a" * 32,
            })
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))
            try:
                with mock.patch.object(INSTALLER, "_process_is_alive", return_value=True), \
                     mock.patch.object(INSTALLER, "_process_start_identity") as start_probe:
                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1
                    )
                self.assertIsNone(token)
                start_probe.assert_not_called()
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_old_operation_lock_from_dead_process_is_recovered(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.OPERATION_LOCK, {
                "operation": "update", "pid": 424242, "started_at": 100, "token": "a" * 32,
            })
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))
            try:
                with mock.patch.object(INSTALLER, "_process_is_alive", return_value=False) as alive:
                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1
                    )
                alive.assert_called_once_with(424242)
                self.assertIsNotNone(token)
                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["operation"], "health-monitor")
                self.assertEqual(value["pid"], os.getpid())
                assert token is not None
                INSTALLER._release_operation_lock(token)
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_windows_liveness_probe_never_uses_process_signals(self) -> None:
        pid = os.getpid() + 1000
        with mock.patch.object(INSTALLER.os, "name", "nt"), \
             mock.patch.object(INSTALLER, "_windows_process_is_alive", return_value=True) as windows_probe, \
             mock.patch.object(INSTALLER.os, "kill") as process_signal:
            self.assertTrue(INSTALLER._process_is_alive(pid))
        windows_probe.assert_called_once_with(pid)
        process_signal.assert_not_called()

    def test_windows_process_generation_probe_never_uses_process_signals(self) -> None:
        pid = os.getpid() + 1000
        with mock.patch.object(INSTALLER.os, "name", "nt"), \
             mock.patch.object(
                 INSTALLER, "_windows_process_start_identity", return_value="windows:0000000000000001"
             ) as windows_probe, \
             mock.patch.object(INSTALLER.os, "kill") as process_signal:
            self.assertEqual(INSTALLER._process_start_identity(pid), "windows:0000000000000001")
        windows_probe.assert_called_once_with(pid)
        process_signal.assert_not_called()

    def test_changed_operation_lock_is_not_removed_during_stale_recovery(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            INSTALLER.OPERATION_LOCK.write_text("stale", encoding="utf-8")
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))

            def replace_lock(_metadata: os.stat_result) -> bool:
                INSTALLER.OPERATION_LOCK.unlink()
                INSTALLER._atomic_json(INSTALLER.OPERATION_LOCK, {
                    "operation": "rollback", "pid": os.getpid(), "started_at": 200, "token": "replacement",
                })
                return False

            try:
                with mock.patch.object(INSTALLER, "_operation_lock_owner_alive", side_effect=replace_lock):
                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor", now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1
                    )
                self.assertIsNone(token)
                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["operation"], "rollback")
                self.assertEqual(value["token"], "replacement")
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_changed_operation_lock_is_not_removed_during_release(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.OPERATION_LOCK = Path(directory) / "operation.lock"
            try:
                token = INSTALLER._acquire_operation_lock("update", now=100)
                self.assertIsNotNone(token)
                original_value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))

                def replace_after_read(_metadata: os.stat_result) -> dict:
                    INSTALLER.OPERATION_LOCK.unlink()
                    INSTALLER._atomic_json(INSTALLER.OPERATION_LOCK, {
                        "operation": "rollback", "pid": os.getpid(), "started_at": 200, "token": "replacement",
                    })
                    return original_value

                with mock.patch.object(INSTALLER, "_read_operation_lock", side_effect=replace_after_read):
                    assert token is not None
                    INSTALLER._release_operation_lock(token)

                value = INSTALLER.json.loads(INSTALLER.OPERATION_LOCK.read_text(encoding="utf-8"))
                self.assertEqual(value["operation"], "rollback")
                self.assertEqual(value["token"], "replacement")
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_health_monitor_clean_probe_does_not_restart_services(self) -> None:
        originals = (INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(True, True)), \
                     mock.patch.object(INSTALLER, "restart_guard_runtime") as guard_restart, \
                     mock.patch.object(INSTALLER, "restart_mcp_http_runtime") as mcp_restart:
                    self.assertEqual(INSTALLER.health_monitor_run(now=1000), 0)
                guard_restart.assert_not_called()
                mcp_restart.assert_not_called()
                state = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "ok")
                self.assertEqual(state["repairs"], [])
                self.assertEqual(state["next_repair_at"], 0)
            finally:
                INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_health_monitor_files_are_bounded_and_schema_validated(self) -> None:
        originals = (INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            try:
                INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                    "enabled": True,
                    "interval_seconds": 60,
                    "plugin_required": False,
                    "claude_command": "claude",
                })
                self.assertTrue(INSTALLER.health_monitor_enabled())

                INSTALLER.HEALTH_CONFIG.write_bytes(
                    b"x" * (INSTALLER.MAX_HEALTH_FILE_BYTES + 1)
                )
                if os.name != "nt":
                    INSTALLER.HEALTH_CONFIG.chmod(0o600)
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    INSTALLER._load_health_config()

                INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {"enabled": "false"})
                with self.assertRaisesRegex(RuntimeError, "field enabled"):
                    INSTALLER._load_health_config()

                INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {
                    "status": "blocked",
                    "checked_at": 10,
                    "consecutive_failures": True,
                })
                with self.assertRaisesRegex(RuntimeError, "consecutive_failures"):
                    INSTALLER._load_health_state()
            finally:
                INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE = originals

    @unittest.skipIf(os.name == "nt", "POSIX link and permission test")
    def test_health_monitor_rejects_links_and_open_permissions_without_repair(self) -> None:
        originals = (INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            sentinel = root / "sentinel.json"
            INSTALLER._atomic_json(sentinel, {
                "status": "blocked",
                "checked_at": 100,
                "consecutive_failures": 5,
                "last_repair_at": 100,
                "next_repair_at": 3700,
            })
            INSTALLER.HEALTH_STATE.symlink_to(sentinel)
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status") as probe, \
                     mock.patch.object(INSTALLER, "restart_guard_runtime") as restart, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=200), 2)
                probe.assert_not_called()
                restart.assert_not_called()
                self.assertTrue(INSTALLER.HEALTH_STATE.is_symlink())
                self.assertEqual(
                    INSTALLER.json.loads(sentinel.read_text(encoding="utf-8"))["next_repair_at"],
                    3700,
                )

                INSTALLER.HEALTH_STATE.unlink()
                INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {"status": "ok", "checked_at": 200})
                INSTALLER.HEALTH_STATE.chmod(0o644)
                with self.assertRaisesRegex(RuntimeError, "owner-only"):
                    INSTALLER._load_health_state()
            finally:
                INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_health_monitor_rejects_state_identity_change_while_reading(self) -> None:
        original = INSTALLER.HEALTH_STATE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            replacement = root / "replacement.json"
            INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {"status": "ok", "checked_at": 1})
            INSTALLER._atomic_json(replacement, {"status": "ok", "checked_at": 2})
            opened = INSTALLER.HEALTH_STATE.stat()
            changed = replacement.stat()
            try:
                with mock.patch.object(INSTALLER.os, "fstat", side_effect=(opened, changed)):
                    with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                        INSTALLER._load_health_state()
            finally:
                INSTALLER.HEALTH_STATE = original

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_update_blocks_unsafe_health_policy_before_candidate_execution(self) -> None:
        originals = (INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "sentinel.json"
            INSTALLER._atomic_json(sentinel, {"enabled": False})
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_CONFIG.symlink_to(sentinel)
            INSTALLER.HEALTH_STATE = root / "missing-state.json"
            try:
                with mock.patch.object(INSTALLER, "_clean_checkout_revision", return_value="a" * 40), \
                     mock.patch.object(INSTALLER, "_run") as runner, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER._update_unlocked(), 2)
                runner.assert_not_called()
                self.assertTrue(INSTALLER.HEALTH_CONFIG.is_symlink())
                self.assertFalse(INSTALLER.json.loads(sentinel.read_text(encoding="utf-8"))["enabled"])
            finally:
                INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE = originals

    def test_health_monitor_skips_dependent_mcp_probe_when_signer_is_down(self) -> None:
        with mock.patch.object(INSTALLER, "probe_guard_service", side_effect=OSError("offline")), \
             mock.patch.object(INSTALLER, "probe_mcp_http") as mcp_probe:
            self.assertEqual(INSTALLER._guard_stack_status(), (False, False))
        mcp_probe.assert_not_called()

    def test_health_monitor_repairs_only_failed_mcp_and_rechecks_full_stack(self) -> None:
        originals = (INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            statuses = [(True, False), (True, False), (True, True)]
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", side_effect=statuses), \
                     mock.patch.object(INSTALLER, "_wait_for_stack", return_value=True) as waiter, \
                     mock.patch.object(INSTALLER, "restart_guard_runtime") as guard_restart, \
                     mock.patch.object(INSTALLER, "restart_mcp_http_runtime", return_value=(True, "test")) as mcp_restart:
                    self.assertEqual(INSTALLER.health_monitor_run(now=2000), 0)
                guard_restart.assert_not_called()
                mcp_restart.assert_called_once_with()
                waiter.assert_called_once_with(guard=True, mcp=True)
                state = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "recovered")
                self.assertEqual(state["repairs"], ["mcp-restart"])
                self.assertEqual(state["consecutive_failures"], 0)
                self.assertEqual(state["next_repair_at"], 0)
            finally:
                INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_health_monitor_repairs_guard_before_dependent_mcp(self) -> None:
        originals = (INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            statuses = [(False, False), (True, False), (True, True)]
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", side_effect=statuses), \
                     mock.patch.object(INSTALLER, "_wait_for_stack", side_effect=[True, True]), \
                     mock.patch.object(INSTALLER, "restart_guard_runtime", return_value=(True, "guard")) as guard_restart, \
                     mock.patch.object(INSTALLER, "restart_mcp_http_runtime", return_value=(True, "mcp")) as mcp_restart:
                    self.assertEqual(INSTALLER.health_monitor_run(now=3000), 0)
                guard_restart.assert_called_once_with()
                mcp_restart.assert_called_once_with()
                state = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(state["repairs"], ["guard-restart", "mcp-restart"])
            finally:
                INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_health_monitor_backoff_prevents_minute_restart_storm(self) -> None:
        originals = (INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {
                "status": "blocked", "checked_at": 4000, "last_repair_at": 4000,
                "consecutive_failures": 3,
            })
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(False, False)), \
                     mock.patch.object(INSTALLER, "restart_guard_runtime") as guard_restart, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=4060), 1)
                guard_restart.assert_not_called()
                state = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(state["reason"], "repair-backoff")
                self.assertEqual(state["consecutive_failures"], 3)
                self.assertEqual(state["next_repair_at"], 4300)
            finally:
                INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_failed_repairs_back_off_exponentially_and_cap_at_one_hour(self) -> None:
        originals = (INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(False, False)), \
                     mock.patch.object(INSTALLER, "restart_guard_runtime", return_value=(False, "offline")), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=5000), 1)
                first = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(first["consecutive_failures"], 1)
                self.assertEqual(first["next_repair_at"], 5060)

                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(False, False)), \
                     mock.patch.object(INSTALLER, "restart_guard_runtime", return_value=(False, "offline")) as restart, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=5030), 1)
                restart.assert_not_called()
                unchanged = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(unchanged["consecutive_failures"], 1)
                self.assertEqual(unchanged["next_repair_at"], 5060)

                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(False, False)), \
                     mock.patch.object(INSTALLER, "restart_guard_runtime", return_value=(False, "offline")) as restart, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=5060), 1)
                restart.assert_called_once_with()
                second = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(second["consecutive_failures"], 2)
                self.assertEqual(second["next_repair_at"], 5180)
                self.assertEqual(INSTALLER._health_repair_delay(999), 3600)
            finally:
                INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_health_monitor_enrolls_and_repairs_an_installed_stale_claude_plugin(self) -> None:
        originals = (
            INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.UPDATE_CONFIG,
            INSTALLER.OPERATION_LOCK, INSTALLER.TARGETS,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            claude_target = root / "claude-skill"
            claude_target.symlink_to(skill, target_is_directory=True)
            current_version = (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
            executable, plugin_version = self._fake_claude(
                root, old_version="6.9.0", new_version=current_version
            )
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.UPDATE_CONFIG = root / "missing-updater.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER.TARGETS = {**INSTALLER.TARGETS, "claude": claude_target}
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                "enabled": True, "interval_seconds": 60, "claude_command": str(executable),
            })
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(True, True)):
                    self.assertEqual(INSTALLER.health_monitor_run(now=6000), 0)
                self.assertEqual(plugin_version.read_text(encoding="utf-8"), current_version)
                policy = INSTALLER.json.loads(INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8"))
                self.assertTrue(policy["plugin_required"])
                state = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "recovered")
                self.assertTrue(state["plugin_required"])
                self.assertTrue(state["plugin_cache_healthy"])
                self.assertEqual(state["plugin_cache_version"], current_version)
                self.assertEqual(state["repairs"], ["claude-plugin-update"])
            finally:
                (
                    INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.UPDATE_CONFIG,
                    INSTALLER.OPERATION_LOCK, INSTALLER.TARGETS,
                ) = originals

    def test_health_monitor_never_installs_a_missing_enrolled_claude_plugin(self) -> None:
        originals = (
            INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.UPDATE_CONFIG,
            INSTALLER.OPERATION_LOCK, INSTALLER.TARGETS,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            claude_target = root / "claude-skill"
            claude_target.symlink_to(skill, target_is_directory=True)
            calls = root / "calls.jsonl"
            executable = root / "claude"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                f"calls = pathlib.Path({str(calls)!r})\n"
                "with calls.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1:] == ['plugin', 'list', '--json']:\n"
                "    print('[]')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(9)\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.UPDATE_CONFIG = root / "missing-updater.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER.TARGETS = {**INSTALLER.TARGETS, "claude": claude_target}
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                "enabled": True,
                "interval_seconds": 60,
                "plugin_required": True,
                "claude_command": str(executable),
            })
            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(True, True)), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=7000), 1)
                observed = [INSTALLER.json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(observed)
                self.assertTrue(all(call == ["plugin", "list", "--json"] for call in observed))
                state = INSTALLER.json.loads(INSTALLER.HEALTH_STATE.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "blocked")
                self.assertFalse(state["plugin_cache_healthy"])
                self.assertEqual(state["plugin_cache_reason"], "plugin-not-installed")
                self.assertEqual(state["repairs"], [])
            finally:
                (
                    INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.UPDATE_CONFIG,
                    INSTALLER.OPERATION_LOCK, INSTALLER.TARGETS,
                ) = originals

    def test_health_monitor_platform_schedulers_are_one_minute_and_non_overlapping(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed):
                ok, _detail = INSTALLER.install_health_monitor(home)
            self.assertTrue(ok)
            timer = (home / ".config" / "systemd" / "user" / "blun-language-guard-health.timer").read_text()
            self.assertIn("OnUnitActiveSec=1m", timer)
            service = (home / ".config" / "systemd" / "user" / "blun-language-guard-health.service").read_text()
            self.assertIn("health-monitor", service)

            with mock.patch.object(INSTALLER.platform, "system", return_value="Darwin"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed):
                ok, _detail = INSTALLER.install_health_monitor(home)
            self.assertTrue(ok)
            plist = (home / "Library" / "LaunchAgents" / "ai.blun.language-guard-health.plist").read_text()
            self.assertIn("<integer>60</integer>", plist)

            with mock.patch.object(INSTALLER.platform, "system", return_value="Windows"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                ok, _detail = INSTALLER.install_health_monitor(home)
            self.assertTrue(ok)
            script = runner.call_args.args[0][4]
            self.assertIn("New-TimeSpan -Minutes 1", script)
            self.assertIn("MultipleInstances IgnoreNew", script)


if __name__ == "__main__":
    unittest.main()
