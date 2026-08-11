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
import time
import platform
from pathlib import Path


REPO_URL = "https://github.com/Maykbiletti/translate-native.git"
TARGETS = {
    "codex": Path.home() / ".agents" / "skills" / "translate-native",
    "claude": Path.home() / ".claude" / "skills" / "translate-native",
    "blun": Path.home() / ".blun" / "skills" / "translate-native",
}
UPDATE_CONFIG = Path.home() / ".config" / "blun-language-guard" / "updater.json"
UPDATE_STATE = Path.home() / ".config" / "blun-language-guard" / "update-state.json"


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
    if "blun" in targets:
        blun_config = Path.home() / ".blun" / "mcp.json"
        current = json.loads(blun_config.read_text(encoding="utf-8-sig")) if blun_config.exists() else {}
        servers = current.setdefault("mcpServers", {})
        servers["blun-language-guard"] = config["mcpServers"]["blun-language-guard"]
        if blun_config.exists():
            backup = blun_config.with_suffix(".json.bak")
            shutil.copy2(blun_config, backup)
            print(f"BLUN MCP backup: {backup}")
        _atomic_json(blun_config, current)
        print(f"BLUN MCP configuration merged: {blun_config}")
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
    probe = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        '{"jsonrpc":"2.0","id":3,"method":"prompts/list"}\n'
    )
    mcp = subprocess.run([sys.executable, str(server), "serve"], input=probe, text=True, capture_output=True, check=False)
    tools_ok = (
        mcp.returncode == 0
        and "release_response" in mcp.stdout
        and "release_translation" in mcp.stdout
        and "verify_release_token" in mcp.stdout
        and "translate-native" in mcp.stdout
        and "Never use release_response to bypass" in mcp.stdout
    )
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
        purpose_blocked = not quality.verify_receipt(
            receipt, "Hello", "Hej", "sv-SE", key, purpose="response"
        )["valid"]
    checks.append((
        "signed receipt round-trip",
        valid and tamper_blocked and purpose_blocked,
        "valid receipt accepted; edited target and wrong purpose rejected",
    ))
    if UPDATE_CONFIG.exists():
        config = json.loads(UPDATE_CONFIG.read_text(encoding="utf-8"))
        state = json.loads(UPDATE_STATE.read_text(encoding="utf-8")) if UPDATE_STATE.exists() else {}
        maximum_age = int(config.get("interval_hours", 24)) * 7200
        fresh = int(time.time()) - int(state.get("checked_at", 0)) <= maximum_age
        checks.append(("automatic updater heartbeat", fresh, str(UPDATE_STATE)))
    failed = False
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
        failed |= not passed
    return int(failed)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def update(require_signed_commits: bool = False) -> int:
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
        if require_signed_commits:
            verified = _run(["git", "verify-commit", revision], candidate)
            if verified.returncode:
                print("Candidate update is not signed by a trusted Git identity; current installation is unchanged.", file=sys.stderr)
                return 1
    previous = _run(["git", "rev-parse", "HEAD"], root).stdout.strip()
    fetch = _run(["git", "fetch", "origin", revision], root)
    if fetch.returncode:
        print(fetch.stderr, file=sys.stderr)
        return 1
    merge = _run(["git", "merge", "--ff-only", revision], root)
    if merge.returncode:
        print(merge.stderr, file=sys.stderr)
        return 1
    post_tests = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], root)
    if post_tests.returncode:
        rollback = _run(["git", "reset", "--keep", previous], root)
        print("Installed revision failed its post-update check; rollback " + ("succeeded." if rollback.returncode == 0 else "FAILED."), file=sys.stderr)
        return 1
    _atomic_json(UPDATE_STATE, {"status": "ok", "revision": revision, "previous": previous, "checked_at": int(time.time())})
    print(f"Updated to tested revision {revision}; rollback revision is {previous}")
    return 0


def install_scheduler() -> tuple[bool, str]:
    command = f'"{sys.executable}" "{Path(__file__).resolve()}" auto-update run'
    system = platform.system()
    if system == "Linux":
        units = Path.home() / ".config" / "systemd" / "user"
        units.mkdir(parents=True, exist_ok=True)
        service = units / "blun-language-guard-update.service"
        timer = units / "blun-language-guard-update.timer"
        service.write_text("[Unit]\nDescription=Update BLUN Language Guard safely\n\n[Service]\nType=oneshot\nExecStart=" + command + "\n", encoding="utf-8")
        timer.write_text("[Unit]\nDescription=Daily BLUN Language Guard update check\n\n[Timer]\nOnBootSec=5m\nOnUnitActiveSec=1h\nPersistent=true\nRandomizedDelaySec=10m\n\n[Install]\nWantedBy=timers.target\n", encoding="utf-8")
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", timer.name])
        ok = reload_result.returncode == 0 and enable_result.returncode == 0
        return ok, str(timer)
    if system == "Darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "ai.blun.language-guard-updater.plist"
        plist.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>ai.blun.language-guard-updater</string>
<key>ProgramArguments</key><array><string>""" + sys.executable + "</string><string>" + str(Path(__file__).resolve()) + "</string><string>auto-update</string><string>run</string></array>\n<key>StartInterval</key><integer>3600</integer><key>RunAtLoad</key><true/></dict></plist>\n", encoding="utf-8")
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _run(["schtasks", "/Create", "/F", "/SC", "HOURLY", "/TN", "BLUN Language Guard Updater", "/TR", command])
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard Updater"
    return False, f"No scheduler adapter for {system}"


def remove_scheduler() -> None:
    system = platform.system()
    if system == "Linux":
        _run(["systemctl", "--user", "disable", "--now", "blun-language-guard-update.timer"])
        for name in ("blun-language-guard-update.service", "blun-language-guard-update.timer"):
            (Path.home() / ".config" / "systemd" / "user" / name).unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    elif system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "ai.blun.language-guard-updater.plist"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        plist.unlink(missing_ok=True)
    elif system == "Windows":
        _run(["schtasks", "/Delete", "/F", "/TN", "BLUN Language Guard Updater"])


def auto_update(action: str, interval_hours: int = 24, require_signed_commits: bool = False, scheduler: bool = True) -> int:
    if action == "enable":
        _atomic_json(UPDATE_CONFIG, {
            "enabled": True,
            "interval_hours": max(1, interval_hours),
            "require_signed_commits": require_signed_commits,
            "repository": REPO_URL,
        })
        print(f"Automatic updates enabled every {max(1, interval_hours)} hour(s).")
        if scheduler:
            ok, detail = install_scheduler()
            print(f"{'Scheduler installed' if ok else 'Scheduler installation failed'}: {detail}")
            return 0 if ok else 1
        return 0
    if action == "disable":
        remove_scheduler()
        UPDATE_CONFIG.unlink(missing_ok=True)
        print("Automatic updates disabled.")
        return 0
    if action == "status":
        print(UPDATE_CONFIG.read_text(encoding="utf-8") if UPDATE_CONFIG.exists() else json.dumps({"enabled": False}))
        if UPDATE_STATE.exists():
            print(UPDATE_STATE.read_text(encoding="utf-8"))
        return 0
    if not UPDATE_CONFIG.exists():
        print("Automatic updates are not enabled.", file=sys.stderr)
        return 2
    config = json.loads(UPDATE_CONFIG.read_text(encoding="utf-8"))
    last = json.loads(UPDATE_STATE.read_text(encoding="utf-8")) if UPDATE_STATE.exists() else {}
    due = int(time.time()) - int(last.get("checked_at", 0)) >= int(config["interval_hours"]) * 3600
    if not due:
        print("Update check is not due yet.")
        return 0
    return update(bool(config.get("require_signed_commits")))


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and diagnose BLUN Language Guard")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--target", action="append", choices=tuple(TARGETS), dest="targets")
    sub.add_parser("doctor")
    update_parser = sub.add_parser("update")
    update_parser.add_argument("--require-signed-commits", action="store_true")
    auto = sub.add_parser("auto-update")
    auto.add_argument("action", choices=("enable", "disable", "status", "run"))
    auto.add_argument("--interval-hours", type=int, default=24)
    auto.add_argument("--require-signed-commits", action="store_true")
    auto.add_argument("--no-scheduler", action="store_true", help="Write policy only; do not install an OS scheduler")
    args = parser.parse_args()
    if args.command == "install":
        return install(args.targets or list(TARGETS))
    if args.command == "doctor":
        return doctor()
    if args.command == "update":
        return update(args.require_signed_commits)
    return auto_update(args.action, args.interval_hours, args.require_signed_commits, not args.no_scheduler)


if __name__ == "__main__":
    raise SystemExit(main())
