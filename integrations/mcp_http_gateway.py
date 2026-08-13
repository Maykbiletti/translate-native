#!/usr/bin/env python3
"""Authenticated, stateless Streamable HTTP transport for BLUN Language Guard MCP."""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import ipaddress
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "translate-native" / "scripts" / "blun_language_guard.py"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47632
DEFAULT_MCP_PATH = "/mcp"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18"}


def _load_guard():
    spec = importlib.util.spec_from_file_location("blun_http_language_guard", GUARD_PATH)
    if not spec or not spec.loader:
        raise RuntimeError("Cannot load the BLUN Language Guard MCP implementation")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GUARD = _load_guard()


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_access_token(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"MCP access-token file does not exist: {path}")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError(f"MCP access-token permissions must be owner-only: {path}")
    token = path.read_text(encoding="utf-8-sig").strip()
    if len(token) < 32:
        raise RuntimeError("MCP access token must contain at least 32 characters")
    return token


class MCPHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class MCPRequestHandler(BaseHTTPRequestHandler):
    server_version = "BLUNLanguageGuardMCP"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        # Request bodies and authorization headers must never reach logs.
        return

    @property
    def mcp_server(self) -> MCPHTTPServer:
        return self.server  # type: ignore[return-value]

    def _send_empty(self, status: int, *, allow: str | None = None) -> None:
        self.send_response(status)
        if allow:
            self.send_header("Allow", allow)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        authenticate: bool = False,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if authenticate:
            self.send_header("WWW-Authenticate", 'Bearer realm="blun-language-guard"')
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _jsonrpc_error(self, status: int, code: int, message: str, *, request_id: Any = None) -> None:
        self._send_json(status, {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and _is_loopback(parsed.hostname or "")

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.mcp_server.access_token}"  # type: ignore[attr-defined]
        return hmac.compare_digest(supplied, expected)

    def _authorize_request(self) -> bool:
        if not self._origin_allowed():
            self._jsonrpc_error(403, -32001, "Forbidden origin")
            return False
        if not self._authorized():
            payload = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32001, "message": "Unauthorized"},
            }
            self._send_json(401, payload, authenticate=True)
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/healthz":
            if not self._authorize_request():
                return
            try:
                health = GUARD.SERVICE_CLIENT.call_guard_service(
                    GUARD.SERVICE_ENDPOINT,
                    {"operation": "health"},
                    auth_token=GUARD._service_token(),
                    timeout=3.0,
                )
                healthy = health.get("status") == "ok" and health.get("isolated_key") is True
            except (OSError, RuntimeError, ValueError, GUARD.SERVICE_CLIENT.GuardServiceError):
                healthy = False
                health = {"status": "BLOCK", "service": "blun-language-guard"}
            self._send_json(200 if healthy else 503, health)
            return
        if self.path == self.mcp_server.mcp_path:  # type: ignore[attr-defined]
            self._send_empty(405, allow="POST")
            return
        self._send_empty(404)

    def do_DELETE(self) -> None:
        if self.path == self.mcp_server.mcp_path:  # type: ignore[attr-defined]
            self._send_empty(405, allow="POST")
            return
        self._send_empty(404)

    def do_POST(self) -> None:
        if self.path != self.mcp_server.mcp_path:  # type: ignore[attr-defined]
            self._send_empty(404)
            return
        if not self._authorize_request():
            return
        media_type = self.headers.get("Content-Type", "").partition(";")[0].strip().casefold()
        if media_type != "application/json":
            self._jsonrpc_error(415, -32600, "Content-Type must be application/json")
            return
        accepted = {item.partition(";")[0].strip().casefold() for item in self.headers.get("Accept", "").split(",")}
        if not ({"application/json", "text/event-stream"} <= accepted or "*/*" in accepted):
            self._jsonrpc_error(406, -32600, "Accept must include application/json and text/event-stream")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._jsonrpc_error(411, -32600, "Content-Length is required")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._jsonrpc_error(413, -32600, "MCP request is too large")
            return
        try:
            raw = self.rfile.read(length)
            message = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._jsonrpc_error(400, -32700, "Invalid UTF-8 JSON")
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._jsonrpc_error(400, -32600, "MCP message must be one JSON-RPC 2.0 object")
            return
        method = message.get("method")
        if method != "initialize":
            protocol = self.headers.get("MCP-Protocol-Version", "2025-03-26")
            if protocol not in SUPPORTED_PROTOCOL_VERSIONS:
                self._jsonrpc_error(400, -32600, "Unsupported MCP protocol version", request_id=message.get("id"))
                return
        try:
            if isinstance(method, str):
                response = GUARD.handle_message(message)
                if message.get("id") is None:
                    self._send_empty(202)
                elif response is None:
                    self._jsonrpc_error(500, -32603, "MCP request produced no response", request_id=message.get("id"))
                else:
                    self._send_json(200, response)
                return
            if "result" in message or "error" in message:
                self._send_empty(202)
                return
            self._jsonrpc_error(400, -32600, "Invalid JSON-RPC message", request_id=message.get("id"))
        except Exception:
            # One malformed or failing request must never terminate the persistent service.
            self._jsonrpc_error(500, -32603, "Internal MCP failure", request_id=message.get("id"))


def build_server(host: str, port: int, access_token: str, mcp_path: str = DEFAULT_MCP_PATH) -> MCPHTTPServer:
    if not _is_loopback(host):
        raise ValueError("the local MCP gateway may bind only to a loopback address")
    if not access_token or len(access_token) < 32:
        raise ValueError("MCP access token must contain at least 32 characters")
    path = "/" + mcp_path.strip("/")
    server = MCPHTTPServer((host, port), MCPRequestHandler)
    server.access_token = access_token  # type: ignore[attr-defined]
    server.mcp_path = path  # type: ignore[attr-defined]
    return server


def main() -> int:
    runtime = Path.home() / ".config" / "blun-language-guard"
    parser = argparse.ArgumentParser(description="Run the persistent BLUN Streamable HTTP MCP gateway")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--path", default=DEFAULT_MCP_PATH)
    parser.add_argument("--access-token-file", type=Path, default=runtime / "mcp-http.token")
    parser.add_argument("--service-endpoint", required=True)
    parser.add_argument("--service-token-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        access_token = load_access_token(args.access_token_file)
        if not args.service_token_file.is_file():
            raise RuntimeError(f"Guard service-token file does not exist: {args.service_token_file}")
        GUARD.SERVICE_ENDPOINT = args.service_endpoint
        GUARD.SERVICE_TOKEN_FILE = str(args.service_token_file)
        server = build_server(args.host, args.port, access_token, args.path)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"BLOCK: {error}", file=sys.stderr)
        return 1

    def stop(_signum=None, _frame=None) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
