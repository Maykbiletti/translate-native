#!/usr/bin/env python3
"""Non-destructive installer, updater, and live doctor for BLUN Language Guard."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_URL = "https://github.com/Maykbiletti/translate-native.git"
TARGETS = {
    "codex": Path.home() / ".agents" / "skills" / "translate-native",
    "claude": Path.home() / ".claude" / "skills" / "translate-native",
}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def atomic_symlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_symlink():
        raise RuntimeError(f"Refusing to overwrite existing non-symlink: {destination}")
    temporary = destination.with_name(destination.name + ".new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source, target_is_directory=True)
    temporary.replace(destination)


def install(targets: list[str]) -> int:
    root = repository_root()
    skill = root / "translate-native"
    for target in targets:
        atomic_symlink(skill, TARGETS[target])
        print(f"OK {target}: {TARGETS[target]} -> {skill}")
    config = {
        "mcpServers": {
            "blun-language-guard": {
                "command": sys.executable,
                "args": [str(skill / "scripts" / "blun_language_guard.py"), "serve"],
            }
        }
    }
    output = Path.home() / ".config" / "blun-language-guard" / "mcp-snippet.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"MCP snippet written without modifying host configuration: {output}")
    return 0


def _run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def doctor() -> int:
    root = repository_root()
    checks: list[tuple[str, bool, str]] = []
    for name, target in TARGETS.items():
        checks.append((f"{name} skill", target.is_symlink() and target.resolve() == (root / "translate-native").resolve(), str(target)))
    tests = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], root)
    checks.append(("test suite", tests.returncode == 0, tests.stderr.strip() or tests.stdout.strip()))
    server = root / "translate-native" / "scripts" / "blun_language_guard.py"
    probe = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    mcp = subprocess.run([sys.executable, str(server), "serve"], input=probe, text=True, capture_output=True, check=False)
    tools_ok = mcp.returncode == 0 and "release_translation" in mcp.stdout and "verify_release_token" in mcp.stdout
    checks.append(("live MCP tools", tools_ok, mcp.stderr.strip() or mcp.stdout[:240]))
    quality_path = root / "translate-native" / "scripts" / "language_quality.py"
    spec = importlib.util.spec_from_file_location("blun_doctor_quality", quality_path)
    assert spec and spec.loader
    quality = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quality)
    with tempfile.TemporaryDirectory(prefix="blun-doctor-") as directory:
        key = quality.load_or_create_key(Path(directory) / "signing.key")
        receipt = quality.issue_receipt("Hello", "Hej", "sv-SE", key)
        valid = quality.verify_receipt(receipt, "Hello", "Hej", "sv-SE", key)["valid"]
        tamper_blocked = not quality.verify_receipt(receipt, "Hello", "Hallå", "sv-SE", key)["valid"]
    checks.append(("signed receipt round-trip", valid and tamper_blocked, "valid receipt accepted; edited target rejected"))
    failed = False
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
        failed |= not passed
    return int(failed)


def update() -> int:
    root = repository_root()
    if not (root / ".git").exists():
        print("Update requires a Git checkout; reinstall from the latest release.", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="blun-language-guard-") as directory:
        candidate = Path(directory) / "repo"
        clone = _run(["git", "clone", "--depth", "1", REPO_URL, str(candidate)])
        if clone.returncode:
            print(clone.stderr, file=sys.stderr)
            return 1
        tests = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], candidate)
        if tests.returncode:
            print("Candidate update failed tests; current installation is unchanged.", file=sys.stderr)
            return 1
        revision = _run(["git", "rev-parse", "HEAD"], candidate).stdout.strip()
    fetch = _run(["git", "fetch", "origin", revision], root)
    if fetch.returncode:
        print(fetch.stderr, file=sys.stderr)
        return 1
    merge = _run(["git", "merge", "--ff-only", revision], root)
    if merge.returncode:
        print(merge.stderr, file=sys.stderr)
        return 1
    print(f"Updated to tested revision {revision}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and diagnose BLUN Language Guard")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--target", action="append", choices=tuple(TARGETS), dest="targets")
    sub.add_parser("doctor")
    sub.add_parser("update")
    args = parser.parse_args()
    if args.command == "install":
        return install(args.targets or list(TARGETS))
    if args.command == "doctor":
        return doctor()
    return update()


if __name__ == "__main__":
    raise SystemExit(main())
