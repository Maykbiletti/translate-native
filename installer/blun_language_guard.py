#!/usr/bin/env python3
"""Non-destructive installer, updater, and live doctor for BLUN Language Guard."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_URL = "https://github.com/Maykbiletti/translate-native.git"
TARGETS = {
    "codex": Path.home() / ".agents" / "skills" / "translate-native",
    "claude": Path.home() / ".claude" / "skills" / "translate-native",
    "blun": Path.home() / ".blun" / "skills" / "translate-native",
}
UPDATE_CONFIG = Path.home() / ".config" / "blun-language-guard" / "updater.json"
UPDATE_STATE = Path.home() / ".config" / "blun-language-guard" / "update-state.json"
HEALTH_STATE = Path.home() / ".config" / "blun-language-guard" / "health-state.json"
HEALTH_CONFIG = Path.home() / ".config" / "blun-language-guard" / "health-monitor.json"
OPERATION_LOCK = Path.home() / ".config" / "blun-language-guard" / "operation.lock"
DELIVERY_COMMAND = Path.home() / ".local" / "bin" / "blun-language-deliver"
DELIVERY_POLICY = Path.home() / ".config" / "blun-language-guard" / "delivery-policy.json"
SIGNING_KEY = Path.home() / ".config" / "blun-language-guard" / "signing.key"
SERVICE_COMMAND = Path.home() / ".local" / "bin" / "blun-language-guard-service"
SERVICE_TOKEN = Path.home() / ".config" / "blun-language-guard" / "service.token"
AUDIT_LOG = Path.home() / ".config" / "blun-language-guard" / "audit.jsonl"
MCP_HTTP_COMMAND = Path.home() / ".local" / "bin" / "blun-language-guard-mcp"
MCP_HEADERS_COMMAND = Path.home() / ".local" / "bin" / "blun-language-guard-mcp-headers"
MCP_HTTP_TOKEN = Path.home() / ".config" / "blun-language-guard" / "mcp-http.token"
MCP_HTTP_URL = "http://127.0.0.1:47632/mcp"
CLAUDE_CONFIG = Path.home() / ".claude.json"
MCP_SERVER_NAME = "blun-language-guard"
CLAUDE_PLUGIN_NAME = "translate-native@blun-language-tools"
OPERATION_LOCK_STALE_SECONDS = 30 * 60
HEALTH_REPAIR_COOLDOWN_SECONDS = 45
SERVICE_ENDPOINT = (
    "tcp:127.0.0.1:47631"
    if os.name == "nt"
    else f"unix:{Path.home() / '.config' / 'blun-language-guard' / 'guard.sock'}"
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def atomic_symlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_symlink():
        raise RuntimeError(f"Refusing to overwrite existing non-symlink: {destination}")
    temporary = destination.with_name(destination.name + ".new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source, target_is_directory=source.is_dir())
    temporary.replace(destination)


def ensure_signing_key(path: Path | None = None) -> None:
    """Create the local trust key once and never replace an existing key."""
    path = path or SIGNING_KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f"Signing-key path is not a file: {path}")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise RuntimeError(f"Signing-key permissions must be owner-only: {path}")
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(os.urandom(32))
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    temporary.replace(path)


def ensure_service_token(path: Path | None = None) -> None:
    """Create a stable text token used only by host adapters and the MCP process."""
    path = path or SERVICE_TOKEN
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f"Service-token path is not a file: {path}")
        token = path.read_text(encoding="utf-8-sig").strip()
        if len(token) < 32:
            raise RuntimeError(f"Service token is invalid: {path}")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise RuntimeError(f"Service-token permissions must be owner-only: {path}")
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text(os.urandom(32).hex() + "\n", encoding="ascii")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    temporary.replace(path)


def ensure_mcp_http_token(path: Path | None = None) -> None:
    """Create a stable bearer token for the loopback HTTP MCP endpoint."""
    path = path or MCP_HTTP_TOKEN
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f"MCP access-token path is not a file: {path}")
        token = path.read_text(encoding="utf-8-sig").strip()
        if len(token) < 32:
            raise RuntimeError(f"MCP access token is invalid: {path}")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise RuntimeError(f"MCP access-token permissions must be owner-only: {path}")
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text(os.urandom(32).hex() + "\n", encoding="ascii")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    temporary.replace(path)


def install_delivery_boundary(root: Path) -> None:
    source = root / "integrations" / "enforced_delivery.py"
    if not source.is_file():
        raise RuntimeError(f"Missing delivery boundary: {source}")
    source.chmod(source.stat().st_mode | 0o111)
    atomic_symlink(source, DELIVERY_COMMAND)
    ensure_signing_key()
    policy = json.loads((root / "integrations" / "delivery-policy.example.json").read_text(encoding="utf-8"))
    policy["isolated_service"] = {
        "required": True,
        "endpoint": SERVICE_ENDPOINT,
        "token_file": str(SERVICE_TOKEN),
        "audit_file": str(AUDIT_LOG),
    }
    _atomic_json(DELIVERY_POLICY, policy)
    print(f"Mandatory delivery command: {DELIVERY_COMMAND}")
    print(f"Fail-closed delivery policy: {DELIVERY_POLICY}")


def install_guard_runtime(root: Path) -> None:
    source = root / "integrations" / "guard_service.py"
    if not source.is_file():
        raise RuntimeError(f"Missing isolated guard service: {source}")
    source.chmod(source.stat().st_mode | 0o111)
    atomic_symlink(source, SERVICE_COMMAND)
    ensure_signing_key()
    ensure_service_token()
    print(f"Isolated guard service command: {SERVICE_COMMAND}")
    print(f"Content-free audit log: {AUDIT_LOG}")


def install_mcp_http_runtime(root: Path) -> None:
    gateway = root / "integrations" / "mcp_http_gateway.py"
    headers = root / "integrations" / "mcp_auth_headers.py"
    for source in (gateway, headers):
        if not source.is_file():
            raise RuntimeError(f"Missing persistent MCP component: {source}")
        source.chmod(source.stat().st_mode | 0o111)
    atomic_symlink(gateway, MCP_HTTP_COMMAND)
    atomic_symlink(headers, MCP_HEADERS_COMMAND)
    ensure_mcp_http_token()
    print(f"Persistent MCP command: {MCP_HTTP_COMMAND}")
    print(f"Dynamic MCP headers command: {MCP_HEADERS_COMMAND}")


def _service_arguments(root: Path) -> list[str]:
    return [
        sys.executable,
        str(root / "integrations" / "guard_service.py"),
        "--endpoint", SERVICE_ENDPOINT,
        "--key-file", str(SIGNING_KEY),
        "--token-file", str(SERVICE_TOKEN),
        "--audit-file", str(AUDIT_LOG),
    ]


def _mcp_http_arguments(root: Path) -> list[str]:
    return [
        sys.executable,
        str(root / "integrations" / "mcp_http_gateway.py"),
        "--host", "127.0.0.1",
        "--port", "47632",
        "--path", "/mcp",
        "--access-token-file", str(MCP_HTTP_TOKEN),
        "--service-endpoint", SERVICE_ENDPOINT,
        "--service-token-file", str(SERVICE_TOKEN),
    ]


def _shell_command(arguments: list[str]) -> str:
    if platform.system() == "Windows":
        return subprocess.list2cmdline(arguments)
    return " ".join(shlex.quote(value) for value in arguments)


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _install_windows_restartable_task(task_name: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    executable = arguments[0]
    argument_line = subprocess.list2cmdline(arguments[1:])
    name = _powershell_literal(task_name)
    script = (
        "$ErrorActionPreference='Stop';"
        f"$action=New-ScheduledTaskAction -Execute {_powershell_literal(executable)} "
        f"-Argument {_powershell_literal(argument_line)};"
        "$trigger=New-ScheduledTaskTrigger -AtLogOn;"
        "$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
        "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-ExecutionTimeLimit (New-TimeSpan -Days 3650);"
        f"Register-ScheduledTask -TaskName {name} -Action $action -Trigger $trigger "
        "-Settings $settings -Force | Out-Null;"
        f"Start-ScheduledTask -TaskName {name}"
    )
    return _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def install_guard_autostart(root: Path) -> tuple[bool, str]:
    """Install and start a per-user service. Separate-user deployment stays an admin task."""
    arguments = _service_arguments(root)
    system = platform.system()
    if system == "Linux":
        units = Path.home() / ".config" / "systemd" / "user"
        units.mkdir(parents=True, exist_ok=True)
        service = units / "blun-language-guard.service"
        service.write_text(
            "[Unit]\nDescription=BLUN isolated language release guard\n\n"
            "[Service]\nType=simple\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            f"ExecStart={_shell_command(arguments)}\nRestart=on-failure\nRestartSec=2\n\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", service.name])
        return reload_result.returncode == 0 and enable_result.returncode == 0, str(service)
    if system == "Darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "ai.blun.language-guard.plist"
        program_arguments = "".join(f"<string>{_xml_escape(value)}</string>" for value in arguments)
        plist.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict><key>Label</key><string>ai.blun.language-guard</string>"
            f"<key>ProgramArguments</key><array>{program_arguments}</array>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n",
            encoding="utf-8",
        )
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _install_windows_restartable_task("BLUN Language Guard", arguments)
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard"
    return False, f"No guard-service adapter for {system}"


def install_mcp_http_autostart(root: Path) -> tuple[bool, str]:
    """Install the persistent Claude-facing HTTP MCP with OS-level restart policy."""
    arguments = _mcp_http_arguments(root)
    system = platform.system()
    if system == "Linux":
        units = Path.home() / ".config" / "systemd" / "user"
        units.mkdir(parents=True, exist_ok=True)
        service = units / "blun-language-guard-mcp.service"
        service.write_text(
            "[Unit]\nDescription=BLUN persistent Streamable HTTP MCP\n"
            "After=blun-language-guard.service\nWants=blun-language-guard.service\n\n"
            "[Service]\nType=simple\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            f"ExecStart={_shell_command(arguments)}\nRestart=always\nRestartSec=1\n\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", service.name])
        return reload_result.returncode == 0 and enable_result.returncode == 0, str(service)
    if system == "Darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "ai.blun.language-guard-mcp.plist"
        program_arguments = "".join(f"<string>{_xml_escape(value)}</string>" for value in arguments)
        plist.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict><key>Label</key><string>ai.blun.language-guard-mcp</string>"
            f"<key>ProgramArguments</key><array>{program_arguments}</array>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            "<key>ThrottleInterval</key><integer>1</integer></dict></plist>\n",
            encoding="utf-8",
        )
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _install_windows_restartable_task("BLUN Language Guard MCP", arguments)
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard MCP"
    return False, f"No persistent MCP adapter for {system}"


def restart_guard_runtime() -> tuple[bool, str]:
    system = platform.system()
    if system == "Linux":
        result = _run(["systemctl", "--user", "restart", "blun-language-guard.service"])
        return result.returncode == 0, "systemd user service"
    if system == "Darwin":
        result = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.blun.language-guard"])
        return result.returncode == 0, "LaunchAgent"
    if system == "Windows":
        _run(["schtasks", "/End", "/TN", "BLUN Language Guard"])
        result = _run(["schtasks", "/Run", "/TN", "BLUN Language Guard"])
        return result.returncode == 0, "Windows Task Scheduler"
    return False, f"No guard-service adapter for {system}"


def restart_mcp_http_runtime() -> tuple[bool, str]:
    system = platform.system()
    if system == "Linux":
        result = _run(["systemctl", "--user", "restart", "blun-language-guard-mcp.service"])
        return result.returncode == 0, "systemd user service"
    if system == "Darwin":
        result = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.blun.language-guard-mcp"])
        return result.returncode == 0, "LaunchAgent"
    if system == "Windows":
        _run(["schtasks", "/End", "/TN", "BLUN Language Guard MCP"])
        result = _run(["schtasks", "/Run", "/TN", "BLUN Language Guard MCP"])
        return result.returncode == 0, "Windows Task Scheduler"
    return False, f"No persistent MCP adapter for {system}"


def remove_mcp_http_autostart() -> None:
    system = platform.system()
    if system == "Linux":
        _run(["systemctl", "--user", "disable", "--now", "blun-language-guard-mcp.service"])
        (Path.home() / ".config" / "systemd" / "user" / "blun-language-guard-mcp.service").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    elif system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "ai.blun.language-guard-mcp.plist"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        plist.unlink(missing_ok=True)
    elif system == "Windows":
        _run(["schtasks", "/Delete", "/F", "/TN", "BLUN Language Guard MCP"])


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def probe_guard_service(timeout: float = 3.0) -> dict:
    root = repository_root()
    client_path = root / "translate-native" / "scripts" / "guard_service_client.py"
    spec = importlib.util.spec_from_file_location("blun_installer_guard_client", client_path)
    assert spec and spec.loader
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    token = SERVICE_TOKEN.read_text(encoding="utf-8-sig").strip()
    return client.call_guard_service(
        SERVICE_ENDPOINT,
        {"operation": "health"},
        auth_token=token,
        timeout=timeout,
    )


def guard_service(action: str) -> int:
    root = repository_root()
    if action in {"install", "start"}:
        install_guard_runtime(root)
        ok, detail = install_guard_autostart(root)
        print(f"{'Guard service installed and started' if ok else 'Guard service installation failed'}: {detail}")
        return 0 if ok else 1
    if action == "stop":
        system = platform.system()
        if system == "Linux":
            result = _run(["systemctl", "--user", "stop", "blun-language-guard.service"])
        elif system == "Darwin":
            result = _run(["launchctl", "bootout", f"gui/{os.getuid()}/ai.blun.language-guard"])
        elif system == "Windows":
            result = _run(["schtasks", "/End", "/TN", "BLUN Language Guard"])
        else:
            return 2
        return int(result.returncode != 0)
    try:
        result = probe_guard_service()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"BLOCK guard service unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" and result.get("isolated_key") is True else 1


def claude_mcp_entry() -> dict:
    return {
        "type": "http",
        "url": MCP_HTTP_URL,
        "headersHelper": str(MCP_HEADERS_COMMAND),
    }


def configure_claude_mcp(path: Path | None = None) -> tuple[Path | None, int]:
    """Atomically install the user-scoped HTTP MCP and remove stale local shadows."""
    path = path or CLAUDE_CONFIG
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Refusing to modify unreadable Claude configuration: {path}: {error}") from error
        if not isinstance(current, dict):
            raise RuntimeError(f"Claude configuration root must be an object: {path}")
    else:
        current = {}
    servers = current.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"Claude mcpServers must be an object: {path}")
    servers[MCP_SERVER_NAME] = claude_mcp_entry()

    removed_shadows = 0
    projects = current.get("projects", {})
    if isinstance(projects, dict):
        for project in projects.values():
            if not isinstance(project, dict):
                continue
            local_servers = project.get("mcpServers")
            if isinstance(local_servers, dict) and MCP_SERVER_NAME in local_servers:
                del local_servers[MCP_SERVER_NAME]
                removed_shadows += 1

    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    _atomic_json(path, current)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return backup, removed_shadows


def project_mcp_shadows(start: Path | None = None) -> list[Path]:
    """Return higher-precedence project MCP files that redefine the guard differently."""
    current = (start or Path.cwd()).resolve()
    candidates = [current, *current.parents]
    shadows: list[Path] = []
    for directory in candidates:
        path = directory / ".mcp.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            entry = payload.get("mcpServers", {}).get(MCP_SERVER_NAME)
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            continue
        if entry is not None and entry != claude_mcp_entry():
            shadows.append(path)
    return shadows


def _mcp_http_request(path: str, payload: dict | None = None, *, timeout: float = 4.0) -> tuple[int, dict]:
    token = MCP_HTTP_TOKEN.read_text(encoding="utf-8-sig").strip()
    url = MCP_HTTP_URL.removesuffix("/mcp") + path
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    method = "GET"
    if payload is not None:
        method = "POST"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        })
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        detail = json.loads(raw.decode("utf-8")) if raw else {}
        return error.code, detail


def probe_mcp_http(timeout: float = 4.0) -> dict:
    health_status, health = _mcp_http_request("/healthz", timeout=timeout)
    if health_status != 200 or health.get("status") != "ok" or health.get("isolated_key") is not True:
        raise RuntimeError(f"persistent MCP health failed with HTTP {health_status}")
    initialize_status, initialized = _mcp_http_request("/mcp", {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "blun-language-guard-doctor", "version": "1"},
        },
    }, timeout=timeout)
    tools_status, tools = _mcp_http_request("/mcp", {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }, timeout=timeout)
    names = {
        tool.get("name")
        for tool in tools.get("result", {}).get("tools", [])
        if isinstance(tool, dict)
    }
    if initialize_status != 200 or tools_status != 200 or not {
        "release_response", "release_translation", "verify_release_token",
    } <= names:
        raise RuntimeError("persistent MCP initialize/tools probe failed")
    return {"health": health, "initialize": initialized, "tools": sorted(names)}


def mcp_service(action: str) -> int:
    root = repository_root()
    if action in {"install", "start"}:
        install_mcp_http_runtime(root)
        ok, detail = install_mcp_http_autostart(root)
        print(f"{'Persistent MCP installed and started' if ok else 'Persistent MCP installation failed'}: {detail}")
        return 0 if ok else 1
    if action == "stop":
        system = platform.system()
        if system == "Linux":
            result = _run(["systemctl", "--user", "stop", "blun-language-guard-mcp.service"])
        elif system == "Darwin":
            result = _run(["launchctl", "bootout", f"gui/{os.getuid()}/ai.blun.language-guard-mcp"])
        elif system == "Windows":
            result = _run(["schtasks", "/End", "/TN", "BLUN Language Guard MCP"])
        else:
            return 2
        return int(result.returncode != 0)
    try:
        result = probe_mcp_http()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"BLOCK persistent MCP unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def install(targets: list[str], *, autostart_service: bool = True) -> int:
    root = repository_root()
    skill = root / "translate-native"
    for target in targets:
        atomic_symlink(skill, TARGETS[target])
        print(f"OK {target}: {TARGETS[target]} -> {skill}")
    install_delivery_boundary(root)
    install_guard_runtime(root)
    if "claude" in targets:
        install_mcp_http_runtime(root)
    config = {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": sys.executable,
                "args": [str(skill / "scripts" / "blun_language_guard.py"), "serve"],
                "env": {
                    "BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT": SERVICE_ENDPOINT,
                    "BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE": str(SERVICE_TOKEN),
                },
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
        servers[MCP_SERVER_NAME] = config["mcpServers"][MCP_SERVER_NAME]
        if blun_config.exists():
            backup = blun_config.with_suffix(".json.bak")
            shutil.copy2(blun_config, backup)
            print(f"BLUN MCP backup: {backup}")
        _atomic_json(blun_config, current)
        print(f"BLUN MCP configuration merged: {blun_config}")
    if autostart_service:
        ok, detail = install_guard_autostart(root)
        print(f"{'Guard service installed and started' if ok else 'Guard service installation failed'}: {detail}")
        if not ok:
            return 1
        guard_ready = False
        for _attempt in range(15):
            try:
                health = probe_guard_service()
                guard_ready = health.get("status") == "ok" and health.get("isolated_key") is True
                if guard_ready:
                    break
            except (OSError, RuntimeError, ValueError):
                pass
            time.sleep(0.2)
        if not guard_ready:
            print("Guard service did not become healthy; Claude configuration was not changed.", file=sys.stderr)
            return 1
        if "claude" in targets:
            mcp_ok, mcp_detail = install_mcp_http_autostart(root)
            print(f"{'Persistent MCP installed and started' if mcp_ok else 'Persistent MCP installation failed'}: {mcp_detail}")
            if not mcp_ok:
                return 1
            mcp_ready = False
            for _attempt in range(15):
                try:
                    probe_mcp_http()
                    mcp_ready = True
                    break
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                    time.sleep(0.2)
            if not mcp_ready:
                print("Persistent MCP did not become healthy; Claude configuration was not changed.", file=sys.stderr)
                return 1
    if "claude" in targets:
        backup, removed_shadows = configure_claude_mcp()
        print(f"Claude user MCP configured as persistent HTTP: {CLAUDE_CONFIG}")
        if backup:
            print(f"Claude configuration backup: {backup}")
        if removed_shadows:
            print(f"Removed {removed_shadows} stale project-local Claude MCP shadow(s).")
        if autostart_service and health_monitor("install") != 0:
            return 1
    return 0


def _run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def _find_claude_plugin(value: object) -> dict | None:
    """Accept Claude's documented JSON list while tolerating a wrapped list."""
    if isinstance(value, list):
        plugins = value
    elif isinstance(value, dict):
        plugins = next(
            (value[name] for name in ("plugins", "installedPlugins", "installed") if isinstance(value.get(name), list)),
            [],
        )
    else:
        plugins = []
    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        name = str(plugin.get("name") or plugin.get("id") or plugin.get("plugin") or "")
        marketplace = str(plugin.get("marketplace") or plugin.get("sourceMarketplace") or "")
        if name == CLAUDE_PLUGIN_NAME or (
            name == "translate-native" and marketplace in {"", "blun-language-tools"}
        ):
            return plugin
    return None


def claude_plugin_status(expected_version: str, executable: str | None = None) -> dict:
    """Inspect the cached user plugin without reading Claude's private cache format."""
    command = executable or shutil.which("claude")
    if not command:
        return {"installed": False, "healthy": False, "reason": "claude-command-unavailable"}
    try:
        result = _run([command, "plugin", "list", "--json"])
    except OSError:
        return {"installed": False, "healthy": False, "reason": "claude-command-unavailable"}
    if result.returncode:
        return {"installed": False, "healthy": False, "reason": "plugin-list-failed"}
    try:
        plugin = _find_claude_plugin(json.loads(result.stdout))
    except json.JSONDecodeError:
        return {"installed": False, "healthy": False, "reason": "plugin-list-invalid-json"}
    if plugin is None:
        return {"installed": False, "healthy": False, "reason": "plugin-not-installed"}
    version = str(plugin.get("version") or plugin.get("installedVersion") or "")
    enabled = plugin.get("enabled") is not False
    errors = plugin.get("errors")
    errors = errors if isinstance(errors, list) else ([] if not errors else [errors])
    return {
        "installed": True,
        "healthy": enabled and not errors and version == expected_version,
        "enabled": enabled,
        "errors": errors,
        "version": version,
        "expected_version": expected_version,
    }


def update_claude_plugin(expected_version: str, executable: str | None = None) -> dict:
    """Update an already-installed user plugin and verify the cached version."""
    before = claude_plugin_status(expected_version, executable)
    if not before.get("installed"):
        return {"attempted": False, "updated": False, "status": before}
    if before.get("healthy") is True:
        return {"attempted": False, "updated": True, "status": before, "reload_required": False}
    command = executable or shutil.which("claude")
    assert command
    result = _run([command, "plugin", "update", CLAUDE_PLUGIN_NAME, "--scope", "user"])
    after = claude_plugin_status(expected_version, command)
    updated = result.returncode == 0 and after.get("healthy") is True
    return {
        "attempted": True,
        "updated": updated,
        "returncode": result.returncode,
        "status": after,
        "reload_required": updated and before.get("version") != expected_version,
    }


def doctor() -> int:
    root = repository_root()
    expected_version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    checks: list[tuple[str, bool, str]] = []
    for name, target in TARGETS.items():
        checks.append((f"{name} skill", target.is_symlink() and target.resolve() == (root / "translate-native").resolve(), str(target)))
    delivery_source = root / "integrations" / "enforced_delivery.py"
    checks.append((
        "mandatory delivery command",
        DELIVERY_COMMAND.is_symlink() and DELIVERY_COMMAND.resolve() == delivery_source.resolve(),
        str(DELIVERY_COMMAND),
    ))
    key_secure = SIGNING_KEY.is_file() and (os.name == "nt" or SIGNING_KEY.stat().st_mode & 0o077 == 0)
    checks.append(("signing key", key_secure, str(SIGNING_KEY)))
    service_source = root / "integrations" / "guard_service.py"
    checks.append((
        "isolated guard command",
        SERVICE_COMMAND.is_symlink() and SERVICE_COMMAND.resolve() == service_source.resolve(),
        str(SERVICE_COMMAND),
    ))
    token_secure = SERVICE_TOKEN.is_file() and (os.name == "nt" or SERVICE_TOKEN.stat().st_mode & 0o077 == 0)
    checks.append(("service authentication token", token_secure, str(SERVICE_TOKEN)))
    mcp_gateway_source = root / "integrations" / "mcp_http_gateway.py"
    mcp_headers_source = root / "integrations" / "mcp_auth_headers.py"
    checks.append((
        "persistent MCP command",
        MCP_HTTP_COMMAND.is_symlink() and MCP_HTTP_COMMAND.resolve() == mcp_gateway_source.resolve(),
        str(MCP_HTTP_COMMAND),
    ))
    checks.append((
        "dynamic MCP headers command",
        MCP_HEADERS_COMMAND.is_symlink() and MCP_HEADERS_COMMAND.resolve() == mcp_headers_source.resolve(),
        str(MCP_HEADERS_COMMAND),
    ))
    mcp_token_secure = MCP_HTTP_TOKEN.is_file() and (os.name == "nt" or MCP_HTTP_TOKEN.stat().st_mode & 0o077 == 0)
    checks.append(("MCP HTTP access token", mcp_token_secure, str(MCP_HTTP_TOKEN)))
    claude_config_ok = False
    claude_config_detail = str(CLAUDE_CONFIG)
    if CLAUDE_CONFIG.is_file():
        try:
            claude_config = json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8-sig"))
            claude_entry = claude_config.get("mcpServers", {}).get(MCP_SERVER_NAME)
            local_shadows = sum(
                1
                for project in claude_config.get("projects", {}).values()
                if isinstance(project, dict)
                and MCP_SERVER_NAME in project.get("mcpServers", {})
            ) if isinstance(claude_config.get("projects", {}), dict) else 0
            claude_config_ok = claude_entry == claude_mcp_entry() and local_shadows == 0
            claude_config_detail += f"; stale local shadows={local_shadows}"
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            claude_config_ok = False
    checks.append(("Claude user-scoped HTTP MCP", claude_config_ok, claude_config_detail))
    project_shadows = project_mcp_shadows()
    checks.append((
        "Claude project MCP precedence",
        not project_shadows,
        "none" if not project_shadows else ", ".join(str(path) for path in project_shadows),
    ))
    policy_ok = False
    if DELIVERY_POLICY.is_file():
        try:
            policy = json.loads(DELIVERY_POLICY.read_text(encoding="utf-8-sig"))
            policy_ok = (
                policy.get("mandatory") is True
                and policy.get("direct_delivery_allowed") is False
                and policy.get("raw_streaming_allowed") is False
                and policy.get("isolated_service", {}).get("required") is True
            )
        except (OSError, json.JSONDecodeError):
            policy_ok = False
    checks.append(("fail-closed delivery policy", policy_ok, str(DELIVERY_POLICY)))
    service_live = guard_service("status") == 0
    checks.append(("isolated guard health", service_live, SERVICE_ENDPOINT))
    try:
        persistent_probe = probe_mcp_http()
        persistent_live = True
        persistent_detail = ", ".join(persistent_probe["tools"])
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        persistent_live = False
        persistent_detail = str(error)
    checks.append(("persistent Claude HTTP MCP", persistent_live, persistent_detail))
    plugin = claude_plugin_status(expected_version)
    if plugin.get("reason") != "claude-command-unavailable":
        checks.append((
            "Claude plugin cache",
            plugin.get("healthy") is True,
            json.dumps(plugin, ensure_ascii=False, sort_keys=True),
        ))
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
    if TARGETS["claude"].is_symlink() and health_monitor_enabled():
        try:
            health_state = json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
            health_fresh = int(time.time()) - int(health_state.get("checked_at", 0)) <= 180
            monitor_ok = health_fresh and health_state.get("status") in {"ok", "recovered"}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            monitor_ok = False
        checks.append(("automatic health monitor", monitor_ok, str(HEALTH_STATE)))
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


def _acquire_operation_lock(operation: str, *, now: int | None = None) -> str | None:
    """Take a same-user cross-platform lock without waiting or trusting its contents."""
    timestamp = int(time.time()) if now is None else now
    token = os.urandom(16).hex()
    OPERATION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            descriptor = os.open(OPERATION_LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                stale = timestamp - int(OPERATION_LOCK.stat().st_mtime) > OPERATION_LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if not stale or attempt:
                return None
            try:
                OPERATION_LOCK.unlink()
            except OSError:
                return None
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"operation": operation, "pid": os.getpid(), "started_at": timestamp, "token": token}, handle)
            handle.write("\n")
        return token
    return None


def _release_operation_lock(token: str) -> None:
    """Release only the lock instance acquired by this process."""
    try:
        value = json.loads(OPERATION_LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if value.get("token") == token:
        OPERATION_LOCK.unlink(missing_ok=True)


def update(require_signed_commits: bool = False, claude_command: str | None = None) -> int:
    root = repository_root()
    if not (root / ".git").exists():
        print("Update requires a Git checkout; reinstall from the latest release.", file=sys.stderr)
        return 2
    token = _acquire_operation_lock("update")
    if token is None:
        print("Another guard maintenance operation is active; update skipped safely.", file=sys.stderr)
        return 3
    try:
        return _update_unlocked(require_signed_commits, claude_command)
    finally:
        _release_operation_lock(token)


def _update_unlocked(require_signed_commits: bool = False, claude_command: str | None = None) -> int:
    root = repository_root()
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
    claude_installed = TARGETS["claude"].is_symlink()
    mcp_runtime_preexisting = MCP_HTTP_COMMAND.exists() or MCP_HTTP_COMMAND.is_symlink()
    mcp_headers_preexisting = MCP_HEADERS_COMMAND.exists() or MCP_HEADERS_COMMAND.is_symlink()
    mcp_token_preexisting = MCP_HTTP_TOKEN.exists()
    claude_config_preexisting = CLAUDE_CONFIG.exists()
    claude_config_bytes = CLAUDE_CONFIG.read_bytes() if claude_config_preexisting else b""
    claude_config_mode = CLAUDE_CONFIG.stat().st_mode & 0o777 if claude_config_preexisting else None

    def rollback_runtime() -> subprocess.CompletedProcess[str]:
        rollback = _run(["git", "reset", "--keep", previous], root)
        if claude_installed:
            if not mcp_runtime_preexisting:
                remove_mcp_http_autostart()
                MCP_HTTP_COMMAND.unlink(missing_ok=True)
            if not mcp_headers_preexisting:
                MCP_HEADERS_COMMAND.unlink(missing_ok=True)
            if not mcp_token_preexisting:
                MCP_HTTP_TOKEN.unlink(missing_ok=True)
            if claude_config_preexisting:
                temporary = CLAUDE_CONFIG.with_suffix(CLAUDE_CONFIG.suffix + ".restore")
                temporary.write_bytes(claude_config_bytes)
                if claude_config_mode is not None and os.name != "nt":
                    os.chmod(temporary, claude_config_mode)
                temporary.replace(CLAUDE_CONFIG)
            else:
                CLAUDE_CONFIG.unlink(missing_ok=True)
        restart_guard_runtime()
        if mcp_runtime_preexisting:
            restart_mcp_http_runtime()
        return rollback

    if claude_installed:
        try:
            install_mcp_http_runtime(root)
            installed, detail = install_mcp_http_autostart(root)
            if not installed:
                raise RuntimeError(f"persistent MCP autostart failed: {detail}")
            configure_claude_mcp()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            rollback = rollback_runtime()
            print(
                f"Claude persistent MCP activation failed ({error}); rollback "
                + ("succeeded." if rollback.returncode == 0 else "FAILED."),
                file=sys.stderr,
            )
            return 1
    if SERVICE_COMMAND.is_symlink() or SERVICE_COMMAND.exists():
        restarted, runtime = restart_guard_runtime()
        healthy = False
        if restarted:
            for _attempt in range(10):
                try:
                    health = probe_guard_service()
                    healthy = health.get("status") == "ok" and health.get("isolated_key") is True
                    if healthy:
                        break
                except (OSError, RuntimeError, ValueError):
                    pass
                time.sleep(0.2)
        if not restarted or not healthy:
            rollback = rollback_runtime()
            print(
                "Updated guard could not restart; rollback "
                + ("succeeded." if rollback.returncode == 0 else "FAILED."),
                file=sys.stderr,
            )
            return 1
        print(f"Restarted isolated guard through {runtime}.")
    if MCP_HTTP_COMMAND.is_symlink() or MCP_HTTP_COMMAND.exists():
        restarted, runtime = restart_mcp_http_runtime()
        healthy = False
        if restarted:
            for _attempt in range(15):
                try:
                    probe_mcp_http()
                    healthy = True
                    break
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                    time.sleep(0.2)
        if not restarted or not healthy:
            rollback = rollback_runtime()
            print(
                "Updated persistent MCP could not restart; rollback "
                + ("succeeded." if rollback.returncode == 0 else "FAILED."),
                file=sys.stderr,
            )
            return 1
        print(f"Restarted persistent MCP through {runtime}.")
    monitor_expected = claude_installed and health_monitor_enabled()
    monitor_install = {
        "attempted": False,
        "installed": not monitor_expected,
        "detail": "explicitly-disabled" if claude_installed else "claude-skill-not-installed",
    }
    if monitor_expected:
        monitor_ok, monitor_detail = install_health_monitor()
        guard_now, mcp_now = _guard_stack_status(timeout=4.0)
        monitor_ok = monitor_ok and guard_now and mcp_now
        if monitor_ok:
            _atomic_json(HEALTH_CONFIG, {"enabled": True, "interval_seconds": 60})
            _atomic_json(HEALTH_STATE, {
                "status": "ok",
                "checked_at": int(time.time()),
                "guard_healthy": True,
                "mcp_healthy": True,
                "consecutive_failures": 0,
                "last_repair_at": 0,
                "repairs": [],
            })
        monitor_install = {"attempted": True, "installed": monitor_ok, "detail": monitor_detail}
    expected_version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    plugin_update = update_claude_plugin(expected_version, claude_command) if claude_installed else {
        "attempted": False,
        "updated": False,
        "status": {"reason": "claude-skill-not-installed"},
    }
    plugin_reason = plugin_update.get("status", {}).get("reason")
    plugin_failed = claude_installed and not plugin_update.get("updated") and plugin_reason != "plugin-not-installed"
    monitor_failed = monitor_expected and not monitor_install["installed"]
    if plugin_failed or monitor_failed:
        print(
            "Repository, guard service, and MCP updated successfully, but Claude maintenance did not "
            "reach a healthy synchronized state. The guard remains fail-closed; rerun the updater after "
            "repairing the reported plugin or health-monitor adapter.",
            file=sys.stderr,
        )
        _atomic_json(UPDATE_STATE, {
            "status": "degraded",
            "revision": revision,
            "previous": previous,
            "checked_at": int(time.time()),
            "runtime_version": expected_version,
            "claude_plugin": plugin_update,
            "health_monitor": monitor_install,
        })
        return 1
    _atomic_json(UPDATE_STATE, {
        "status": "ok",
        "revision": revision,
        "previous": previous,
        "checked_at": int(time.time()),
        "runtime_version": expected_version,
        "claude_plugin": plugin_update,
        "health_monitor": monitor_install,
    })
    print(f"Updated to tested revision {revision}; rollback revision is {previous}")
    if plugin_update.get("reload_required"):
        print("Claude plugin cache updated. Existing sessions still use their loaded hooks; run /reload-plugins or start a new session.")
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


def _install_windows_health_task(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    executable = arguments[0]
    argument_line = subprocess.list2cmdline(arguments[1:])
    script = (
        "$ErrorActionPreference='Stop';"
        f"$action=New-ScheduledTaskAction -Execute {_powershell_literal(executable)} "
        f"-Argument {_powershell_literal(argument_line)};"
        "$trigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        "-RepetitionInterval (New-TimeSpan -Minutes 1);"
        "$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 2);"
        "Register-ScheduledTask -TaskName 'BLUN Language Guard Health' -Action $action "
        "-Trigger $trigger -Settings $settings -Force | Out-Null"
    )
    return _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def install_health_monitor(home: Path | None = None) -> tuple[bool, str]:
    """Install a one-minute dependency-aware health check without embedding secrets."""
    home = home or Path.home()
    arguments = [sys.executable, str(Path(__file__).resolve()), "health-monitor", "run"]
    system = platform.system()
    if system == "Linux":
        units = home / ".config" / "systemd" / "user"
        units.mkdir(parents=True, exist_ok=True)
        service = units / "blun-language-guard-health.service"
        timer = units / "blun-language-guard-health.timer"
        service.write_text(
            "[Unit]\nDescription=Verify and repair BLUN Language Guard\n"
            "After=blun-language-guard.service blun-language-guard-mcp.service\n\n"
            "[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            f"ExecStart={_shell_command(arguments)}\n",
            encoding="utf-8",
        )
        timer.write_text(
            "[Unit]\nDescription=Monitor BLUN Language Guard every minute\n\n"
            "[Timer]\nOnBootSec=1m\nOnUnitActiveSec=1m\nAccuracySec=10s\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n",
            encoding="utf-8",
        )
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        enable_result = _run(["systemctl", "--user", "enable", "--now", timer.name])
        return reload_result.returncode == 0 and enable_result.returncode == 0, str(timer)
    if system == "Darwin":
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "ai.blun.language-guard-health.plist"
        program_arguments = "".join(f"<string>{_xml_escape(value)}</string>" for value in arguments)
        plist.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict><key>Label</key>"
            "<string>ai.blun.language-guard-health</string>"
            f"<key>ProgramArguments</key><array>{program_arguments}</array>"
            "<key>RunAtLoad</key><true/><key>StartInterval</key><integer>60</integer>"
            "<key>ThrottleInterval</key><integer>10</integer></dict></plist>\n",
            encoding="utf-8",
        )
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return result.returncode == 0, str(plist)
    if system == "Windows":
        result = _install_windows_health_task(arguments)
        return result.returncode == 0, "Windows Task Scheduler: BLUN Language Guard Health"
    return False, f"No health-monitor adapter for {system}"


def remove_health_monitor(home: Path | None = None) -> None:
    home = home or Path.home()
    system = platform.system()
    if system == "Linux":
        _run(["systemctl", "--user", "disable", "--now", "blun-language-guard-health.timer"])
        units = home / ".config" / "systemd" / "user"
        for name in ("blun-language-guard-health.service", "blun-language-guard-health.timer"):
            (units / name).unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    elif system == "Darwin":
        plist = home / "Library" / "LaunchAgents" / "ai.blun.language-guard-health.plist"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        plist.unlink(missing_ok=True)
    elif system == "Windows":
        _run(["schtasks", "/Delete", "/F", "/TN", "BLUN Language Guard Health"])


def health_monitor_enabled() -> bool:
    """Default existing Claude installations into the safe one-time migration."""
    if not HEALTH_CONFIG.exists():
        return True
    try:
        value = json.loads(HEALTH_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return value.get("enabled") is not False


def _guard_stack_status(timeout: float = 1.0) -> tuple[bool, bool]:
    try:
        guard = probe_guard_service(timeout=timeout)
        guard_healthy = guard.get("status") == "ok" and guard.get("isolated_key") is True
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        guard_healthy = False
    if not guard_healthy:
        return False, False
    try:
        probe_mcp_http(timeout=timeout)
        mcp_healthy = True
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        mcp_healthy = False
    return guard_healthy, mcp_healthy


def _wait_for_stack(*, guard: bool = False, mcp: bool = False, attempts: int = 8) -> bool:
    for _attempt in range(attempts):
        guard_healthy, mcp_healthy = _guard_stack_status()
        if (not guard or guard_healthy) and (not mcp or mcp_healthy):
            return True
        time.sleep(0.2)
    return False


def health_monitor_run(*, now: int | None = None) -> int:
    """Probe the complete release path and make at most one dependency-ordered repair."""
    timestamp = int(time.time()) if now is None else now
    token = _acquire_operation_lock("health-monitor", now=timestamp)
    if token is None:
        print(json.dumps({"status": "busy", "checked_at": timestamp}, sort_keys=True))
        return 0
    try:
        try:
            previous = json.loads(HEALTH_STATE.read_text(encoding="utf-8")) if HEALTH_STATE.exists() else {}
        except (OSError, json.JSONDecodeError):
            previous = {}
        guard_healthy, mcp_healthy = _guard_stack_status()
        if guard_healthy and mcp_healthy:
            state = {
                "status": "ok",
                "checked_at": timestamp,
                "guard_healthy": True,
                "mcp_healthy": True,
                "consecutive_failures": 0,
                "last_repair_at": previous.get("last_repair_at", 0),
                "repairs": [],
            }
            _atomic_json(HEALTH_STATE, state)
            print(json.dumps(state, sort_keys=True))
            return 0
        last_repair = int(previous.get("last_repair_at", 0))
        if timestamp - last_repair < HEALTH_REPAIR_COOLDOWN_SECONDS:
            state = {
                "status": "blocked",
                "reason": "repair-cooldown",
                "checked_at": timestamp,
                "guard_healthy": guard_healthy,
                "mcp_healthy": mcp_healthy,
                "consecutive_failures": int(previous.get("consecutive_failures", 0)) + 1,
                "last_repair_at": last_repair,
                "repairs": [],
            }
            _atomic_json(HEALTH_STATE, state)
            print(json.dumps(state, sort_keys=True), file=sys.stderr)
            return 1
        repairs: list[str] = []
        if not guard_healthy:
            restarted, _detail = restart_guard_runtime()
            repairs.append("guard-restart")
            guard_healthy = restarted and _wait_for_stack(guard=True)
        if guard_healthy:
            _guard_now, mcp_healthy = _guard_stack_status()
            if not mcp_healthy:
                restarted, _detail = restart_mcp_http_runtime()
                repairs.append("mcp-restart")
                mcp_healthy = restarted and _wait_for_stack(guard=True, mcp=True)
        guard_healthy, mcp_healthy = _guard_stack_status()
        recovered = guard_healthy and mcp_healthy
        state = {
            "status": "recovered" if recovered else "blocked",
            "checked_at": timestamp,
            "guard_healthy": guard_healthy,
            "mcp_healthy": mcp_healthy,
            "consecutive_failures": 0 if recovered else int(previous.get("consecutive_failures", 0)) + 1,
            "last_repair_at": timestamp,
            "repairs": repairs,
        }
        _atomic_json(HEALTH_STATE, state)
        print(json.dumps(state, sort_keys=True), file=sys.stdout if recovered else sys.stderr)
        return 0 if recovered else 1
    finally:
        _release_operation_lock(token)


def health_monitor(action: str) -> int:
    if action == "run":
        return health_monitor_run()
    if action == "status":
        if not health_monitor_enabled():
            print(json.dumps({"status": "disabled"}))
            return 0
        if not HEALTH_STATE.exists():
            print(json.dumps({"status": "not-run"}))
            return 1
        print(HEALTH_STATE.read_text(encoding="utf-8"), end="")
        try:
            state = json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 1
        fresh = int(time.time()) - int(state.get("checked_at", 0)) <= 180
        return 0 if fresh and state.get("status") in {"ok", "recovered"} else 1
    if action == "remove":
        remove_health_monitor()
        _atomic_json(HEALTH_CONFIG, {"enabled": False, "interval_seconds": 60})
        HEALTH_STATE.unlink(missing_ok=True)
        print("Health monitor removed; guard services and secrets were preserved.")
        return 0
    check = health_monitor_run()
    ok, detail = install_health_monitor()
    if ok:
        _atomic_json(HEALTH_CONFIG, {"enabled": True, "interval_seconds": 60})
    print(f"{'Health monitor installed' if ok else 'Health monitor installation failed'}: {detail}")
    return 0 if ok and check == 0 else 1


def auto_update(action: str, interval_hours: int = 24, require_signed_commits: bool = False, scheduler: bool = True) -> int:
    if action == "enable":
        _atomic_json(UPDATE_CONFIG, {
            "enabled": True,
            "interval_hours": max(1, interval_hours),
            "require_signed_commits": require_signed_commits,
            "repository": REPO_URL,
            "claude_command": shutil.which("claude") or "",
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
    due = (
        (
            TARGETS["claude"].is_symlink()
            and health_monitor_enabled()
            and (not HEALTH_CONFIG.exists() or not HEALTH_STATE.exists())
        )
        or
        last.get("status") != "ok"
        or int(time.time()) - int(last.get("checked_at", 0)) >= int(config["interval_hours"]) * 3600
    )
    if not due:
        print("Update check is not due yet.")
        return 0
    return update(bool(config.get("require_signed_commits")), config.get("claude_command") or None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and diagnose BLUN Language Guard")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--target", action="append", choices=tuple(TARGETS), dest="targets")
    install_parser.add_argument("--no-service-autostart", action="store_true")
    service_parser = sub.add_parser("service")
    service_parser.add_argument("action", choices=("install", "start", "stop", "status"))
    mcp_service_parser = sub.add_parser("mcp-service")
    mcp_service_parser.add_argument("action", choices=("install", "start", "stop", "status"))
    monitor_parser = sub.add_parser("health-monitor")
    monitor_parser.add_argument("action", choices=("install", "remove", "run", "status"))
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
        return install(args.targets or list(TARGETS), autostart_service=not args.no_service_autostart)
    if args.command == "service":
        return guard_service(args.action)
    if args.command == "mcp-service":
        return mcp_service(args.action)
    if args.command == "health-monitor":
        return health_monitor(args.action)
    if args.command == "doctor":
        return doctor()
    if args.command == "update":
        return update(args.require_signed_commits)
    return auto_update(args.action, args.interval_hours, args.require_signed_commits, not args.no_scheduler)


if __name__ == "__main__":
    raise SystemExit(main())
