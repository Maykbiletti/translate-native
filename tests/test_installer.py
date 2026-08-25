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
from types import SimpleNamespace
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

    def test_atomic_symlink_preserves_existing_staging_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            destination = root / "bin" / "guard"
            destination.parent.mkdir()
            legacy = destination.with_name(destination.name + ".new")
            legacy.write_text("keep legacy staging path\n", encoding="utf-8")
            collision = destination.with_name(f".{destination.name}.collision.new")
            collision.write_text("keep random collision\n", encoding="utf-8")
            with mock.patch.object(
                INSTALLER.secrets, "token_hex", side_effect=("collision", "reserved")
            ):
                INSTALLER.atomic_symlink(source, destination)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.resolve(), source.resolve())
            self.assertEqual(legacy.read_text(encoding="utf-8"), "keep legacy staging path\n")
            self.assertEqual(collision.read_text(encoding="utf-8"), "keep random collision\n")
            self.assertFalse(destination.with_name(f".{destination.name}.reserved.new").exists())

    def test_atomic_symlink_preserves_concurrently_replaced_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            original = root / "original"
            original.mkdir()
            destination = root / "bin" / "guard"
            destination.parent.mkdir()
            destination.symlink_to(original, target_is_directory=True)
            real_assert = INSTALLER._assert_installed_symlink_unchanged

            def exchange_then_recheck(path: Path, expected) -> None:
                path.unlink()
                path.write_text("concurrent user file\n", encoding="utf-8")
                real_assert(path, expected)

            with mock.patch.object(
                INSTALLER,
                "_assert_installed_symlink_unchanged",
                side_effect=exchange_then_recheck,
            ):
                with self.assertRaisesRegex(RuntimeError, "non-symlink"):
                    INSTALLER.atomic_symlink(source, destination)
            self.assertFalse(destination.is_symlink())
            self.assertEqual(destination.read_text(encoding="utf-8"), "concurrent user file\n")
            self.assertEqual(
                list(destination.parent.glob(f".{destination.name}.*.new")),
                [],
            )

    def test_service_definition_rejects_links_before_starting_runtime(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            units = home / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            sentinel = home / "sentinel.service"
            sentinel.write_text("preserve me\n", encoding="utf-8")
            service = units / "blun-language-guard-health.service"
            service.symlink_to(sentinel)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                    INSTALLER.install_health_monitor(home)
            runner.assert_not_called()
            self.assertTrue(service.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")

    def test_service_definition_rejects_hard_links_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "sentinel"
            sentinel.write_text("preserve me\n", encoding="utf-8")
            linked = root / "linked.service"
            os.link(sentinel, linked)
            with self.assertRaisesRegex(RuntimeError, "additional hard links"):
                INSTALLER._write_service_definition(linked, "replacement\n")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")

            if hasattr(os, "mkfifo"):
                fifo = root / "blocked.timer"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                    INSTALLER._write_service_definition(fifo, "replacement\n")

    def test_service_definition_rejects_oversize_and_open_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "oversized.service"
            oversized.write_bytes(b"x" * (INSTALLER.MAX_SERVICE_DEFINITION_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, "exceeds the size limit"):
                INSTALLER._write_service_definition(oversized, "replacement\n")

            if os.name != "nt":
                broad = root / "broad.timer"
                broad.write_text("original\n", encoding="utf-8")
                broad.chmod(0o666)
                with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                    INSTALLER._write_service_definition(broad, "replacement\n")
                self.assertEqual(broad.read_text(encoding="utf-8"), "original\n")

    def test_service_definition_preserves_concurrently_replaced_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = root / "guard.service"
            definition.write_text("original\n", encoding="utf-8")
            real_assert = INSTALLER._assert_service_definition_unchanged

            def exchange_then_recheck(path: Path, expected) -> None:
                path.unlink()
                path.write_text("concurrent user file\n", encoding="utf-8")
                real_assert(path, expected)

            with mock.patch.object(
                INSTALLER,
                "_assert_service_definition_unchanged",
                side_effect=exchange_then_recheck,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed before replacement"):
                    INSTALLER._write_service_definition(definition, "replacement\n")
            self.assertEqual(
                definition.read_text(encoding="utf-8"),
                "concurrent user file\n",
            )
            self.assertEqual(list(root.glob(".guard.service.*.tmp")), [])

    def test_service_definition_rejects_unsafe_parent_before_starting_runtime(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            redirected = home / "redirected"
            redirected.mkdir()
            systemd = home / ".config" / "systemd"
            systemd.mkdir(parents=True)
            units = systemd / "user"
            units.symlink_to(redirected, target_is_directory=True)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "safely open service-definition directory"):
                    INSTALLER.install_health_monitor(home)
            runner.assert_not_called()
            self.assertEqual(list(redirected.iterdir()), [])

            units.unlink()
            units.mkdir()
            if os.name != "nt":
                units.chmod(0o777)
                with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                     mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                    with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                        INSTALLER.install_health_monitor(home)
                runner.assert_not_called()
                units.chmod(0o700)

            library = home / "Library"
            library.mkdir()
            agents = library / "LaunchAgents"
            agents.symlink_to(redirected, target_is_directory=True)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Darwin"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "safely open service-definition directory"):
                    INSTALLER.install_health_monitor(home)
            runner.assert_not_called()
            self.assertEqual(list(redirected.iterdir()), [])

    def test_service_definition_preserves_parent_exchanged_during_write(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            units = home / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            redirected = home / "redirected"
            redirected.mkdir()
            detached = home / "detached-units"
            real_assert = INSTALLER._assert_service_directory_unchanged

            def exchange_then_recheck(path: Path, expected) -> None:
                path.rename(detached)
                path.symlink_to(redirected, target_is_directory=True)
                real_assert(path, expected)

            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner, \
                 mock.patch.object(
                     INSTALLER,
                     "_assert_service_directory_unchanged",
                     side_effect=exchange_then_recheck,
                 ):
                with self.assertRaisesRegex(RuntimeError, "not a directory"):
                    INSTALLER.install_health_monitor(home)
            runner.assert_not_called()
            self.assertEqual(list(redirected.iterdir()), [])
            self.assertTrue((detached / "blun-language-guard-health.service").is_file())
            self.assertEqual(list(detached.glob(".*.tmp")), [])

    def test_service_definition_removal_rejects_unsafe_or_foreign_state(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            units = home / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            sentinel = home / "sentinel.service"
            sentinel.write_text("preserve me\n", encoding="utf-8")
            service = units / "blun-language-guard-health.service"
            service.symlink_to(sentinel)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                    INSTALLER.remove_health_monitor(home)
            runner.assert_not_called()
            self.assertTrue(service.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")

            service.unlink()
            service.write_text("unrelated user service\n", encoding="utf-8")
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "not managed by BLUN"):
                    INSTALLER.remove_health_monitor(home)
            runner.assert_not_called()
            self.assertEqual(service.read_text(encoding="utf-8"), "unrelated user service\n")

            service.unlink()
            os.link(sentinel, service)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "additional hard links"):
                    INSTALLER.remove_health_monitor(home)
            runner.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")

            if hasattr(os, "mkfifo"):
                service.unlink()
                os.mkfifo(service)
                with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                     mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                    with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                        INSTALLER.remove_health_monitor(home)
                runner.assert_not_called()

    def test_service_definition_removal_preserves_concurrent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = root / "guard.service"
            definition.write_text(
                "[Unit]\nDescription=Verify and repair BLUN Language Guard\n"
                "[Service]\nExecStart=python health-monitor run\n",
                encoding="utf-8",
            )
            directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            expected = INSTALLER._preflight_service_definition_removal_at(
                directory_fd,
                definition,
                ("Verify and repair BLUN Language Guard", "health-monitor"),
            )
            real_assert = INSTALLER._assert_service_definition_at_unchanged

            def exchange_then_recheck(open_directory: int, path: Path, identity) -> None:
                path.unlink()
                path.write_text("concurrent user file\n", encoding="utf-8")
                real_assert(open_directory, path, identity)

            try:
                with mock.patch.object(
                    INSTALLER,
                    "_assert_service_definition_at_unchanged",
                    side_effect=exchange_then_recheck,
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed before replacement"):
                        INSTALLER._remove_service_definition_at(
                            directory_fd,
                            definition,
                            expected,
                        )
            finally:
                os.close(directory_fd)
            self.assertEqual(
                definition.read_text(encoding="utf-8"),
                "concurrent user file\n",
            )

    def test_service_definition_removal_rejects_unsafe_parent_before_commands(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            redirected = home / "redirected"
            redirected.mkdir()
            service = redirected / "blun-language-guard-health.service"
            timer = redirected / "blun-language-guard-health.timer"
            service.write_text(
                "[Unit]\nDescription=Verify and repair BLUN Language Guard\n"
                "[Service]\nExecStart=python health-monitor run\n",
                encoding="utf-8",
            )
            timer.write_text(
                "[Unit]\nDescription=Monitor BLUN Language Guard every minute\n"
                "[Timer]\nOnUnitActiveSec=1m\n",
                encoding="utf-8",
            )
            systemd = home / ".config" / "systemd"
            systemd.mkdir(parents=True)
            units = systemd / "user"
            units.symlink_to(redirected, target_is_directory=True)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "safely open service-definition directory"):
                    INSTALLER.remove_health_monitor(home)
            runner.assert_not_called()
            self.assertTrue(service.is_file())
            self.assertTrue(timer.is_file())

            units.unlink()
            units.mkdir()
            local_service = units / service.name
            local_service.write_text(service.read_text(encoding="utf-8"), encoding="utf-8")
            local_timer = units / timer.name
            local_timer.write_text(timer.read_text(encoding="utf-8"), encoding="utf-8")
            if os.name != "nt":
                units.chmod(0o777)
                with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                     mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                    with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                        INSTALLER.remove_health_monitor(home)
                runner.assert_not_called()
                self.assertTrue(local_service.is_file())
                self.assertTrue(local_timer.is_file())
                units.chmod(0o700)

            library = home / "Library"
            library.mkdir()
            agents = library / "LaunchAgents"
            agents.symlink_to(redirected, target_is_directory=True)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Darwin"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                with self.assertRaisesRegex(RuntimeError, "safely open service-definition directory"):
                    INSTALLER.remove_health_monitor(home)
            runner.assert_not_called()

    def test_service_definition_removal_keeps_missing_directory_compatible(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner:
                INSTALLER.remove_health_monitor(home)
            self.assertEqual(runner.call_count, 2)
            self.assertFalse((home / ".config").exists())

    def test_service_definition_removal_blocks_parent_exchange_before_commands(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed):
                self.assertTrue(INSTALLER.install_health_monitor(home)[0])
            units = home / ".config" / "systemd" / "user"
            redirected = home / "redirected"
            redirected.mkdir()
            sentinel = redirected / "blun-language-guard-health.service"
            sentinel.write_text("preserve redirected service\n", encoding="utf-8")
            detached = home / "detached-units"
            real_preflight = INSTALLER._preflight_service_definition_removal_at
            exchanged = False

            def exchange_parent(directory_fd: int, path: Path, markers: tuple[str, ...]):
                nonlocal exchanged
                expected = real_preflight(directory_fd, path, markers)
                if not exchanged:
                    exchanged = True
                    units.rename(detached)
                    units.symlink_to(redirected, target_is_directory=True)
                return expected

            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed) as runner, \
                 mock.patch.object(
                     INSTALLER,
                     "_preflight_service_definition_removal_at",
                     side_effect=exchange_parent,
                 ):
                with self.assertRaisesRegex(RuntimeError, "not a directory"):
                    INSTALLER.remove_health_monitor(home)
            runner.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve redirected service\n")
            self.assertTrue((detached / "blun-language-guard-health.service").is_file())
            self.assertTrue((detached / "blun-language-guard-health.timer").is_file())

    def test_health_service_definitions_install_and_remove_on_linux_and_macos(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed):
                self.assertTrue(INSTALLER.install_health_monitor(home)[0])
                INSTALLER.remove_health_monitor(home)
            units = home / ".config" / "systemd" / "user"
            self.assertFalse((units / "blun-language-guard-health.service").exists())
            self.assertFalse((units / "blun-language-guard-health.timer").exists())

            with mock.patch.object(INSTALLER.platform, "system", return_value="Darwin"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed):
                self.assertTrue(INSTALLER.install_health_monitor(home)[0])
                INSTALLER.remove_health_monitor(home)
            plist = home / "Library" / "LaunchAgents" / "ai.blun.language-guard-health.plist"
            self.assertFalse(plist.exists())

    def test_updater_and_mcp_service_definitions_remove_when_managed(self) -> None:
        completed = INSTALLER.subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(INSTALLER.Path, "home", return_value=home), \
                 mock.patch.object(INSTALLER.platform, "system", return_value="Linux"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed):
                self.assertTrue(INSTALLER.install_scheduler()[0])
                self.assertTrue(INSTALLER.install_mcp_http_autostart(ROOT)[0])
                INSTALLER.remove_scheduler()
                INSTALLER.remove_mcp_http_autostart()
            units = home / ".config" / "systemd" / "user"
            for name in (
                "blun-language-guard-update.service",
                "blun-language-guard-update.timer",
                "blun-language-guard-mcp.service",
            ):
                self.assertFalse((units / name).exists())

            with mock.patch.object(INSTALLER.Path, "home", return_value=home), \
                 mock.patch.object(INSTALLER.platform, "system", return_value="Darwin"), \
                 mock.patch.object(INSTALLER, "_run", return_value=completed):
                self.assertTrue(INSTALLER.install_scheduler()[0])
                self.assertTrue(INSTALLER.install_mcp_http_autostart(ROOT)[0])
                INSTALLER.remove_scheduler()
                INSTALLER.remove_mcp_http_autostart()
            agents = home / "Library" / "LaunchAgents"
            self.assertFalse((agents / "ai.blun.language-guard-updater.plist").exists())
            self.assertFalse((agents / "ai.blun.language-guard-mcp.plist").exists())

    def test_blun_mcp_merge_preserves_servers_and_protects_config_state(self) -> None:
        entry = {
            "command": "python3",
            "args": ["guard.py", "serve"],
            "env": {
                "BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT": "tcp:127.0.0.1:47631",
                "BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE": "/private/service.token",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".blun" / "mcp.json"
            config.parent.mkdir()
            original = INSTALLER.json.dumps({
                "theme": "dark",
                "mcpServers": {"keep": {"command": "keep"}},
            }).encode("utf-8")
            config.write_bytes(original)
            if os.name != "nt":
                config.chmod(0o644)
            backup = config.with_suffix(".json.bak")
            sentinel = root / "sentinel"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            try:
                backup.symlink_to(sentinel)
            except OSError:
                backup = Path()

            result = INSTALLER.merge_blun_mcp_config(config, entry)

            self.assertEqual(result, config.with_suffix(".json.bak"))
            merged = INSTALLER.json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(merged["theme"], "dark")
            self.assertEqual(merged["mcpServers"]["keep"], {"command": "keep"})
            self.assertEqual(merged["mcpServers"][INSTALLER.MCP_SERVER_NAME], entry)
            self.assertEqual(config.with_suffix(".json.bak").read_bytes(), original)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            if os.name != "nt":
                self.assertEqual(config.stat().st_mode & 0o077, 0)
                self.assertEqual(config.with_suffix(".json.bak").stat().st_mode & 0o077, 0)

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".blun" / "mcp.json"
            result = INSTALLER.merge_blun_mcp_config(config, entry)
            self.assertIsNone(result)
            self.assertEqual(
                INSTALLER.json.loads(config.read_text(encoding="utf-8"))["mcpServers"][INSTALLER.MCP_SERVER_NAME],
                entry,
            )

    @unittest.skipIf(os.name == "nt", "POSIX file-type and permission tests")
    def test_blun_mcp_merge_rejects_unsafe_files_without_changing_targets(self) -> None:
        entry = {"command": "python3"}
        cases = ("symlink", "hardlink", "fifo", "permissions", "oversized")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / ".blun" / "mcp.json"
                config.parent.mkdir()
                sentinel = root / "sentinel"
                sentinel.write_text('{"mcpServers": {}}\n', encoding="utf-8")
                sentinel.chmod(0o600)
                if case == "symlink":
                    config.symlink_to(sentinel)
                    expected = "regular file"
                elif case == "hardlink":
                    os.link(sentinel, config)
                    expected = "hard links"
                elif case == "fifo":
                    os.mkfifo(config)
                    expected = "regular file"
                elif case == "permissions":
                    config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
                    config.chmod(0o622)
                    expected = "writable outside"
                else:
                    config.write_bytes(b"x" * (INSTALLER.MAX_BLUN_MCP_CONFIG_BYTES + 1))
                    config.chmod(0o600)
                    expected = "size limit"
                with self.assertRaisesRegex(RuntimeError, expected):
                    INSTALLER.merge_blun_mcp_config(config, entry)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')
                self.assertFalse(config.with_suffix(".json.bak").exists())

    def test_blun_mcp_merge_rejects_identity_exchange_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".blun" / "mcp.json"
            config.parent.mkdir()
            config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            if os.name != "nt":
                config.chmod(0o600)
            details = config.stat()
            fields = {
                name: getattr(details, name)
                for name in (
                    "st_mode", "st_uid", "st_dev", "st_ino", "st_nlink", "st_size",
                    "st_ctime_ns", "st_mtime_ns",
                )
            }
            opened = SimpleNamespace(**fields)
            changed = SimpleNamespace(**fields)
            changed.st_mtime_ns += 1
            with mock.patch.object(INSTALLER.os, "fstat", side_effect=(opened, changed)):
                with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                    INSTALLER.merge_blun_mcp_config(config, {"command": "python3"})
            self.assertEqual(config.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')
            self.assertFalse(config.with_suffix(".json.bak").exists())

    @unittest.skipIf(os.name == "nt", "POSIX symbolic-link test")
    def test_blun_install_preflights_config_before_runtime_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blun = root / ".blun"
            blun.mkdir()
            sentinel = root / "sentinel.json"
            sentinel.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            sentinel.chmod(0o600)
            (blun / "mcp.json").symlink_to(sentinel)
            with mock.patch.object(INSTALLER.Path, "home", return_value=root), \
                 mock.patch.object(INSTALLER, "atomic_symlink") as link, \
                 mock.patch.object(INSTALLER, "install_delivery_boundary") as delivery, \
                 mock.patch.object(INSTALLER, "install_guard_runtime") as runtime:
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    INSTALLER.install(["blun"], autostart_service=False)
            link.assert_not_called()
            delivery.assert_not_called()
            runtime.assert_not_called()
            self.assertTrue((blun / "mcp.json").is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')

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

    def test_update_runtime_rollback_preserves_exchanged_created_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, active, old, _candidate = self._update_repository_pair(root)
            claude_skill = root / "claude-skill"
            claude_skill.symlink_to(active / "translate-native", target_is_directory=True)
            mcp_command = root / "bin" / "blun-language-guard-mcp"
            mcp_headers = root / "bin" / "blun-language-guard-mcp-headers"
            mcp_token = root / "config" / "mcp-http.token"
            claude_config = root / ".claude.json"

            def install_runtime_artifacts(_repository: Path) -> None:
                mcp_command.parent.mkdir(parents=True, exist_ok=True)
                mcp_token.parent.mkdir(parents=True, exist_ok=True)
                mcp_command.symlink_to(root / "gateway.py")
                mcp_headers.symlink_to(root / "headers.py")
                mcp_token.write_text("t" * 64 + "\n", encoding="ascii")
                if os.name != "nt":
                    mcp_token.chmod(0o600)

            def exchange_command_before_failure(_repository: Path) -> tuple[bool, str]:
                replacement = root / "foreign-command"
                replacement.write_text("operator-owned replacement\n", encoding="utf-8")
                os.replace(replacement, mcp_command)
                return False, "injected activation failure"

            errors = io.StringIO()
            with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                 mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", root / "update-state.json"), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "updater.json"), \
                 mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", root / "updater-paused.json"), \
                 mock.patch.object(INSTALLER, "HEALTH_CONFIG", root / "health-monitor.json"), \
                 mock.patch.object(INSTALLER, "HEALTH_STATE", root / "health-state.json"), \
                 mock.patch.object(INSTALLER, "MCP_HTTP_COMMAND", mcp_command), \
                 mock.patch.object(INSTALLER, "MCP_HEADERS_COMMAND", mcp_headers), \
                 mock.patch.object(INSTALLER, "MCP_HTTP_TOKEN", mcp_token), \
                 mock.patch.object(INSTALLER, "CLAUDE_CONFIG", claude_config), \
                 mock.patch.object(INSTALLER, "SERVICE_COMMAND", root / "missing-service"), \
                 mock.patch.object(INSTALLER, "TARGETS", {**INSTALLER.TARGETS, "claude": claude_skill}), \
                 mock.patch.object(INSTALLER, "preflight_claude_plugin_update", return_value={"ready": True}), \
                 mock.patch.object(
                     INSTALLER,
                     "install_mcp_http_runtime",
                     side_effect=install_runtime_artifacts,
                 ), \
                 mock.patch.object(
                     INSTALLER,
                     "install_mcp_http_autostart",
                     side_effect=exchange_command_before_failure,
                 ), \
                 mock.patch.object(INSTALLER, "remove_mcp_http_autostart") as remove_autostart, \
                 mock.patch.object(INSTALLER, "restart_guard_runtime"), \
                 contextlib.redirect_stderr(errors):
                self.assertEqual(INSTALLER._update_unlocked(), 1)

            self.assertTrue(
                mcp_command.exists() or mcp_command.is_symlink(),
                errors.getvalue(),
            )
            self.assertEqual(
                mcp_command.read_text(encoding="utf-8"),
                "operator-owned replacement\n",
            )
            self.assertTrue(mcp_headers.is_symlink())
            self.assertTrue(mcp_token.is_file())
            self.assertIn("cleanup blocked fail-closed", errors.getvalue())
            self.assertIn("rollback FAILED", errors.getvalue())
            remove_autostart.assert_not_called()
            self.assertEqual(INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip(), old)

    def test_update_runtime_rollback_preserves_parallel_checkout_changes(self) -> None:
        for change_kind in ("dirty", "new-head"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                upstream, active, _old, candidate = self._update_repository_pair(root)
                service = root / "installed-service"
                service.write_text("installed\n", encoding="utf-8")
                parallel = active / "parallel-runtime-work.txt"
                real_run = INSTALLER._run

                def fail_after_parallel_change() -> tuple[bool, str]:
                    parallel.write_text("operator work during runtime activation\n", encoding="utf-8")
                    if change_kind == "new-head":
                        self.assertEqual(real_run(["git", "add", parallel.name], active).returncode, 0)
                        self.assertEqual(
                            real_run(["git", "commit", "-m", "parallel runtime work"], active).returncode,
                            0,
                        )
                    return False, "injected restart failure"

                errors = io.StringIO()
                with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                     mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                     mock.patch.object(INSTALLER, "UPDATE_STATE", root / "update-state.json"), \
                     mock.patch.object(INSTALLER, "UPDATE_CONFIG", root / "updater.json"), \
                     mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", root / "updater-paused.json"), \
                     mock.patch.object(INSTALLER, "HEALTH_CONFIG", root / "health-monitor.json"), \
                     mock.patch.object(INSTALLER, "HEALTH_STATE", root / "health-state.json"), \
                     mock.patch.object(INSTALLER, "SERVICE_COMMAND", service), \
                     mock.patch.object(
                         INSTALLER,
                         "TARGETS",
                         {**INSTALLER.TARGETS, "claude": root / "missing-claude"},
                     ), \
                     mock.patch.object(
                         INSTALLER,
                         "restart_guard_runtime",
                         side_effect=fail_after_parallel_change,
                     ) as guard_restart, \
                     mock.patch.object(INSTALLER, "restart_mcp_http_runtime") as mcp_restart, \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(INSTALLER._update_unlocked(), 1)

                observed = real_run(["git", "rev-parse", "HEAD"], active).stdout.strip()
                if change_kind == "dirty":
                    self.assertEqual(observed, candidate)
                    self.assertIn("checkout changed before runtime rollback", errors.getvalue())
                else:
                    self.assertNotEqual(observed, candidate)
                    self.assertEqual(
                        real_run(["git", "merge-base", "--is-ancestor", candidate, observed], active).returncode,
                        0,
                    )
                    self.assertIn("HEAD changed independently before runtime rollback", errors.getvalue())
                self.assertEqual(
                    parallel.read_text(encoding="utf-8"),
                    "operator work during runtime activation\n",
                )
                self.assertIn("rollback FAILED", errors.getvalue())
                self.assertEqual(guard_restart.call_count, 1)
                mcp_restart.assert_not_called()

    def test_update_rollback_helpers_preserve_exchanged_artifacts_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, install_as_link in (
                ("mcp-command", True),
                ("mcp-headers", True),
                ("mcp-token", False),
                ("claude-config", False),
            ):
                with self.subTest(name=name):
                    path = root / name
                    if install_as_link:
                        path.symlink_to(root / f"{name}-source")
                    else:
                        path.write_text("installer-created\n", encoding="utf-8")
                    expected = INSTALLER._capture_update_artifact(path)
                    replacement = root / f"{name}-replacement"
                    replacement.write_text("operator-owned\n", encoding="utf-8")
                    os.replace(replacement, path)
                    with self.assertRaisesRegex(RuntimeError, "changed before rollback"):
                        INSTALLER._remove_created_update_artifact(path, expected)
                    self.assertEqual(path.read_text(encoding="utf-8"), "operator-owned\n")

            config = root / "restore-config"
            config.write_text("updated\n", encoding="utf-8")
            expected = INSTALLER._capture_update_artifact(config)
            replacement = root / "restore-config-replacement"
            replacement.write_text("concurrent configuration\n", encoding="utf-8")
            os.replace(replacement, config)
            with self.assertRaisesRegex(RuntimeError, "changed before rollback"):
                INSTALLER._restore_updated_claude_config(config, b"original\n", expected)
            self.assertEqual(config.read_text(encoding="utf-8"), "concurrent configuration\n")

            owned = root / "owned-artifact"
            owned.write_text("created by updater\n", encoding="utf-8")
            expected = INSTALLER._capture_update_artifact(owned)
            INSTALLER._remove_created_update_artifact(owned, expected)
            self.assertFalse(owned.exists())

            config.write_text("updated by updater\n", encoding="utf-8")
            expected = INSTALLER._capture_update_artifact(config)
            INSTALLER._restore_updated_claude_config(config, b"original\n", expected)
            self.assertEqual(config.read_bytes(), b"original\n")

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

    def test_rollback_preserves_policies_replaced_during_runtime_verification(self) -> None:
        for replaced_name in ("active", "paused"):
            with self.subTest(replaced_name=replaced_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, target, current = self._rollback_repository(root)
                state_path = root / "update-state.json"
                active_path = root / "updater.json"
                paused_path = root / "updater.rollback-paused.json"
                INSTALLER._atomic_json(state_path, {
                    "status": "ok", "revision": current, "previous": target, "checked_at": 1,
                })
                INSTALLER._atomic_json(active_path, {
                    "enabled": True, "interval_hours": 24, "require_signed_commits": False,
                })
                if replaced_name == "paused":
                    INSTALLER._atomic_json(paused_path, {"require_signed_commits": False})
                restart_calls = 0

                def replace_policy_on_first_restart() -> tuple[bool, str]:
                    nonlocal restart_calls
                    restart_calls += 1
                    if restart_calls == 1:
                        path = active_path if replaced_name == "active" else paused_path
                        path.unlink()
                        INSTALLER._atomic_json(path, {
                            "enabled": True,
                            "interval_hours": 9,
                            "require_signed_commits": False,
                        })
                    return True, "ok"

                with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                     mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                     mock.patch.object(INSTALLER, "UPDATE_CONFIG", active_path), \
                     mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", paused_path), \
                     mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                     mock.patch.object(INSTALLER, "SERVICE_COMMAND", root / "missing-service"), \
                     mock.patch.object(INSTALLER, "MCP_HTTP_COMMAND", root / "missing-mcp"), \
                     mock.patch.object(
                         INSTALLER,
                         "TARGETS",
                         {**INSTALLER.TARGETS, "claude": root / "missing-claude"},
                     ), \
                     mock.patch.object(
                         INSTALLER,
                         "_restart_installed_runtimes",
                         side_effect=replace_policy_on_first_restart,
                     ) as restarter, \
                     mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.rollback(), 1)
                self.assertEqual(restarter.call_count, 2)
                scheduler_removal.assert_not_called()
                self.assertEqual(
                    INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(),
                    current,
                )
                replacement_path = active_path if replaced_name == "active" else paused_path
                replacement = INSTALLER.json.loads(
                    replacement_path.read_text(encoding="utf-8")
                )
                self.assertEqual(replacement["interval_hours"], 9)

    def test_rollback_state_exchange_during_verification_restores_forward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, target, current = self._rollback_repository(root)
            state_path = root / "update-state.json"
            active_path = root / "updater.json"
            paused_path = root / "updater.rollback-paused.json"
            INSTALLER._atomic_json(state_path, {
                "status": "ok", "revision": current, "previous": target, "checked_at": 1,
            })
            INSTALLER._atomic_json(active_path, {
                "enabled": True, "interval_hours": 24, "require_signed_commits": False,
            })
            restart_calls = 0

            def replace_state_on_first_restart() -> tuple[bool, str]:
                nonlocal restart_calls
                restart_calls += 1
                if restart_calls == 1:
                    state_path.unlink()
                    INSTALLER._atomic_json(state_path, {
                        "status": "degraded",
                        "revision": current,
                        "previous": target,
                        "checked_at": 777,
                        "runtime_unchanged": True,
                    })
                return True, "ok"

            errors = io.StringIO()
            with mock.patch.object(INSTALLER, "repository_root", return_value=repository), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", state_path), \
                 mock.patch.object(INSTALLER, "UPDATE_CONFIG", active_path), \
                 mock.patch.object(INSTALLER, "UPDATE_PAUSED_CONFIG", paused_path), \
                 mock.patch.object(INSTALLER, "OPERATION_LOCK", root / "operation.lock"), \
                 mock.patch.object(INSTALLER, "SERVICE_COMMAND", root / "missing-service"), \
                 mock.patch.object(INSTALLER, "MCP_HTTP_COMMAND", root / "missing-mcp"), \
                 mock.patch.object(
                     INSTALLER,
                     "TARGETS",
                     {**INSTALLER.TARGETS, "claude": root / "missing-claude"},
                 ), mock.patch.object(
                     INSTALLER,
                     "_restart_installed_runtimes",
                     side_effect=replace_state_on_first_restart,
                 ) as restarter, mock.patch.object(
                     INSTALLER, "remove_scheduler"
                 ) as scheduler_removal, contextlib.redirect_stderr(errors):
                self.assertEqual(INSTALLER.rollback(), 2)
            self.assertEqual(restarter.call_count, 2)
            scheduler_removal.assert_not_called()
            self.assertEqual(
                INSTALLER._run(["git", "rev-parse", "HEAD"], repository).stdout.strip(),
                current,
            )
            self.assertTrue(active_path.exists())
            self.assertFalse(paused_path.exists())
            replacement = INSTALLER.json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(replacement["checked_at"], 777)
            self.assertTrue(replacement["runtime_unchanged"])
            self.assertIn("forward restoration succeeded", errors.getvalue())

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

    def test_update_state_loader_rejects_unsafe_files_and_invalid_schema(self) -> None:
        original = INSTALLER.UPDATE_STATE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            INSTALLER._atomic_json(target, {
                "status": "ok",
                "revision": "a" * 40,
                "previous": "b" * 40,
                "checked_at": 10,
                "auto_update_paused": False,
            })
            linked = root / "linked.json"
            try:
                linked.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links are unavailable: {error}")
            INSTALLER.UPDATE_STATE = linked
            try:
                with self.assertRaisesRegex(RuntimeError, "Unsafe updater state file type"):
                    INSTALLER._load_update_state()

                INSTALLER.UPDATE_STATE = root / "oversized.json"
                INSTALLER.UPDATE_STATE.write_bytes(
                    b" " * (INSTALLER.MAX_UPDATE_STATE_BYTES + 1)
                )
                if os.name != "nt":
                    INSTALLER.UPDATE_STATE.chmod(0o600)
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    INSTALLER._load_update_state()

                invalid_values = (
                    {"status": 1},
                    {"revision": "short"},
                    {"previous": True},
                    {"checked_at": False},
                    {"auto_update_paused": "false"},
                    {"runtime_unchanged": 1},
                    {"claude_plugin": []},
                    {"health_monitor": "ok"},
                )
                INSTALLER.UPDATE_STATE = root / "invalid.json"
                for payload in invalid_values:
                    with self.subTest(payload=payload):
                        INSTALLER._atomic_json(INSTALLER.UPDATE_STATE, payload)
                        with self.assertRaisesRegex(RuntimeError, "Invalid updater state field"):
                            INSTALLER._load_update_state()

                if os.name != "nt":
                    INSTALLER._atomic_json(INSTALLER.UPDATE_STATE, {"status": "ok"})
                    INSTALLER.UPDATE_STATE.chmod(0o644)
                    with self.assertRaisesRegex(RuntimeError, "owner-only"):
                        INSTALLER._load_update_state()
            finally:
                INSTALLER.UPDATE_STATE = original

    def test_update_state_loader_rejects_identity_change_while_reading(self) -> None:
        original = INSTALLER.UPDATE_STATE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.UPDATE_STATE = root / "update-state.json"
            replacement = root / "replacement.json"
            INSTALLER._atomic_json(INSTALLER.UPDATE_STATE, {"status": "ok", "checked_at": 1})
            INSTALLER._atomic_json(replacement, {"status": "degraded", "checked_at": 2})
            opened = INSTALLER.UPDATE_STATE.stat()
            changed = replacement.stat()
            try:
                with mock.patch.object(INSTALLER.os, "fstat", side_effect=(opened, changed)):
                    with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                        INSTALLER._load_update_state()
            finally:
                INSTALLER.UPDATE_STATE = original

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_public_update_paths_reject_linked_state_before_commands(self) -> None:
        originals = (
            INSTALLER.UPDATE_CONFIG,
            INSTALLER.UPDATE_PAUSED_CONFIG,
            INSTALLER.UPDATE_STATE,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            payload = {
                "status": "ok",
                "revision": "a" * 40,
                "previous": "b" * 40,
                "checked_at": 1,
            }
            INSTALLER._atomic_json(target, payload)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "missing-paused.json"
            INSTALLER.UPDATE_STATE = root / "update-state.json"
            INSTALLER.UPDATE_STATE.symlink_to(target)
            INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                "enabled": True,
                "interval_hours": 1,
                "require_signed_commits": False,
            })
            try:
                with mock.patch.object(INSTALLER, "update") as updater, \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("status"), 2)
                    self.assertEqual(INSTALLER.auto_update("run"), 2)
                updater.assert_not_called()

                with mock.patch.object(INSTALLER, "_clean_checkout_revision", return_value="a" * 40), \
                     mock.patch.object(INSTALLER, "_run") as runner, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER._update_unlocked(), 2)
                    self.assertEqual(INSTALLER._rollback_unlocked(), 2)
                runner.assert_not_called()
                self.assertTrue(INSTALLER.UPDATE_STATE.is_symlink())
                self.assertEqual(INSTALLER.json.loads(target.read_text(encoding="utf-8")), payload)
            finally:
                (
                    INSTALLER.UPDATE_CONFIG,
                    INSTALLER.UPDATE_PAUSED_CONFIG,
                    INSTALLER.UPDATE_STATE,
                ) = originals

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

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_auto_update_disable_rejects_linked_policy_before_scheduler_change(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            INSTALLER._atomic_json(target, {
                "enabled": True, "interval_hours": 24, "require_signed_commits": True,
            })
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_CONFIG.symlink_to(target)
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "missing-paused.json"
            try:
                with mock.patch.object(INSTALLER, "remove_scheduler") as scheduler_removal, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("disable"), 2)
                scheduler_removal.assert_not_called()
                self.assertTrue(INSTALLER.UPDATE_CONFIG.is_symlink())
                self.assertTrue(
                    INSTALLER.json.loads(target.read_text(encoding="utf-8"))[
                        "require_signed_commits"
                    ]
                )
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG = originals

    def test_auto_update_disable_preserves_policy_replaced_after_preflight(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "missing-paused.json"
            INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                "enabled": True, "interval_hours": 24, "require_signed_commits": True,
            })

            def replace_policy() -> None:
                INSTALLER.UPDATE_CONFIG.unlink()
                INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                    "enabled": True, "interval_hours": 1, "require_signed_commits": True,
                })

            try:
                with mock.patch.object(
                    INSTALLER, "remove_scheduler", side_effect=replace_policy
                ) as scheduler_removal, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("disable"), 2)
                scheduler_removal.assert_called_once_with()
                replacement = INSTALLER.json.loads(
                    INSTALLER.UPDATE_CONFIG.read_text(encoding="utf-8")
                )
                self.assertEqual(replacement["interval_hours"], 1)
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG = originals

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_auto_update_enable_rejects_linked_paused_policy_before_active_write(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            INSTALLER._atomic_json(target, {"require_signed_commits": True})
            INSTALLER.UPDATE_CONFIG = root / "missing-active.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "updater.rollback-paused.json"
            INSTALLER.UPDATE_PAUSED_CONFIG.symlink_to(target)
            try:
                with mock.patch.object(INSTALLER, "install_scheduler") as scheduler_install, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("enable"), 2)
                scheduler_install.assert_not_called()
                self.assertFalse(INSTALLER.UPDATE_CONFIG.exists())
                self.assertTrue(INSTALLER.UPDATE_PAUSED_CONFIG.is_symlink())
                self.assertTrue(
                    INSTALLER.json.loads(target.read_text(encoding="utf-8"))[
                        "require_signed_commits"
                    ]
                )
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG = originals

    def test_auto_update_enable_preserves_active_policy_replaced_after_preflight(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.UPDATE_CONFIG = root / "updater.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "missing-paused.json"
            INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                "enabled": True, "interval_hours": 24, "require_signed_commits": True,
            })

            def replace_active(_command: str) -> str | None:
                INSTALLER.UPDATE_CONFIG.unlink()
                INSTALLER._atomic_json(INSTALLER.UPDATE_CONFIG, {
                    "enabled": True, "interval_hours": 1, "require_signed_commits": True,
                })
                return None

            try:
                with mock.patch.object(INSTALLER.shutil, "which", side_effect=replace_active), \
                     mock.patch.object(INSTALLER, "install_scheduler") as scheduler_install, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("enable"), 2)
                scheduler_install.assert_not_called()
                replacement = INSTALLER.json.loads(
                    INSTALLER.UPDATE_CONFIG.read_text(encoding="utf-8")
                )
                self.assertEqual(replacement["interval_hours"], 1)
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG = originals

    def test_auto_update_enable_preserves_paused_policy_replaced_after_active_write(self) -> None:
        originals = (INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.UPDATE_CONFIG = root / "missing-active.json"
            INSTALLER.UPDATE_PAUSED_CONFIG = root / "updater.rollback-paused.json"
            INSTALLER._atomic_json(
                INSTALLER.UPDATE_PAUSED_CONFIG, {"require_signed_commits": True}
            )
            atomic_json = INSTALLER._atomic_json

            def replace_paused(path: Path, payload: dict, **kwargs) -> None:
                atomic_json(path, payload, **kwargs)
                INSTALLER.UPDATE_PAUSED_CONFIG.unlink()
                atomic_json(
                    INSTALLER.UPDATE_PAUSED_CONFIG,
                    {"require_signed_commits": True, "interval_hours": 1},
                )

            try:
                with mock.patch.object(INSTALLER, "_atomic_json", side_effect=replace_paused), \
                     mock.patch.object(INSTALLER, "install_scheduler") as scheduler_install, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.auto_update("enable"), 2)
                scheduler_install.assert_not_called()
                replacement = INSTALLER.json.loads(
                    INSTALLER.UPDATE_PAUSED_CONFIG.read_text(encoding="utf-8")
                )
                self.assertEqual(replacement["interval_hours"], 1)
            finally:
                INSTALLER.UPDATE_CONFIG, INSTALLER.UPDATE_PAUSED_CONFIG = originals

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

    def test_update_state_exchange_during_candidate_preflight_blocks_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, active, old, _candidate = self._update_repository_pair(root)
            claude_skill = root / "claude-skill"
            claude_skill.symlink_to(active / "translate-native", target_is_directory=True)
            update_state = root / "update-state.json"
            INSTALLER._atomic_json(update_state, {
                "status": "ok", "revision": old, "previous": old, "checked_at": 1,
            })

            def replace_state(*_arguments: object) -> dict:
                update_state.unlink()
                INSTALLER._atomic_json(update_state, {
                    "status": "degraded",
                    "revision": old,
                    "previous": old,
                    "checked_at": 999,
                    "runtime_unchanged": True,
                })
                return {"ready": True}

            errors = io.StringIO()
            with mock.patch.object(INSTALLER, "repository_root", return_value=active), \
                 mock.patch.object(INSTALLER, "REPO_URL", str(upstream)), \
                 mock.patch.object(INSTALLER, "UPDATE_STATE", update_state), \
                 mock.patch.object(INSTALLER, "HEALTH_CONFIG", root / "missing-health-config.json"), \
                 mock.patch.object(INSTALLER, "HEALTH_STATE", root / "missing-health-state.json"), \
                 mock.patch.object(
                     INSTALLER,
                     "TARGETS",
                     {**INSTALLER.TARGETS, "claude": claude_skill},
                 ), mock.patch.object(
                     INSTALLER, "preflight_claude_plugin_update", side_effect=replace_state
                 ), mock.patch.object(INSTALLER, "restart_guard_runtime") as guard_restart, \
                 contextlib.redirect_stderr(errors):
                self.assertEqual(INSTALLER._update_unlocked(), 2)
            self.assertEqual(
                INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip(),
                old,
            )
            self.assertFalse((active / "new-runtime.txt").exists())
            replacement = INSTALLER.json.loads(update_state.read_text(encoding="utf-8"))
            self.assertEqual(replacement["checked_at"], 999)
            self.assertTrue(replacement["runtime_unchanged"])
            self.assertIn("activation is blocked", errors.getvalue())
            guard_restart.assert_not_called()

    def test_update_health_publication_preserves_exchanged_policy_and_state(self) -> None:
        for exchanged in ("policy", "state"):
            with self.subTest(exchanged=exchanged), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                upstream, active, _old, candidate = self._update_repository_pair(root)
                claude_skill = root / "claude-skill"
                claude_skill.symlink_to(active / "translate-native", target_is_directory=True)
                health_config = root / "health-monitor.json"
                health_state = root / "health-state.json"
                update_state = root / "update-state.json"
                INSTALLER._atomic_json(health_config, {
                    "enabled": True,
                    "interval_seconds": 60,
                    "plugin_required": True,
                })
                INSTALLER._atomic_json(health_state, {
                    "status": "ok",
                    "checked_at": 1,
                    "consecutive_failures": 0,
                })

                def install_and_exchange() -> tuple[bool, str]:
                    target = health_config if exchanged == "policy" else health_state
                    target.unlink()
                    if exchanged == "policy":
                        INSTALLER._atomic_json(target, {
                            "enabled": False,
                            "interval_seconds": 300,
                            "plugin_required": False,
                            "claude_command": "replacement",
                        })
                    else:
                        INSTALLER._atomic_json(target, {
                            "status": "replacement",
                            "checked_at": 999,
                            "consecutive_failures": 7,
                            "next_repair_at": 9999,
                        })
                    return True, "test schedule"

                errors = io.StringIO()
                with contextlib.ExitStack() as stack:
                    for name, value in (
                        ("repository_root", mock.Mock(return_value=active)),
                        ("REPO_URL", str(upstream)),
                        ("UPDATE_STATE", update_state),
                        ("HEALTH_CONFIG", health_config),
                        ("HEALTH_STATE", health_state),
                        ("MCP_HTTP_COMMAND", root / "missing-mcp"),
                        ("MCP_HEADERS_COMMAND", root / "missing-headers"),
                        ("MCP_HTTP_TOKEN", root / "missing-token"),
                        ("CLAUDE_CONFIG", root / "missing-claude.json"),
                        ("SERVICE_COMMAND", root / "missing-service"),
                        ("TARGETS", {**INSTALLER.TARGETS, "claude": claude_skill}),
                    ):
                        stack.enter_context(mock.patch.object(INSTALLER, name, value))
                    stack.enter_context(mock.patch.object(
                        INSTALLER, "preflight_claude_plugin_update", return_value={"ready": True}
                    ))
                    stack.enter_context(mock.patch.object(INSTALLER, "install_mcp_http_runtime"))
                    stack.enter_context(mock.patch.object(
                        INSTALLER, "install_mcp_http_autostart", return_value=(True, "test")
                    ))
                    stack.enter_context(mock.patch.object(
                        INSTALLER, "configure_claude_mcp", return_value=(None, [])
                    ))
                    stack.enter_context(mock.patch.object(
                        INSTALLER, "_capture_update_artifact", return_value=(1, 1, 1, 1, 1, 1, 1)
                    ))
                    stack.enter_context(mock.patch.object(
                        INSTALLER, "install_health_monitor", side_effect=install_and_exchange
                    ))
                    stack.enter_context(mock.patch.object(
                        INSTALLER, "_guard_stack_status", return_value=(True, True)
                    ))
                    remove_monitor = stack.enter_context(mock.patch.object(
                        INSTALLER, "remove_health_monitor"
                    ))
                    plugin_update = stack.enter_context(mock.patch.object(
                        INSTALLER, "_apply_claude_plugin_update"
                    ))
                    stack.enter_context(contextlib.redirect_stderr(errors))
                    self.assertEqual(INSTALLER._update_unlocked(), 1)

                plugin_update.assert_not_called()
                self.assertEqual(
                    INSTALLER._run(["git", "rev-parse", "HEAD"], active).stdout.strip(),
                    candidate,
                )
                recorded = INSTALLER.json.loads(update_state.read_text(encoding="utf-8"))
                self.assertEqual(recorded["status"], "degraded")
                self.assertEqual(
                    recorded["health_monitor"]["detail"],
                    "protected-health-state-changed",
                )
                self.assertIn("replacement was preserved", errors.getvalue())
                policy = INSTALLER.json.loads(health_config.read_text(encoding="utf-8"))
                state = INSTALLER.json.loads(health_state.read_text(encoding="utf-8"))
                if exchanged == "policy":
                    self.assertFalse(policy["enabled"])
                    self.assertEqual(policy["claude_command"], "replacement")
                    self.assertEqual(state["checked_at"], 1)
                    remove_monitor.assert_called_once_with()
                else:
                    self.assertTrue(policy["enabled"])
                    self.assertEqual(state["status"], "replacement")
                    self.assertEqual(state["next_repair_at"], 9999)
                    remove_monitor.assert_not_called()

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

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_health_monitor_remove_rejects_unsafe_files_before_schedule_change(self) -> None:
        originals = (INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "sentinel.json"
            INSTALLER._atomic_json(sentinel, {"enabled": True})
            try:
                for unsafe_name in ("health-config.json", "health-state.json"):
                    with self.subTest(unsafe_name=unsafe_name):
                        config = root / "health-config.json"
                        state = root / "health-state.json"
                        config.unlink(missing_ok=True)
                        state.unlink(missing_ok=True)
                        INSTALLER.HEALTH_CONFIG = config
                        INSTALLER.HEALTH_STATE = state
                        (config if unsafe_name == config.name else state).symlink_to(sentinel)
                        with mock.patch.object(INSTALLER, "remove_health_monitor") as remover, \
                             contextlib.redirect_stderr(io.StringIO()):
                            self.assertEqual(INSTALLER.health_monitor("remove"), 2)
                        remover.assert_not_called()
                        self.assertTrue((config if unsafe_name == config.name else state).is_symlink())
                        self.assertTrue(
                            INSTALLER.json.loads(sentinel.read_text(encoding="utf-8"))["enabled"]
                        )
            finally:
                INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE = originals

    def test_health_monitor_remove_preserves_state_replaced_after_preflight(self) -> None:
        originals = (INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {"status": "ok", "checked_at": 1})

            def replace_state() -> None:
                INSTALLER.HEALTH_STATE.unlink()
                INSTALLER._atomic_json(
                    INSTALLER.HEALTH_STATE, {"status": "replacement", "checked_at": 2}
                )

            try:
                with mock.patch.object(
                    INSTALLER, "remove_health_monitor", side_effect=replace_state
                ) as remover, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor("remove"), 2)
                remover.assert_called_once_with()
                replacement = INSTALLER.json.loads(
                    INSTALLER.HEALTH_STATE.read_text(encoding="utf-8")
                )
                self.assertEqual(replacement["status"], "replacement")
                policy = INSTALLER.json.loads(
                    INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8")
                )
                self.assertFalse(policy["enabled"])
            finally:
                INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE = originals

    def test_health_monitor_install_persists_the_inspected_policy(self) -> None:
        original = INSTALLER.HEALTH_CONFIG
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.HEALTH_CONFIG = Path(directory) / "health-config.json"
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                "enabled": False, "plugin_required": True, "claude_command": "/bin/claude",
            })
            try:
                with mock.patch.object(INSTALLER, "health_monitor_run", return_value=0), \
                     mock.patch.object(
                         INSTALLER, "install_health_monitor", return_value=(True, "test schedule")
                     ), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor("install"), 0)
                policy = INSTALLER.json.loads(
                    INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8")
                )
                self.assertTrue(policy["enabled"])
                self.assertTrue(policy["plugin_required"])
                self.assertEqual(policy["interval_seconds"], 60)
                self.assertEqual(policy["claude_command"], "/bin/claude")
            finally:
                INSTALLER.HEALTH_CONFIG = original

    def test_health_monitor_install_blocks_policy_exchange_before_scheduler_change(self) -> None:
        original = INSTALLER.HEALTH_CONFIG
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.HEALTH_CONFIG = Path(directory) / "health-config.json"
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {"enabled": False})

            def replace_policy(_config: dict | None = None) -> str:
                INSTALLER.HEALTH_CONFIG.unlink()
                INSTALLER._atomic_json(
                    INSTALLER.HEALTH_CONFIG, {"enabled": False, "claude_command": "replacement"}
                )
                return "/bin/claude"

            try:
                with mock.patch.object(INSTALLER, "health_monitor_run", return_value=0), \
                     mock.patch.object(
                         INSTALLER, "_configured_claude_command", side_effect=replace_policy
                     ), mock.patch.object(INSTALLER, "install_health_monitor") as installer, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor("install"), 2)
                installer.assert_not_called()
                policy = INSTALLER.json.loads(
                    INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8")
                )
                self.assertEqual(policy["claude_command"], "replacement")
            finally:
                INSTALLER.HEALTH_CONFIG = original

    def test_health_monitor_install_rolls_back_after_policy_exchange(self) -> None:
        original = INSTALLER.HEALTH_CONFIG
        with tempfile.TemporaryDirectory() as directory:
            INSTALLER.HEALTH_CONFIG = Path(directory) / "health-config.json"
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {"enabled": False})

            def install_and_replace() -> tuple[bool, str]:
                INSTALLER.HEALTH_CONFIG.unlink()
                INSTALLER._atomic_json(
                    INSTALLER.HEALTH_CONFIG, {"enabled": False, "claude_command": "replacement"}
                )
                return True, "test schedule"

            try:
                with mock.patch.object(INSTALLER, "health_monitor_run", return_value=0), \
                     mock.patch.object(
                         INSTALLER, "_configured_claude_command", return_value="/bin/claude"
                     ), mock.patch.object(
                         INSTALLER, "install_health_monitor", side_effect=install_and_replace
                     ) as installer, mock.patch.object(
                         INSTALLER, "remove_health_monitor"
                     ) as remover, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor("install"), 2)
                installer.assert_called_once_with()
                remover.assert_called_once_with()
                policy = INSTALLER.json.loads(
                    INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8")
                )
                self.assertEqual(policy["claude_command"], "replacement")
            finally:
                INSTALLER.HEALTH_CONFIG = original

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

    @unittest.skipIf(os.name == "nt", "POSIX hard-link test")
    def test_signing_key_rejects_hard_links_during_install_and_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "signing.key"
            alias = root / "signing-key-alias"
            key.write_bytes(b"k" * 32)
            key.chmod(0o600)
            os.link(key, alias)

            with self.assertRaisesRegex(RuntimeError, "exactly one hard link"):
                INSTALLER.ensure_signing_key(key)
            with self.assertRaisesRegex(RuntimeError, "exactly one hard link"):
                INSTALLER._inspect_protected_signing_key(key)
            self.assertEqual(alias.read_bytes(), b"k" * 32)

            key.unlink()
            alias.unlink()

            def link_during_creation(_descriptor: int) -> None:
                os.link(key, alias)

            with mock.patch.object(INSTALLER.os, "fsync", side_effect=link_during_creation):
                with self.assertRaisesRegex(RuntimeError, "exactly one hard link"):
                    INSTALLER.ensure_signing_key(key)
            self.assertEqual(alias.read_bytes(), key.read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX directory safety test")
    def test_signing_key_rejects_unsafe_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "safely open"):
                INSTALLER.ensure_signing_key(linked / "signing.key")
            self.assertFalse((target / "signing.key").exists())

            writable = root / "writable"
            writable.mkdir()
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                    INSTALLER.ensure_signing_key(writable / "signing.key")
                self.assertFalse((writable / "signing.key").exists())
            finally:
                writable.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_signing_key_parent_exchange_during_creation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "guard"
            trusted.mkdir()
            replacement = root / "replacement"
            replacement.mkdir()
            key = trusted / "signing.key"
            original_open = INSTALLER._open_signing_key_file

            def exchange_parent(
                path: Path, directory_handle: int | None, flags: int, mode: int,
            ) -> int:
                trusted.rename(root / "guard-old")
                replacement.rename(trusted)
                return original_open(path, directory_handle, flags, mode)

            with mock.patch.object(
                INSTALLER,
                "_open_signing_key_file",
                side_effect=exchange_parent,
            ):
                with self.assertRaisesRegex(RuntimeError, "directory changed"):
                    INSTALLER.ensure_signing_key(key)

            self.assertFalse(key.exists())
            self.assertEqual((root / "guard-old" / "signing.key").stat().st_size, 32)

    @unittest.skipIf(os.name == "nt", "POSIX directory safety test")
    def test_signing_key_inspection_rejects_unsafe_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            target_key = target / "signing.key"
            target_key.write_bytes(b"a" * 32)
            target_key.chmod(0o600)
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "safely open"):
                INSTALLER._inspect_protected_signing_key(linked / "signing.key")

            writable = root / "writable"
            writable.mkdir()
            writable_key = writable / "signing.key"
            writable_key.write_bytes(b"b" * 32)
            writable_key.chmod(0o600)
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                    INSTALLER._inspect_protected_signing_key(writable_key)
            finally:
                writable.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_signing_key_inspection_detects_parent_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "guard"
            trusted.mkdir()
            key = trusted / "signing.key"
            key.write_bytes(b"a" * 32)
            key.chmod(0o600)
            replacement = root / "replacement"
            replacement.mkdir()
            replacement_key = replacement / "signing.key"
            replacement_key.write_bytes(b"b" * 32)
            replacement_key.chmod(0o600)
            original_lstat = INSTALLER._signing_key_lstat

            def exchange_parent(
                path: Path, directory_handle: int | None,
            ) -> os.stat_result:
                details = original_lstat(path, directory_handle)
                trusted.rename(root / "guard-old")
                replacement.rename(trusted)
                return details

            with mock.patch.object(
                INSTALLER,
                "_signing_key_lstat",
                side_effect=exchange_parent,
            ):
                with self.assertRaisesRegex(RuntimeError, "directory changed"):
                    INSTALLER._inspect_protected_signing_key(key)

            self.assertEqual(key.read_bytes(), b"b" * 32)
            self.assertEqual((root / "guard-old" / "signing.key").read_bytes(), b"a" * 32)

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

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_install_delivery_boundary_rejects_unsafe_policy_before_mutation(self) -> None:
        originals = (
            INSTALLER.DELIVERY_COMMAND,
            INSTALLER.DELIVERY_POLICY,
            INSTALLER.SIGNING_KEY,
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            sentinel = temporary / "sentinel.json"
            sentinel.write_text('{"do_not_replace": true}\n', encoding="utf-8")
            sentinel.chmod(0o600)
            INSTALLER.DELIVERY_COMMAND = temporary / "bin" / "blun-language-deliver"
            INSTALLER.DELIVERY_POLICY = temporary / "config" / "delivery-policy.json"
            INSTALLER.DELIVERY_POLICY.parent.mkdir(parents=True)
            INSTALLER.DELIVERY_POLICY.symlink_to(sentinel)
            INSTALLER.SIGNING_KEY = temporary / "config" / "signing.key"
            try:
                with mock.patch.object(INSTALLER, "atomic_symlink") as command_install, \
                     mock.patch.object(INSTALLER, "ensure_signing_key") as key_install:
                    with self.assertRaisesRegex(RuntimeError, "file type"):
                        INSTALLER.install_delivery_boundary(ROOT)
                command_install.assert_not_called()
                key_install.assert_not_called()
                self.assertEqual(
                    sentinel.read_text(encoding="utf-8"),
                    '{"do_not_replace": true}\n',
                )
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

    @unittest.skipIf(os.name == "nt", "POSIX directory safety test")
    def test_service_token_rejects_unsafe_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "safely open"):
                INSTALLER.ensure_service_token(linked / "service.token")
            self.assertFalse((target / "service.token").exists())

            writable = root / "writable"
            writable.mkdir()
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                    INSTALLER.ensure_service_token(writable / "service.token")
                self.assertFalse((writable / "service.token").exists())
            finally:
                writable.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_service_token_parent_exchange_during_creation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "guard"
            trusted.mkdir()
            replacement = root / "replacement"
            replacement.mkdir()
            token = trusted / "service.token"
            original_open = INSTALLER._open_service_token_file

            def exchange_parent(
                path: Path,
                directory_handle: int | None,
                flags: int,
                mode: int | None = None,
            ) -> int:
                trusted.rename(root / "guard-old")
                replacement.rename(trusted)
                return original_open(path, directory_handle, flags, mode)

            with mock.patch.object(
                INSTALLER,
                "_open_service_token_file",
                side_effect=exchange_parent,
            ):
                with self.assertRaisesRegex(RuntimeError, "directory changed"):
                    INSTALLER.ensure_service_token(token)

            self.assertFalse(token.exists())
            old_token = root / "guard-old" / "service.token"
            self.assertGreaterEqual(len(old_token.read_text(encoding="ascii").strip()), 32)

    @unittest.skipIf(os.name == "nt", "POSIX directory safety test")
    def test_service_token_reader_rejects_unsafe_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            target_token = target / "service.token"
            target_token.write_text("a" * 64 + "\n", encoding="ascii")
            target_token.chmod(0o600)
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "safely open"):
                INSTALLER._read_protected_service_token(linked / "service.token")

            writable = root / "writable"
            writable.mkdir()
            writable_token = writable / "service.token"
            writable_token.write_text("b" * 64 + "\n", encoding="ascii")
            writable_token.chmod(0o600)
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                    INSTALLER._read_protected_service_token(writable_token)
            finally:
                writable.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_service_token_reader_detects_parent_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "guard"
            trusted.mkdir()
            token = trusted / "service.token"
            token.write_text("a" * 64 + "\n", encoding="ascii")
            token.chmod(0o600)
            replacement = root / "replacement"
            replacement.mkdir()
            replacement_token = replacement / "service.token"
            replacement_token.write_text("b" * 64 + "\n", encoding="ascii")
            replacement_token.chmod(0o600)
            original_open = INSTALLER._open_service_token_file

            def exchange_parent(
                path: Path,
                directory_handle: int | None,
                flags: int,
                mode: int | None = None,
            ) -> int:
                trusted.rename(root / "guard-old")
                replacement.rename(trusted)
                return original_open(path, directory_handle, flags, mode)

            with mock.patch.object(
                INSTALLER,
                "_open_service_token_file",
                side_effect=exchange_parent,
            ):
                with self.assertRaisesRegex(RuntimeError, "directory changed"):
                    INSTALLER._read_protected_service_token(token)

            self.assertEqual(token.read_text(encoding="ascii"), "b" * 64 + "\n")
            self.assertEqual(
                (root / "guard-old" / "service.token").read_text(encoding="ascii"),
                "a" * 64 + "\n",
            )

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

    def test_mcp_http_token_creation_rejects_links_and_legacy_temporary_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "mcp-http.token"
            sentinel = root / "sentinel"
            sentinel.write_text("s" * 64 + "\n", encoding="ascii")
            legacy_temporary = token.with_suffix(".tmp")
            try:
                legacy_temporary.symlink_to(sentinel)
            except OSError as error:
                self.skipTest(f"symbolic links are unavailable: {error}")

            INSTALLER.ensure_mcp_http_token(token)
            self.assertEqual(sentinel.read_text(encoding="ascii"), "s" * 64 + "\n")
            self.assertTrue(legacy_temporary.is_symlink())
            self.assertFalse(token.is_symlink())
            self.assertGreaterEqual(len(INSTALLER._read_protected_mcp_http_token(token)), 32)

            token.unlink()
            token.symlink_to(sentinel)
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                INSTALLER.ensure_mcp_http_token(token)
            self.assertEqual(sentinel.read_text(encoding="ascii"), "s" * 64 + "\n")

    @unittest.skipIf(os.name == "nt", "POSIX MCP-token hard-link test")
    def test_mcp_http_token_creation_and_reader_reject_hard_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "mcp-http.token"
            token.write_text("h" * 64 + "\n", encoding="ascii")
            token.chmod(0o600)
            alias = root / "mcp-http-token-alias"
            os.link(token, alias)

            for consumer in (
                INSTALLER.ensure_mcp_http_token,
                INSTALLER._read_protected_mcp_http_token,
            ):
                with self.subTest(consumer=consumer.__name__):
                    with self.assertRaisesRegex(RuntimeError, "additional hard links"):
                        consumer(token)

    @unittest.skipIf(os.name == "nt", "POSIX directory safety test")
    def test_mcp_http_token_rejects_unsafe_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "safely open"):
                INSTALLER.ensure_mcp_http_token(linked / "mcp-http.token")
            self.assertFalse((target / "mcp-http.token").exists())

            writable = root / "writable"
            writable.mkdir()
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                    INSTALLER.ensure_mcp_http_token(writable / "mcp-http.token")
                self.assertFalse((writable / "mcp-http.token").exists())
            finally:
                writable.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_mcp_http_token_parent_exchange_during_creation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "guard"
            trusted.mkdir()
            replacement = root / "replacement"
            replacement.mkdir()
            token = trusted / "mcp-http.token"
            original_open = INSTALLER._open_mcp_http_token_file

            def exchange_parent(
                path: Path,
                directory_handle: int | None,
                flags: int,
                mode: int | None = None,
            ) -> int:
                trusted.rename(root / "guard-old")
                replacement.rename(trusted)
                return original_open(path, directory_handle, flags, mode)

            with mock.patch.object(
                INSTALLER,
                "_open_mcp_http_token_file",
                side_effect=exchange_parent,
            ):
                with self.assertRaisesRegex(RuntimeError, "directory changed"):
                    INSTALLER.ensure_mcp_http_token(token)

            self.assertFalse(token.exists())
            old_token = root / "guard-old" / "mcp-http.token"
            self.assertGreaterEqual(len(old_token.read_text(encoding="ascii").strip()), 32)

    def test_installer_mcp_http_token_reader_is_bounded_and_identity_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "mcp-http.token"
            token.write_bytes(b"x" * (INSTALLER.MAX_MCP_HTTP_TOKEN_BYTES + 1))
            if os.name != "nt":
                token.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "invalid size"):
                INSTALLER._read_protected_mcp_http_token(token)

            token.write_text("a" * 64 + "\n", encoding="ascii")
            replacement = root / "replacement.token"
            replacement.write_text("b" * 64 + "\n", encoding="ascii")
            if os.name != "nt":
                token.chmod(0o600)
                replacement.chmod(0o600)
            opened = token.stat()
            changed = replacement.stat()
            with mock.patch.object(INSTALLER.os, "fstat", side_effect=(opened, changed)):
                with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                    INSTALLER._read_protected_mcp_http_token_at(token, None)

    @unittest.skipIf(os.name == "nt", "POSIX directory safety test")
    def test_mcp_http_token_reader_rejects_unsafe_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            target_token = target / "mcp-http.token"
            target_token.write_text("a" * 64 + "\n", encoding="ascii")
            target_token.chmod(0o600)
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "safely open"):
                INSTALLER._read_protected_mcp_http_token(linked / "mcp-http.token")

            writable = root / "writable"
            writable.mkdir()
            writable_token = writable / "mcp-http.token"
            writable_token.write_text("b" * 64 + "\n", encoding="ascii")
            writable_token.chmod(0o600)
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside its owner"):
                    INSTALLER._read_protected_mcp_http_token(writable_token)
            finally:
                writable.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX directory identity test")
    def test_mcp_http_token_reader_detects_parent_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "guard"
            trusted.mkdir()
            token = trusted / "mcp-http.token"
            token.write_text("a" * 64 + "\n", encoding="ascii")
            token.chmod(0o600)
            replacement = root / "replacement"
            replacement.mkdir()
            replacement_token = replacement / "mcp-http.token"
            replacement_token.write_text("b" * 64 + "\n", encoding="ascii")
            replacement_token.chmod(0o600)
            original_open = INSTALLER._open_mcp_http_token_file

            def exchange_parent(
                path: Path,
                directory_handle: int | None,
                flags: int,
                mode: int | None = None,
            ) -> int:
                trusted.rename(root / "guard-old")
                replacement.rename(trusted)
                return original_open(path, directory_handle, flags, mode)

            with mock.patch.object(
                INSTALLER,
                "_open_mcp_http_token_file",
                side_effect=exchange_parent,
            ):
                with self.assertRaisesRegex(RuntimeError, "directory changed"):
                    INSTALLER._read_protected_mcp_http_token(token)

            self.assertEqual(token.read_text(encoding="ascii"), "b" * 64 + "\n")
            self.assertEqual(
                (root / "guard-old" / "mcp-http.token").read_text(encoding="ascii"),
                "a" * 64 + "\n",
            )

    @unittest.skipIf(os.name == "nt", "POSIX link test")
    def test_mcp_probe_rejects_linked_access_token_before_network(self) -> None:
        original = INSTALLER.MCP_HTTP_TOKEN
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.token"
            target.write_text("known-token-" + "x" * 32 + "\n", encoding="ascii")
            target.chmod(0o600)
            INSTALLER.MCP_HTTP_TOKEN = root / "mcp-http.token"
            INSTALLER.MCP_HTTP_TOKEN.symlink_to(target)
            try:
                with mock.patch.object(INSTALLER.urllib.request, "urlopen") as opener:
                    with self.assertRaisesRegex(RuntimeError, "regular file"):
                        INSTALLER._mcp_http_request("/healthz")
                opener.assert_not_called()
            finally:
                INSTALLER.MCP_HTTP_TOKEN = original

    def test_mcp_runtime_rejects_linked_token_before_command_mutation(self) -> None:
        original = INSTALLER.MCP_HTTP_TOKEN
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.token"
            target.write_text("known-token-" + "x" * 32 + "\n", encoding="ascii")
            if os.name != "nt":
                target.chmod(0o600)
            INSTALLER.MCP_HTTP_TOKEN = root / "mcp-http.token"
            try:
                INSTALLER.MCP_HTTP_TOKEN.symlink_to(target)
            except OSError as error:
                INSTALLER.MCP_HTTP_TOKEN = original
                self.skipTest(f"symbolic links are unavailable: {error}")
            try:
                with mock.patch.object(INSTALLER, "atomic_symlink") as link:
                    with self.assertRaisesRegex(RuntimeError, "regular file"):
                        INSTALLER.install_mcp_http_runtime(ROOT)
                link.assert_not_called()
                self.assertTrue(INSTALLER.MCP_HTTP_TOKEN.is_symlink())
                self.assertEqual(target.read_text(encoding="ascii"), "known-token-" + "x" * 32 + "\n")
            finally:
                INSTALLER.MCP_HTTP_TOKEN = original

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
                    self.assertEqual(backup.stat().st_mode & 0o077, 0)
            finally:
                INSTALLER.MCP_HEADERS_COMMAND = original_headers

    def test_claude_configuration_refuses_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".claude.json"
            config.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "configuration is invalid"):
                INSTALLER.configure_claude_mcp(config)
            self.assertEqual(config.read_text(encoding="utf-8"), "{broken")

    @unittest.skipIf(os.name == "nt", "POSIX file-type and permission tests")
    def test_claude_configuration_rejects_unsafe_files_without_changing_targets(self) -> None:
        cases = ("symlink", "hardlink", "fifo", "permissions", "oversized")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / ".claude.json"
                sentinel = root / "sentinel.json"
                sentinel.write_text('{"mcpServers": {}}\n', encoding="utf-8")
                sentinel.chmod(0o600)
                if case == "symlink":
                    config.symlink_to(sentinel)
                    expected = "regular file"
                elif case == "hardlink":
                    os.link(sentinel, config)
                    expected = "hard links"
                elif case == "fifo":
                    os.mkfifo(config)
                    expected = "regular file"
                elif case == "permissions":
                    config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
                    config.chmod(0o622)
                    expected = "writable outside"
                else:
                    config.write_bytes(b"x" * (INSTALLER.MAX_CLAUDE_CONFIG_BYTES + 1))
                    config.chmod(0o600)
                    expected = "size limit"
                with self.assertRaisesRegex(RuntimeError, expected):
                    INSTALLER.configure_claude_mcp(config)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')
                self.assertFalse(config.with_suffix(".json.bak").exists())

    def test_claude_configuration_rejects_identity_exchange_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".claude.json"
            config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            if os.name != "nt":
                config.chmod(0o600)
            details = config.stat()
            fields = {
                name: getattr(details, name)
                for name in (
                    "st_mode", "st_uid", "st_dev", "st_ino", "st_nlink", "st_size",
                    "st_ctime_ns", "st_mtime_ns",
                )
            }
            opened = SimpleNamespace(**fields)
            changed = SimpleNamespace(**fields)
            changed.st_ctime_ns += 1
            with mock.patch.object(INSTALLER.os, "fstat", side_effect=(opened, changed)):
                with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                    INSTALLER.configure_claude_mcp(config)
            self.assertEqual(config.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')
            self.assertFalse(config.with_suffix(".json.bak").exists())

    def test_claude_configuration_preserves_concurrent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".claude.json"
            config.write_text('{"theme": "original", "mcpServers": {}}\n', encoding="utf-8")
            if os.name != "nt":
                config.chmod(0o600)
            real_atomic_bytes = INSTALLER._atomic_bytes

            def exchange_before_target_replace(path: Path, payload: bytes, *, before_replace=None) -> None:
                if path != config:
                    real_atomic_bytes(path, payload, before_replace=before_replace)
                    return

                def exchange_then_recheck() -> None:
                    replacement = root / "concurrent.json"
                    replacement.write_text(
                        '{"theme": "concurrent", "mcpServers": {}}\n',
                        encoding="utf-8",
                    )
                    if os.name != "nt":
                        replacement.chmod(0o600)
                    os.replace(replacement, config)
                    assert before_replace is not None
                    before_replace()

                real_atomic_bytes(path, payload, before_replace=exchange_then_recheck)

            with mock.patch.object(
                INSTALLER, "_atomic_bytes", side_effect=exchange_before_target_replace
            ):
                with self.assertRaisesRegex(RuntimeError, "changed before replacement"):
                    INSTALLER.configure_claude_mcp(config)
            self.assertEqual(
                config.read_text(encoding="utf-8"),
                '{"theme": "concurrent", "mcpServers": {}}\n',
            )

    @unittest.skipIf(os.name == "nt", "POSIX symbolic-link test")
    def test_claude_install_preflights_config_before_runtime_mutation(self) -> None:
        original = INSTALLER.CLAUDE_CONFIG
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "sentinel.json"
            sentinel.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            sentinel.chmod(0o600)
            INSTALLER.CLAUDE_CONFIG = root / ".claude.json"
            INSTALLER.CLAUDE_CONFIG.symlink_to(sentinel)
            try:
                with mock.patch.object(INSTALLER, "atomic_symlink") as link, \
                     mock.patch.object(INSTALLER, "install_delivery_boundary") as delivery, \
                     mock.patch.object(INSTALLER, "install_guard_runtime") as runtime, \
                     mock.patch.object(INSTALLER, "install_mcp_http_runtime") as mcp_runtime:
                    with self.assertRaisesRegex(RuntimeError, "regular file"):
                        INSTALLER.install(["claude"], autostart_service=False)
                link.assert_not_called()
                delivery.assert_not_called()
                runtime.assert_not_called()
                mcp_runtime.assert_not_called()
                self.assertTrue(INSTALLER.CLAUDE_CONFIG.is_symlink())
                self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')
            finally:
                INSTALLER.CLAUDE_CONFIG = original

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

    def test_project_mcp_shadow_scan_rejects_invalid_json_and_schema(self) -> None:
        cases = (b"{broken", b"[]", b'{"mcpServers": []}')
        for raw in cases:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                nested = root / "nested"
                nested.mkdir()
                config = root / ".mcp.json"
                config.write_bytes(raw)
                with self.assertRaisesRegex(RuntimeError, "invalid|root must|must be an object"):
                    INSTALLER.project_mcp_shadows(nested)

    @unittest.skipIf(os.name == "nt", "POSIX file-type and permission tests")
    def test_project_mcp_shadow_scan_rejects_unsafe_files(self) -> None:
        cases = ("symlink", "hardlink", "fifo", "permissions", "oversized")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                nested = root / "nested"
                nested.mkdir()
                config = root / ".mcp.json"
                sentinel = root / "sentinel.json"
                sentinel.write_text('{"mcpServers": {}}\n', encoding="utf-8")
                sentinel.chmod(0o600)
                if case == "symlink":
                    config.symlink_to(sentinel)
                    expected = "regular file"
                elif case == "hardlink":
                    os.link(sentinel, config)
                    expected = "hard links"
                elif case == "fifo":
                    os.mkfifo(config)
                    expected = "regular file"
                elif case == "permissions":
                    config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
                    config.chmod(0o622)
                    expected = "writable outside"
                else:
                    config.write_bytes(b"x" * (INSTALLER.MAX_PROJECT_MCP_CONFIG_BYTES + 1))
                    config.chmod(0o600)
                    expected = "size limit"
                with self.assertRaisesRegex(RuntimeError, expected):
                    INSTALLER.project_mcp_shadows(nested)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')

    def test_project_mcp_shadow_scan_rejects_identity_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            config = root / ".mcp.json"
            config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            if os.name != "nt":
                config.chmod(0o600)
            details = config.stat()
            fields = {
                name: getattr(details, name)
                for name in (
                    "st_mode", "st_uid", "st_dev", "st_ino", "st_nlink", "st_size",
                    "st_ctime_ns", "st_mtime_ns",
                )
            }
            opened = SimpleNamespace(**fields)
            changed = SimpleNamespace(**fields)
            changed.st_ctime_ns += 1
            with mock.patch.object(INSTALLER.os, "fstat", side_effect=(opened, changed)):
                with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                    INSTALLER.project_mcp_shadows(nested)

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
            if os.name != "nt":
                INSTALLER.OPERATION_LOCK.chmod(0o600)
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

    @unittest.skipUnless(os.name != "nt", "POSIX ownership and link semantics")
    def test_unsafe_operation_lock_paths_block_without_removal(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "operation.lock"
            sentinel = root / "sentinel"
            payload = b'{"operation":"update","pid":999999,"started_at":1,"token":"' + b"a" * 32 + b'"}\n'
            sentinel.write_bytes(payload)
            sentinel.chmod(0o600)
            try:
                for unsafe in ("symlink", "hardlink", "permissions"):
                    lock.unlink(missing_ok=True)
                    if unsafe == "symlink":
                        lock.symlink_to(sentinel)
                    elif unsafe == "hardlink":
                        os.link(sentinel, lock)
                    else:
                        lock.write_bytes(payload)
                        lock.chmod(0o644)
                    os.utime(lock, (100, 100), follow_symlinks=False)
                    INSTALLER.OPERATION_LOCK = lock

                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor",
                        now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1,
                    )

                    self.assertIsNone(token, unsafe)
                    self.assertTrue(lock.exists() or lock.is_symlink(), unsafe)
                    self.assertEqual(sentinel.read_bytes(), payload, unsafe)
                    if unsafe == "symlink":
                        self.assertTrue(lock.is_symlink())
                    elif unsafe == "hardlink":
                        self.assertEqual(lock.stat().st_ino, sentinel.stat().st_ino)
                    else:
                        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)
            finally:
                INSTALLER.OPERATION_LOCK = original

    @unittest.skipUnless(os.name != "nt", "POSIX directory and symlink semantics")
    def test_unsafe_operation_lock_parent_directories_block_without_redirecting(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        try:
            for unsafe in ("symlink", "permissions"):
                with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    home = root / "home"
                    home.mkdir(mode=0o700)
                    config = home / ".config"
                    redirected = root / "redirected"
                    redirected.mkdir(mode=0o700)
                    if unsafe == "symlink":
                        config.symlink_to(redirected, target_is_directory=True)
                    else:
                        config.mkdir(mode=0o700)
                        config.chmod(0o777)
                    INSTALLER.OPERATION_LOCK = config / "blun-language-guard" / "operation.lock"

                    with mock.patch.object(INSTALLER.Path, "home", return_value=home):
                        token = INSTALLER._acquire_operation_lock("update", now=100)

                    self.assertIsNone(token)
                    self.assertFalse((redirected / "blun-language-guard" / "operation.lock").exists())
                    if unsafe == "permissions":
                        self.assertFalse(INSTALLER.OPERATION_LOCK.exists())
                        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o777)
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
            if os.name != "nt":
                INSTALLER.OPERATION_LOCK.chmod(0o600)
            os.utime(INSTALLER.OPERATION_LOCK, (100, 100))

            def replace_lock(_metadata: os.stat_result, **_kwargs) -> bool:
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

                def replace_after_read(_metadata: os.stat_result, **_kwargs) -> dict:
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

    def test_operation_lock_parent_exchange_during_release_preserves_replacement(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "locks"
            parent.mkdir(mode=0o700)
            detached = root / "detached-locks"
            lock = parent / "operation.lock"
            replacement = b"replacement-parent-lock\n"
            INSTALLER.OPERATION_LOCK = lock
            try:
                token = INSTALLER._acquire_operation_lock("update", now=100)
                self.assertIsNotNone(token)
                real_unlink = INSTALLER._unlink_operation_lock

                def exchange_parent_then_unlink(directory_handle: int | None) -> None:
                    parent.rename(detached)
                    parent.mkdir(mode=0o700)
                    lock.write_bytes(replacement)
                    if os.name != "nt":
                        lock.chmod(0o600)
                    real_unlink(directory_handle)

                with mock.patch.object(
                    INSTALLER,
                    "_unlink_operation_lock",
                    side_effect=exchange_parent_then_unlink,
                ):
                    assert token is not None
                    INSTALLER._release_operation_lock(token)

                self.assertEqual(lock.read_bytes(), replacement)
                self.assertFalse((detached / "operation.lock").exists())
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_operation_lock_changed_while_reading_is_preserved(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "operation.lock"
            INSTALLER.OPERATION_LOCK = lock
            INSTALLER._atomic_json(lock, {
                "operation": "update", "pid": 999999, "started_at": 100, "token": "a" * 32,
            })
            os.utime(lock, (100, 100))
            replacement = {
                "operation": "rollback", "pid": os.getpid(), "started_at": 200,
                "token": "b" * 32,
            }
            real_fstat = INSTALLER.os.fstat
            stat_calls = 0

            def exchange_during_read(descriptor: int) -> os.stat_result:
                nonlocal stat_calls
                stat_calls += 1
                if stat_calls == 2:
                    lock.write_text(INSTALLER.json.dumps(replacement) + "\n", encoding="utf-8")
                    if os.name != "nt":
                        lock.chmod(0o600)
                return real_fstat(descriptor)

            try:
                with mock.patch.object(INSTALLER.os, "fstat", side_effect=exchange_during_read):
                    token = INSTALLER._acquire_operation_lock(
                        "health-monitor",
                        now=100 + INSTALLER.OPERATION_LOCK_STALE_SECONDS + 1,
                    )
                self.assertIsNone(token)
                self.assertEqual(INSTALLER.json.loads(lock.read_text(encoding="utf-8")), replacement)
            finally:
                INSTALLER.OPERATION_LOCK = original

    def test_operation_lock_replaced_during_creation_is_not_claimed(self) -> None:
        original = INSTALLER.OPERATION_LOCK
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "operation.lock"
            INSTALLER.OPERATION_LOCK = lock
            replacement = b"replacement-lock\n"
            real_lstat = INSTALLER._operation_lock_lstat
            exchanged = False

            def exchange_before_install_check(directory: int | None) -> os.stat_result:
                nonlocal exchanged
                if not exchanged:
                    exchanged = True
                    lock.unlink()
                    lock.write_bytes(replacement)
                    if os.name != "nt":
                        lock.chmod(0o600)
                return real_lstat(directory)

            try:
                with mock.patch.object(
                    INSTALLER, "_operation_lock_lstat", side_effect=exchange_before_install_check,
                ):
                    token = INSTALLER._acquire_operation_lock("update", now=100)
                self.assertIsNone(token)
                self.assertEqual(lock.read_bytes(), replacement)
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

    def test_health_monitor_auto_enrollment_preserves_replaced_policy(self) -> None:
        originals = (
            INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK,
            INSTALLER.TARGETS,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            claude_target = root / "claude-skill"
            claude_target.symlink_to(skill, target_is_directory=True)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER.TARGETS = {**INSTALLER.TARGETS, "claude": claude_target}
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                "enabled": True, "interval_seconds": 60, "claude_command": "/bin/claude",
            })

            def replace_policy(_version: str, _command: str) -> dict:
                INSTALLER.HEALTH_CONFIG.unlink()
                INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                    "enabled": False,
                    "interval_seconds": 300,
                    "plugin_required": False,
                    "claude_command": "replacement",
                })
                return {"installed": True, "healthy": True, "version": _version}

            try:
                with mock.patch.object(INSTALLER, "_guard_stack_status", return_value=(True, True)), \
                     mock.patch.object(
                         INSTALLER, "claude_plugin_status", side_effect=replace_policy
                     ), mock.patch.object(INSTALLER, "update_claude_plugin") as updater, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=6500), 2)
                updater.assert_not_called()
                self.assertFalse(INSTALLER.HEALTH_STATE.exists())
                policy = INSTALLER.json.loads(
                    INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8")
                )
                self.assertFalse(policy["enabled"])
                self.assertEqual(policy["interval_seconds"], 300)
                self.assertEqual(policy["claude_command"], "replacement")
            finally:
                (
                    INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK,
                    INSTALLER.TARGETS,
                ) = originals

    def test_health_monitor_state_exchange_blocks_before_repair(self) -> None:
        originals = (INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {
                "status": "blocked", "checked_at": 1, "consecutive_failures": 1,
            })

            def replace_state() -> tuple[bool, bool]:
                INSTALLER.HEALTH_STATE.unlink()
                INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {
                    "status": "replacement",
                    "checked_at": 6501,
                    "consecutive_failures": 9,
                    "next_repair_at": 9999,
                })
                return False, False

            try:
                with mock.patch.object(
                    INSTALLER, "_guard_stack_status", side_effect=replace_state
                ), mock.patch.object(
                    INSTALLER,
                    "_claude_plugin_monitor_status",
                    return_value={"required": False, "healthy": True, "reason": "not-installed"},
                ), mock.patch.object(INSTALLER, "restart_guard_runtime") as restart, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=6500), 2)
                restart.assert_not_called()
                state = INSTALLER.json.loads(
                    INSTALLER.HEALTH_STATE.read_text(encoding="utf-8")
                )
                self.assertEqual(state["status"], "replacement")
                self.assertEqual(state["next_repair_at"], 9999)
            finally:
                INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_health_monitor_state_exchange_during_repair_is_not_overwritten(self) -> None:
        originals = (INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {
                "status": "blocked", "checked_at": 1, "consecutive_failures": 1,
            })

            def replace_state() -> tuple[bool, str]:
                INSTALLER.HEALTH_STATE.unlink()
                INSTALLER._atomic_json(INSTALLER.HEALTH_STATE, {
                    "status": "replacement",
                    "checked_at": 6601,
                    "consecutive_failures": 7,
                    "next_repair_at": 8888,
                })
                return False, "offline"

            try:
                with mock.patch.object(
                    INSTALLER, "_guard_stack_status", return_value=(False, False)
                ), mock.patch.object(
                    INSTALLER,
                    "_claude_plugin_monitor_status",
                    return_value={"required": False, "healthy": True, "reason": "not-installed"},
                ), mock.patch.object(
                    INSTALLER, "restart_guard_runtime", side_effect=replace_state
                ) as restart, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=6600), 2)
                restart.assert_called_once_with()
                state = INSTALLER.json.loads(
                    INSTALLER.HEALTH_STATE.read_text(encoding="utf-8")
                )
                self.assertEqual(state["status"], "replacement")
                self.assertEqual(state["next_repair_at"], 8888)
            finally:
                INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK = originals

    def test_health_monitor_policy_exchange_during_probe_blocks_before_repair(self) -> None:
        originals = (
            INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                "enabled": True, "interval_seconds": 60, "plugin_required": False,
            })

            def replace_policy() -> tuple[bool, bool]:
                INSTALLER.HEALTH_CONFIG.unlink()
                INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                    "enabled": False,
                    "interval_seconds": 300,
                    "plugin_required": False,
                    "claude_command": "replacement",
                })
                return False, False

            try:
                with mock.patch.object(
                    INSTALLER, "_guard_stack_status", side_effect=replace_policy
                ), mock.patch.object(
                    INSTALLER,
                    "_claude_plugin_monitor_status",
                    return_value={"required": False, "healthy": True, "reason": "not-installed"},
                ), mock.patch.object(INSTALLER, "restart_guard_runtime") as restart, \
                     mock.patch.object(INSTALLER, "update_claude_plugin") as updater, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=6700), 2)
                restart.assert_not_called()
                updater.assert_not_called()
                self.assertFalse(INSTALLER.HEALTH_STATE.exists())
                policy = INSTALLER.json.loads(
                    INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8")
                )
                self.assertFalse(policy["enabled"])
                self.assertEqual(policy["interval_seconds"], 300)
                self.assertEqual(policy["claude_command"], "replacement")
            finally:
                (
                    INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE,
                    INSTALLER.OPERATION_LOCK,
                ) = originals

    def test_health_monitor_policy_exchange_during_repair_blocks_publication(self) -> None:
        originals = (
            INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE, INSTALLER.OPERATION_LOCK,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            INSTALLER.HEALTH_CONFIG = root / "health-config.json"
            INSTALLER.HEALTH_STATE = root / "health-state.json"
            INSTALLER.OPERATION_LOCK = root / "operation.lock"
            INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                "enabled": True, "interval_seconds": 60, "plugin_required": False,
            })

            def replace_policy() -> tuple[bool, str]:
                INSTALLER.HEALTH_CONFIG.unlink()
                INSTALLER._atomic_json(INSTALLER.HEALTH_CONFIG, {
                    "enabled": False,
                    "interval_seconds": 600,
                    "plugin_required": False,
                })
                return True, "restarted"

            try:
                with mock.patch.object(
                    INSTALLER, "_guard_stack_status", return_value=(False, False)
                ), mock.patch.object(
                    INSTALLER,
                    "_claude_plugin_monitor_status",
                    return_value={"required": False, "healthy": True, "reason": "not-installed"},
                ), mock.patch.object(
                    INSTALLER, "restart_guard_runtime", side_effect=replace_policy
                ) as restart, mock.patch.object(
                    INSTALLER, "_wait_for_stack", return_value=True
                ) as waiter, mock.patch.object(
                    INSTALLER, "restart_mcp_http_runtime"
                ) as mcp_restart, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALLER.health_monitor_run(now=6800), 2)
                restart.assert_called_once_with()
                waiter.assert_called_once_with(guard=True)
                mcp_restart.assert_not_called()
                self.assertFalse(INSTALLER.HEALTH_STATE.exists())
                policy = INSTALLER.json.loads(
                    INSTALLER.HEALTH_CONFIG.read_text(encoding="utf-8")
                )
                self.assertFalse(policy["enabled"])
                self.assertEqual(policy["interval_seconds"], 600)
            finally:
                (
                    INSTALLER.HEALTH_CONFIG, INSTALLER.HEALTH_STATE,
                    INSTALLER.OPERATION_LOCK,
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
            service_path = home / ".config" / "systemd" / "user" / "blun-language-guard-health.service"
            self.assertIn("health-monitor", service_path.read_text())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(service_path.stat().st_mode), 0o600)

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
