#!/usr/bin/env python3
"""Isolated signer/verifier service for mandatory language-gated delivery."""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import os
import signal
import socket
import socketserver
import stat
import sys
import threading
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "integrations" / "language_gateway.py"
AUDIT_PATH = ROOT / "integrations" / "audit_log.py"
CLIENT_PATH = ROOT / "translate-native" / "scripts" / "guard_service_client.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GATEWAY = _load("blun_isolated_gateway", GATEWAY_PATH)
AUDIT = _load("blun_isolated_audit", AUDIT_PATH)
CLIENT = _load("blun_isolated_client", CLIENT_PATH)
QUALITY = GATEWAY.GUARD.QUALITY
MAX_REQUEST_BYTES = 8 * 1024 * 1024


class GuardProtocolError(ValueError):
    """Raised for malformed or unauthorized local service requests."""


def _exact_string(payload: dict[str, Any], name: str, *, required: bool = True) -> str:
    value = payload.get(name, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise GuardProtocolError(f"{name} must be a string")
    return value


def _decision_audit(request: dict[str, Any], result: dict[str, Any], event: str) -> dict[str, Any]:
    source = request.get("source_text", "") if isinstance(request.get("source_text", ""), str) else ""
    target = request.get("target_text", "") if isinstance(request.get("target_text", ""), str) else ""
    return {
        "event": event,
        "allowed": result.get("release_allowed") is True or result.get("valid") is True,
        "task_kind": request.get("task_kind"),
        "language": request.get("language"),
        "agent_id": request.get("agent_id"),
        "channel": request.get("channel"),
        "source_sha256": QUALITY.canonical_hash(source) if source else "",
        "target_sha256": QUALITY.canonical_hash(target) if target else "",
        "guard_version": QUALITY.VERSION,
        "codes": AUDIT.finding_codes(result),
    }


class GuardService:
    def __init__(self, key_path: Path, audit_path: Path, service_token: str = "") -> None:
        self.key_path = key_path
        self.audit_path = audit_path
        self.service_token = service_token
        self.key = QUALITY.load_or_create_key(key_path)
        if os.name != "nt" and key_path.stat().st_mode & 0o077:
            raise RuntimeError("guard signing key permissions must be owner-only")
        GATEWAY.GUARD.KEY_PATH = key_path
        GATEWAY.GUARD.SERVICE_ENDPOINT = ""

    def _authorize(self, request: dict[str, Any]) -> None:
        supplied = request.pop("service_token", "")
        if self.service_token and (
            not isinstance(supplied, str)
            or not hmac.compare_digest(supplied, self.service_token)
        ):
            raise GuardProtocolError("unauthorized")

    def handle(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw_request, dict):
            raise GuardProtocolError("request must be an object")
        request = dict(raw_request)
        self._authorize(request)
        operation = request.pop("operation", "")
        if operation == "health":
            return {
                "status": "ok",
                "service": "blun-language-guard",
                "version": QUALITY.VERSION,
                "isolated_key": True,
            }
        if operation == "release":
            result = GATEWAY.gate(request)
            AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "release"))
            return result
        if operation == "verify":
            task_kind = _exact_string(request, "task_kind")
            if task_kind not in {"response", "translation"}:
                raise GuardProtocolError("invalid task_kind")
            source = _exact_string(request, "source_text", required=False)
            if task_kind == "translation" and not source.strip():
                raise GuardProtocolError("translation verification requires source_text")
            if task_kind == "response" and source:
                raise GuardProtocolError("response verification cannot contain source_text")
            result = QUALITY.verify_receipt(
                _exact_string(request, "release_token"),
                source,
                _exact_string(request, "target_text"),
                _exact_string(request, "language"),
                self.key,
                request.get("content_type", "prose"),
                request.get("short_text_reviewed") is True,
                purpose=task_kind,
            )
            result["status"] = "PASS" if result.get("valid") else "BLOCK"
            AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "verify"))
            return result
        raise GuardProtocolError("unknown operation")


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise GuardProtocolError("request too large")
            request = json.loads(raw.decode("utf-8-sig"))
            response = self.server.guard_service.handle(request)  # type: ignore[attr-defined]
        except (GuardProtocolError, UnicodeDecodeError, json.JSONDecodeError) as error:
            response = {"status": "BLOCK", "release_allowed": False, "error": str(error)}
        except Exception:
            response = {"status": "BLOCK", "release_allowed": False, "error": "internal guard failure"}
        encoded = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.wfile.write(encoded)


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if hasattr(socketserver, "UnixStreamServer"):
    class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):  # type: ignore[misc]
        daemon_threads = True


def _token_from_file(path: Path | None) -> str:
    if path is None:
        return ""
    token = path.read_text(encoding="utf-8-sig").strip()
    if len(token) < 32:
        raise RuntimeError("service token must contain at least 32 characters")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError("service token permissions must be owner-only")
    return token


def build_server(endpoint: str, service: GuardService):
    transport, address = CLIENT.parse_endpoint(endpoint)
    if transport == "unix":
        if not hasattr(socketserver, "UnixStreamServer"):
            raise RuntimeError("Unix sockets are unavailable")
        socket_path = Path(str(address))
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists() or socket_path.is_symlink():
            mode = socket_path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"refusing to replace non-socket path: {socket_path}")
            socket_path.unlink()
        server = _ThreadingUnixServer(str(socket_path), _RequestHandler)  # type: ignore[name-defined]
        os.chmod(socket_path, 0o660)
        server.socket_path = socket_path
    else:
        server = _ThreadingTCPServer(address, _RequestHandler)
        server.socket_path = None
    server.guard_service = service
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated BLUN Language Guard signer")
    default_runtime = Path.home() / ".config" / "blun-language-guard"
    parser.add_argument("--endpoint", default=f"unix:{default_runtime / 'guard.sock'}" if os.name != "nt" else "tcp:127.0.0.1:47631")
    parser.add_argument("--key-file", type=Path, default=default_runtime / "signing.key")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--audit-file", type=Path, default=default_runtime / "audit.jsonl")
    args = parser.parse_args()
    try:
        service = GuardService(args.key_file, args.audit_file, _token_from_file(args.token_file))
        server = build_server(args.endpoint, service)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"BLOCK: {error}", file=sys.stderr)
        return 1

    def stop(_signum=None, _frame=None) -> None:
        # BaseServer.shutdown() must run outside the serve_forever() thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        socket_path = getattr(server, "socket_path", None)
        if socket_path:
            try:
                if socket_path.exists() and stat.S_ISSOCK(socket_path.lstat().st_mode):
                    socket_path.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
