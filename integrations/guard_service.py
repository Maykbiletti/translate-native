#!/usr/bin/env python3
"""Isolated signer/verifier service for mandatory language-gated delivery."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
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


def _content_type(payload: dict[str, Any]) -> str:
    value = payload.get("content_type", "prose")
    if not isinstance(value, str) or not value.strip():
        raise GuardProtocolError("content_type must be a string")
    return value


def _exact_hash(payload: dict[str, Any], name: str) -> str:
    value = _exact_string(payload, name)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise GuardProtocolError(f"{name} must be a lowercase SHA-256 hash")
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
        self.boot_id = QUALITY._b64encode(os.urandom(12))
        self.consumed_delivery_nonces: dict[str, int] = {}
        self.session_epochs: dict[str, str] = {}
        self.session_epoch_history: dict[str, set[str]] = {}
        self.delivery_lock = threading.Lock()
        if os.name != "nt" and key_path.stat().st_mode & 0o077:
            raise RuntimeError("guard signing key permissions must be owner-only")
        GATEWAY.GUARD.KEY_PATH = key_path
        GATEWAY.GUARD.SERVICE_ENDPOINT = ""

    def _health_self_test(self) -> dict[str, bool]:
        """Exercise the real response gate and signer without writing a canary audit record."""
        target = "Hälsokontrollen är aktiv."
        try:
            released = GATEWAY.gate({
                "task_kind": "response",
                "source_text": "",
                "target_text": target,
                "language": "sv-SE",
                "attestations": {"nativeness": True, "orthography": True},
            })
            release_ok = released.get("release_allowed") is True
            token = released.get("release_token", "") if release_ok else ""
            verified = QUALITY.verify_receipt(
                token, "", target, "sv-SE", self.key, purpose="response"
            )
            tampered = QUALITY.verify_receipt(
                token, "", target + " Ändrad.", "sv-SE", self.key, purpose="response"
            )
            return {
                "release": release_ok,
                "signature": verified.get("valid") is True,
                "tamper_blocked": tampered.get("valid") is False,
            }
        except Exception:
            return {"release": False, "signature": False, "tamper_blocked": False}

    @staticmethod
    def _identity_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _verify_release(self, request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
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
            _content_type(request),
            request.get("short_text_reviewed") is True,
            purpose=task_kind,
        )
        return task_kind, source, result

    def _issue_delivery_grant(self, request: dict[str, Any], task_kind: str) -> str:
        now = int(time.time())
        payload = {
            "v": QUALITY.VERSION,
            "boot": self.boot_id,
            "source_sha256": QUALITY.canonical_hash(_exact_string(request, "source_text", required=False)),
            "target_sha256": QUALITY.canonical_hash(_exact_string(request, "target_text")),
            "session_sha256": self._identity_hash(_exact_string(request, "session_id")),
            "session_epoch_sha256": self._identity_hash(_exact_string(request, "session_epoch")),
            "agent_sha256": self._identity_hash(_exact_string(request, "agent_id")),
            "language": _exact_string(request, "language"),
            "purpose": task_kind,
            "content_type": _content_type(request),
            "short_text_reviewed": request.get("short_text_reviewed") is True,
            "channel": _exact_string(request, "channel"),
            "iat": now,
            "exp": now + 600,
            "nonce": QUALITY._b64encode(os.urandom(16)),
        }
        encoded = QUALITY._b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = QUALITY._b64encode(hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"blgd2.{encoded}.{signature}"

    def _register_session_epoch(self, request: dict[str, Any]) -> dict[str, Any]:
        session_hash = self._identity_hash(_exact_string(request, "session_id"))
        epoch = _exact_string(request, "session_epoch")
        if re.fullmatch(r"[0-9a-f]{64}", epoch) is None:
            raise GuardProtocolError("session_epoch must be 64 lowercase hexadecimal characters")
        epoch_hash = self._identity_hash(epoch)
        with self.delivery_lock:
            history = self.session_epoch_history.setdefault(session_hash, set())
            if epoch_hash in history:
                return {"status": "BLOCK", "registered": False}
            history.add(epoch_hash)
            self.session_epochs[session_hash] = epoch_hash
        return {"status": "PASS", "registered": True}

    def _retire_session_epoch(self, request: dict[str, Any]) -> dict[str, Any]:
        session_hash = self._identity_hash(_exact_string(request, "session_id"))
        epoch = _exact_string(request, "session_epoch")
        if re.fullmatch(r"[0-9a-f]{64}", epoch) is None:
            raise GuardProtocolError("session_epoch must be 64 lowercase hexadecimal characters")
        expected_epoch_hash = self._identity_hash(epoch)
        tombstone_hash = self._identity_hash(os.urandom(32).hex())
        with self.delivery_lock:
            if self.session_epochs.get(session_hash) != expected_epoch_hash:
                return {"status": "BLOCK", "retired": False}
            history = self.session_epoch_history.setdefault(session_hash, set())
            history.add(tombstone_hash)
            self.session_epochs[session_hash] = tombstone_hash
        return {"status": "PASS", "retired": True}

    def _authorize_delivery(self, request: dict[str, Any], task_kind: str) -> dict[str, Any]:
        session_hash = self._identity_hash(_exact_string(request, "session_id"))
        epoch = _exact_string(request, "session_epoch")
        if re.fullmatch(r"[0-9a-f]{64}", epoch) is None:
            raise GuardProtocolError("session_epoch must be 64 lowercase hexadecimal characters")
        epoch_hash = self._identity_hash(epoch)
        with self.delivery_lock:
            current_epoch = self.session_epochs.get(session_hash)
            history = self.session_epoch_history.setdefault(session_hash, set())
            if current_epoch is None and epoch_hash not in history:
                history.add(epoch_hash)
                self.session_epochs[session_hash] = epoch_hash
                current_epoch = epoch_hash
            if current_epoch != epoch_hash:
                return {
                    "valid": False,
                    "status": "BLOCK",
                    "checks": {"session_epoch_current": False},
                }
            return {
                "valid": True,
                "status": "PASS",
                "delivery_grant": self._issue_delivery_grant(request, task_kind),
                "expires_in": 600,
            }

    def _consume_delivery_grant(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            prefix, encoded, signature = _exact_string(request, "delivery_grant").split(".")
            expected = QUALITY._b64encode(hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest())
            if prefix != "blgd2" or not hmac.compare_digest(signature, expected):
                raise ValueError("invalid delivery grant signature")
            payload = json.loads(QUALITY._b64decode(encoded))
            if not isinstance(payload, dict):
                raise ValueError("invalid delivery grant payload")
            nonce = payload.get("nonce")
            if not isinstance(nonce, str) or not nonce:
                raise ValueError("invalid delivery grant nonce")
            now = int(time.time())
            session_hash = self._identity_hash(_exact_string(request, "session_id"))
            epoch_hash = self._identity_hash(_exact_string(request, "session_epoch"))
            checks = {
                "target": payload.get("target_sha256") == QUALITY.canonical_hash(_exact_string(request, "target_text")),
                "source": payload.get("source_sha256") == _exact_hash(request, "source_sha256"),
                "session": payload.get("session_sha256") == session_hash,
                "session_epoch": payload.get("session_epoch_sha256") == epoch_hash,
                "agent": payload.get("agent_sha256") == self._identity_hash(_exact_string(request, "agent_id")),
                "language": payload.get("language") == _exact_string(request, "language"),
                "purpose": payload.get("purpose") == _exact_string(request, "task_kind"),
                "content_type": payload.get("content_type") == _content_type(request),
                "short_text_reviewed": payload.get("short_text_reviewed") is (request.get("short_text_reviewed") is True),
                "channel": payload.get("channel") == _exact_string(request, "channel"),
                "version": payload.get("v") == QUALITY.VERSION,
                "service_boot": payload.get("boot") == self.boot_id,
                "not_expired": int(payload.get("exp", 0)) >= now,
            }
            with self.delivery_lock:
                checks["session_epoch_current"] = self.session_epochs.get(session_hash) == epoch_hash
                self.consumed_delivery_nonces = {
                    used_nonce: expiry
                    for used_nonce, expiry in self.consumed_delivery_nonces.items()
                    if expiry >= now
                }
                checks["one_time"] = nonce not in self.consumed_delivery_nonces
                identity_valid = all(
                    checks[name]
                    for name in ("session", "session_epoch", "session_epoch_current", "agent", "version", "service_boot", "not_expired", "one_time")
                )
                valid = all(checks.values())
                if identity_valid:
                    self.consumed_delivery_nonces[nonce] = int(payload["exp"])
            return {"valid": valid, "status": "PASS" if valid else "BLOCK", "checks": checks}
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
            return {"valid": False, "status": "BLOCK", "error": str(error)}

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
            self_test = self._health_self_test()
            healthy = all(self_test.values())
            return {
                "status": "ok" if healthy else "BLOCK",
                "service": "blun-language-guard",
                "version": QUALITY.VERSION,
                "isolated_key": healthy,
                "self_test": self_test,
            }
        if operation == "release":
            result = GATEWAY.gate(request)
            AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "release"))
            return result
        if operation == "verify":
            _, _, result = self._verify_release(request)
            result["status"] = "PASS" if result.get("valid") else "BLOCK"
            AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "verify"))
            return result
        if operation == "register_session_epoch":
            return self._register_session_epoch(request)
        if operation == "retire_session_epoch":
            return self._retire_session_epoch(request)
        if operation == "authorize_delivery":
            task_kind, _, result = self._verify_release(request)
            if result.get("valid"):
                result = self._authorize_delivery(request, task_kind)
            else:
                result["status"] = "BLOCK"
            AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "authorize-delivery"))
            return result
        if operation == "consume_delivery":
            result = self._consume_delivery_grant(request)
            AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "consume-delivery"))
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
    return CLIENT.load_service_token(path)


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
