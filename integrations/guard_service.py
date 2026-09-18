#!/usr/bin/env python3
"""Isolated signer/verifier service for mandatory language-gated delivery."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
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
RESPONSE_REVIEW_PATH = ROOT / "integrations" / "response_subagent_review.py"
RESPONSE_REVIEW_RUNTIME_PATH = (
    ROOT / "integrations" / "response_subagent_https_runtime.py"
)


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
RESPONSE_REVIEW = _load("blun_response_subagent_review", RESPONSE_REVIEW_PATH)
RESPONSE_REVIEW_RUNTIME = _load(
    "blun_response_subagent_https_runtime", RESPONSE_REVIEW_RUNTIME_PATH,
)
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
    exact_rewrite = request.get("task_kind") == "rewrite"
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    return {
        "event": event,
        "allowed": result.get("release_allowed") is True or result.get("valid") is True,
        "task_kind": request.get("task_kind"),
        "language": request.get("language"),
        "agent_id": request.get("agent_id"),
        "channel": request.get("channel"),
        "source_sha256": (digest(source) if exact_rewrite else QUALITY.canonical_hash(source)) if source else "",
        "target_sha256": (digest(target) if exact_rewrite else QUALITY.canonical_hash(target)) if target else "",
        "guard_version": QUALITY.VERSION,
        "codes": AUDIT.finding_codes(result),
    }


class GuardService:
    def __init__(self, key_path: Path, audit_path: Path, service_token: str = "",
                 response_reviewer: Any | None = None,
                 rewrite_workers: dict[str, Any] | None = None) -> None:
        self.key_path = key_path
        self.audit_path = audit_path
        self.service_token = service_token
        self.response_reviewer = response_reviewer
        # Operator-owned registry, never populated from an agent request.
        self.rewrite_workers = dict(rewrite_workers or {})
        if any(not isinstance(name, str) or not name.strip()
               or not callable(getattr(worker, "run", None))
               or not isinstance(getattr(worker, "locale", None), str)
               or re.fullmatch(r"[0-9a-f]{64}", getattr(worker, "profile_sha256", "")) is None
               for name, worker in self.rewrite_workers.items()):
            raise ValueError("invalid trusted rewrite worker registry")
        self.key = QUALITY.load_or_create_key(key_path)
        self.boot_id = QUALITY._b64encode(os.urandom(12))
        self.consumed_delivery_nonces: dict[str, int] = {}
        self.consumed_review_context_nonces: dict[str, int] = {}
        self.consumed_rewrite_context_nonces: dict[str, int] = {}
        self.authorized_rewrite_receipts: dict[str, dict[str, Any]] = {}
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
        audit_paths = AUDIT.audit_paths_healthy(self.audit_path)
        try:
            binding = {
                "response_session_sha256": "1" * 64,
                "response_session_epoch_sha256": "2" * 64,
                "response_agent_sha256": "3" * 64,
                "response_guard_boot_sha256": self._identity_hash(self.boot_id),
            }
            released = GATEWAY.gate({
                "task_kind": "response",
                "source_text": "",
                "target_text": target,
                "language": "sv-SE",
            }, response_review_sha256="0" * 64,
                response_context_binding=binding)
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
                "audit_paths": audit_paths,
                "response_review_configured": self.response_reviewer is not None,
            }
        except Exception:
            return {
                "release": False,
                "signature": False,
                "tamper_blocked": False,
                "audit_paths": audit_paths,
                "response_review_configured": self.response_reviewer is not None,
            }

    def _issue_response_review_context(self, request: dict[str, Any]) -> str:
        if self.response_reviewer is None:
            raise GuardProtocolError("response review host is unavailable")
        if _exact_string(request, "task_kind") != "response":
            raise GuardProtocolError("response review context requires response task_kind")
        target = _exact_string(request, "target_text")
        language = _exact_string(request, "language")
        session_id = _exact_string(request, "session_id")
        session_epoch = _exact_string(request, "session_epoch")
        agent_id = _exact_string(request, "agent_id")
        if re.fullmatch(r"[0-9a-f]{64}", session_epoch) is None:
            raise GuardProtocolError("session_epoch must be 64 lowercase hexadecimal characters")
        session_hash = self._identity_hash(session_id)
        epoch_hash = self._identity_hash(session_epoch)
        with self.delivery_lock:
            if self.session_epochs.get(session_hash) != epoch_hash:
                raise GuardProtocolError("session epoch is not current")
        now = int(time.time())
        payload = {
            "v": QUALITY.VERSION,
            "boot": self.boot_id,
            "target_sha256": QUALITY.canonical_hash(target),
            "language": language,
            "content_type": _content_type(request),
            "session_sha256": session_hash,
            "session_epoch_sha256": epoch_hash,
            "agent_sha256": self._identity_hash(agent_id),
            "iat": now,
            "exp": now + 180,
            "nonce": QUALITY._b64encode(os.urandom(16)),
        }
        encoded = QUALITY._b64encode(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        signature = QUALITY._b64encode(hmac.new(
            self.key, encoded.encode("ascii"), hashlib.sha256,
        ).digest())
        return f"blrr1.{encoded}.{signature}"

    def _consume_response_review_context(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            prefix, encoded, signature = _exact_string(
                request, "review_context_token",
            ).split(".")
            expected = QUALITY._b64encode(hmac.new(
                self.key, encoded.encode("ascii"), hashlib.sha256,
            ).digest())
            if prefix != "blrr1" or not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(QUALITY._b64decode(encoded))
            if not isinstance(payload, dict) or set(payload) != {
                "v", "boot", "target_sha256", "language", "content_type",
                "session_sha256", "session_epoch_sha256", "agent_sha256",
                "iat", "exp", "nonce",
            }:
                raise ValueError
            nonce = payload["nonce"]
            now = int(time.time())
            checks = {
                "version": payload["v"] == QUALITY.VERSION,
                "boot": payload["boot"] == self.boot_id,
                "target": payload["target_sha256"] == QUALITY.canonical_hash(
                    _exact_string(request, "target_text")),
                "language": payload["language"] == _exact_string(request, "language"),
                "content_type": payload["content_type"] == _content_type(request),
                "time": type(payload["exp"]) is int and type(payload["iat"]) is int
                        and payload["iat"] <= now <= payload["exp"],
                "nonce": isinstance(nonce, str) and bool(nonce),
            }
            if not all(checks.values()):
                raise ValueError
            with self.delivery_lock:
                if self.session_epochs.get(payload["session_sha256"]) != payload["session_epoch_sha256"]:
                    raise ValueError
                expired = [item for item, expiry in self.consumed_review_context_nonces.items()
                           if expiry < now]
                for item in expired:
                    self.consumed_review_context_nonces.pop(item, None)
                if nonce in self.consumed_review_context_nonces:
                    raise ValueError
                # Reserve before external work. A retry needs a fresh host context.
                self.consumed_review_context_nonces[nonce] = payload["exp"]
            return payload
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            raise GuardProtocolError("invalid or replayed response review context") from None

    def _release_reviewed_response(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.response_reviewer is None:
            return {"status": "BLOCK", "release_allowed": False,
                    "reason": "response-review-host-unavailable"}
        try:
            context = self._consume_response_review_context(request)
            reviewed = self.response_reviewer.review(
                _exact_string(request, "target_text"),
                _exact_string(request, "language"),
                _content_type(request),
                creator_id_sha256=context["agent_sha256"],
                creator_session_id_sha256=context["session_sha256"],
            )
            digest = reviewed.get("evidence_sha256") if isinstance(reviewed, dict) else None
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise RESPONSE_REVIEW.ResponseReviewBlocked("evidence_invalid")
            binding = {
                "response_session_sha256": context["session_sha256"],
                "response_session_epoch_sha256": context["session_epoch_sha256"],
                "response_agent_sha256": context["agent_sha256"],
                "response_guard_boot_sha256": self._identity_hash(self.boot_id),
            }
            # Recheck the epoch after the external review and hold the same
            # lock through signing. A prompt/session transition therefore
            # cannot race an old candidate into a new delivery epoch.
            with self.delivery_lock:
                if (self.session_epochs.get(context["session_sha256"])
                        != context["session_epoch_sha256"]):
                    raise RESPONSE_REVIEW.ResponseReviewBlocked("context_stale")
                result = GATEWAY.gate(
                    request,
                    response_review_sha256=digest,
                    response_context_binding=binding,
                )
            if result.get("release_allowed"):
                result["response_review_sha256"] = digest
            return result
        except RESPONSE_REVIEW.ResponseReviewBlocked as error:
            return {"status": "BLOCK", "release_allowed": False,
                    "reason": error.code, "retryable": error.retryable}

    @staticmethod
    def _identity_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _rewrite_worker(self, request: dict[str, Any]) -> Any:
        worker = self.rewrite_workers.get(_exact_string(request, "profile_id"))
        if worker is None or worker.locale != _exact_string(request, "language"):
            raise GuardProtocolError("rewrite profile unavailable or locale mismatch")
        return worker

    def _issue_rewrite_context(self, request: dict[str, Any]) -> str:
        """Bind trusted-host rewrite selection before any model receives text."""
        if _exact_string(request, "task_kind") != "rewrite":
            raise GuardProtocolError("rewrite context requires rewrite task_kind")
        worker = self._rewrite_worker(request)
        source = _exact_string(request, "source_text")
        request_id = _exact_string(request, "request_id")
        session_id = _exact_string(request, "session_id")
        session_epoch = _exact_string(request, "session_epoch")
        agent_id = _exact_string(request, "agent_id")
        if re.fullmatch(r"[0-9a-f]{64}", session_epoch) is None:
            raise GuardProtocolError("session_epoch must be 64 lowercase hexadecimal characters")
        session_hash = self._identity_hash(session_id)
        epoch_hash = self._identity_hash(session_epoch)
        with self.delivery_lock:
            if self.session_epochs.get(session_hash) != epoch_hash:
                raise GuardProtocolError("session epoch is not current")
        now = int(time.time())
        payload = {
            "v": QUALITY.VERSION,
            "boot": self.boot_id,
            "task_kind": "rewrite",
            "source_sha256": self._identity_hash(source),
            "language": worker.locale,
            "content_type": _content_type(request),
            "profile_id": _exact_string(request, "profile_id"),
            "profile_sha256": worker.profile_sha256,
            "request_id_sha256": self._identity_hash(request_id),
            "session_sha256": session_hash,
            "session_epoch_sha256": epoch_hash,
            "agent_sha256": self._identity_hash(agent_id),
            "iat": now,
            "exp": now + 180,
            "nonce": QUALITY._b64encode(os.urandom(16)),
        }
        encoded = QUALITY._b64encode(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        signature = QUALITY._b64encode(hmac.new(
            self.key, encoded.encode("ascii"), hashlib.sha256,
        ).digest())
        return f"blrwc1.{encoded}.{signature}"

    def _consume_rewrite_context(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            prefix, encoded, signature = _exact_string(
                request, "rewrite_context_token",
            ).split(".")
            expected = QUALITY._b64encode(hmac.new(
                self.key, encoded.encode("ascii"), hashlib.sha256,
            ).digest())
            if prefix != "blrwc1" or not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(QUALITY._b64decode(encoded))
            expected_fields = {
                "v", "boot", "task_kind", "source_sha256", "language",
                "content_type", "profile_id", "profile_sha256",
                "request_id_sha256", "session_sha256",
                "session_epoch_sha256", "agent_sha256", "iat", "exp", "nonce",
            }
            if not isinstance(payload, dict) or set(payload) != expected_fields:
                raise ValueError
            worker = self._rewrite_worker(request)
            nonce = payload["nonce"]
            now = int(time.time())
            checks = {
                "version": payload["v"] == QUALITY.VERSION,
                "boot": payload["boot"] == self.boot_id,
                "task_kind": payload["task_kind"] == "rewrite",
                "source": payload["source_sha256"] == self._identity_hash(
                    _exact_string(request, "source_text")),
                "language": payload["language"] == worker.locale,
                "content_type": payload["content_type"] == _content_type(request),
                "profile": payload["profile_id"] == _exact_string(request, "profile_id")
                           and payload["profile_sha256"] == worker.profile_sha256,
                "request_id": payload["request_id_sha256"] == self._identity_hash(
                    _exact_string(request, "request_id")),
                "session": payload["session_sha256"] == self._identity_hash(
                    _exact_string(request, "session_id")),
                "session_epoch": payload["session_epoch_sha256"] == self._identity_hash(
                    _exact_string(request, "session_epoch")),
                "agent": payload["agent_sha256"] == self._identity_hash(
                    _exact_string(request, "agent_id")),
                "time": type(payload["iat"]) is int and type(payload["exp"]) is int
                        and payload["iat"] <= now <= payload["exp"],
                "nonce": isinstance(nonce, str) and bool(nonce),
            }
            if not all(checks.values()):
                raise ValueError
            with self.delivery_lock:
                if self.session_epochs.get(payload["session_sha256"]) != payload["session_epoch_sha256"]:
                    raise ValueError
                expired = [item for item, expiry in self.consumed_rewrite_context_nonces.items()
                           if expiry < now]
                for item in expired:
                    self.consumed_rewrite_context_nonces.pop(item, None)
                if nonce in self.consumed_rewrite_context_nonces:
                    raise ValueError
                # Reserve before creator or reviewer work. A retry needs a new
                # trusted-host context but keeps the same durable request ID.
                self.consumed_rewrite_context_nonces[nonce] = payload["exp"]
            return payload
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            raise GuardProtocolError("invalid or replayed rewrite context") from None

    def _rewrite_text(self, request: dict[str, Any]) -> dict[str, Any]:
        """Create and review internally. No caller-supplied candidate or attestations."""
        allowed = {"source_text", "language", "content_type", "request_id", "profile_id",
                   "rewrite_context_token", "session_id", "session_epoch", "agent_id"}
        if set(request) - allowed:
            raise GuardProtocolError("invalid rewrite request fields")
        rewrite_context = self._consume_rewrite_context(request)
        worker = self._rewrite_worker(request)
        source = _exact_string(request, "source_text")
        content_type = _content_type(request)
        profile_hash = worker.profile_sha256
        try:
            reviewed = worker.run(source, content_type, _exact_string(request, "request_id"))
            target = _exact_string(reviewed, "target_text")
            digest = _exact_hash(reviewed, "evidence_sha256")
            evidence = reviewed.get("evidence")
            if (not isinstance(evidence, dict)
                    or hashlib.sha256(json.dumps(evidence, ensure_ascii=False, allow_nan=False,
                        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() != digest
                    or reviewed.get("profile_sha256") != profile_hash
                    or worker.profile_sha256 != profile_hash):
                raise GuardProtocolError("rewrite evidence mismatch")
            expected_evidence = {
                "source_sha256": self._identity_hash(source),
                "target_sha256": self._identity_hash(target), "locale": worker.locale,
                "content_type": content_type, "profile_sha256": profile_hash,
                "request_id": request["request_id"], "integrity": "PASS",
            }
            if (any(evidence.get(key) != value for key, value in expected_evidence.items())
                    or not isinstance(evidence.get("reviews"), list)
                    or [item.get("phase") for item in evidence["reviews"]]
                    != ["target_native", "source_fidelity"]):
                raise GuardProtocolError("rewrite evidence binding mismatch")
            # Re-run deterministic validation in the sole signing authority.
            module = _load("blun_guard_native_rewrite", ROOT / "integrations" / "native_rewrite_worker.py")
            long_document = worker.is_long_document(source)
            if long_document:
                if (not worker.validate_document_evidence(
                        source, target, evidence.get("document"),
                        content_type=content_type, request_id=request["request_id"],
                        correction_history=evidence.get("correction_history"))
                        or [item.get("scope") for item in evidence["reviews"]]
                        != ["assembled_document", "assembled_document"]
                        or [item.get("reviewed_target_sha256")
                            for item in evidence["reviews"]]
                        != [self._identity_hash(target), self._identity_hash(target)]):
                    raise GuardProtocolError("long rewrite evidence binding mismatch")
            elif "document" in evidence or any("scope" in item for item in evidence["reviews"]):
                raise GuardProtocolError("unexpected long rewrite evidence")
            integrity = module.integrity_errors(source, target)
            report = GATEWAY.GUARD.validate_text(target, worker.locale, content_type=content_type,
                                               short_text_reviewed=True)
            if integrity or report["findings"]:
                return {"status": "BLOCK", "release_allowed": False,
                        "reason": "rewrite.deterministic_check_failed"}
            now = int(time.time())
            payload = {
                "schema": "translate-native.rewrite-release.v1", "v": QUALITY.VERSION,
                "purpose": "rewrite", "source_sha256": self._identity_hash(source),
                "target_sha256": self._identity_hash(target), "language": worker.locale,
                "content_type": content_type, "profile_id": request["profile_id"],
                "profile_sha256": profile_hash, "evidence_sha256": digest,
                "request_id_sha256": rewrite_context["request_id_sha256"],
                "session_sha256": rewrite_context["session_sha256"],
                "session_epoch_sha256": rewrite_context["session_epoch_sha256"],
                "agent_sha256": rewrite_context["agent_sha256"],
                "guard_boot_sha256": self._identity_hash(rewrite_context["boot"]),
                "rewrite_context_nonce_sha256": self._identity_hash(rewrite_context["nonce"]),
                "iat": now, "exp": now + 3600,
            }
            encoded = QUALITY._b64encode(json.dumps(payload, sort_keys=True,
                separators=(",", ":")).encode("utf-8"))
            signature = QUALITY._b64encode(hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest())
            return {"status": "PASS", "release_allowed": True, "task_kind": "rewrite",
                    "target_text": target, "release_token": f"blrw1.{encoded}.{signature}",
                    "evidence_sha256": digest, "profile_sha256": profile_hash,
                    "style_review": report["style_review"],
                    "limitations": "Reviewed output; not proof of human authorship or comparative quality."}
        except Exception as error:
            # Adapter errors must not expose source, candidate, or model reasoning.
            code = getattr(error, "code", "rewrite.worker_failed")
            if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", code) is None:
                code = "rewrite.worker_failed"
            return {"status": "BLOCK", "release_allowed": False, "reason": code}

    def _verify_rewrite(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            worker = self._rewrite_worker(request)
            prefix, encoded, signature = _exact_string(request, "release_token").split(".")
            expected = QUALITY._b64encode(hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest())
            if prefix != "blrw1" or not hmac.compare_digest(expected, signature):
                raise ValueError
            payload = json.loads(QUALITY._b64decode(encoded))
            now = int(time.time())
            supplied_context = all(
                isinstance(request.get(name), str) and bool(request[name])
                for name in ("request_id", "session_id", "session_epoch", "agent_id")
            )
            checks = {
                "schema": payload.get("schema") == "translate-native.rewrite-release.v1",
                "version": payload.get("v") == QUALITY.VERSION,
                "purpose": payload.get("purpose") == "rewrite",
                "source": payload.get("source_sha256") == self._identity_hash(_exact_string(request, "source_text")),
                "target": payload.get("target_sha256") == self._identity_hash(_exact_string(request, "target_text")),
                "language": payload.get("language") == worker.locale,
                "content_type": payload.get("content_type") == _content_type(request),
                "profile": payload.get("profile_id") == request["profile_id"]
                           and payload.get("profile_sha256") == worker.profile_sha256,
                "request_id": (
                    payload.get("request_id_sha256") == self._identity_hash(request["request_id"])
                    if supplied_context else isinstance(payload.get("request_id_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", payload["request_id_sha256"]) is not None
                ),
                "session": (
                    payload.get("session_sha256") == self._identity_hash(request["session_id"])
                    if supplied_context else isinstance(payload.get("session_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", payload["session_sha256"]) is not None
                ),
                "session_epoch": (
                    payload.get("session_epoch_sha256") == self._identity_hash(request["session_epoch"])
                    if supplied_context else isinstance(payload.get("session_epoch_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", payload["session_epoch_sha256"]) is not None
                ),
                "agent": (
                    payload.get("agent_sha256") == self._identity_hash(request["agent_id"])
                    if supplied_context else isinstance(payload.get("agent_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", payload["agent_sha256"]) is not None
                ),
                "guard_boot": payload.get("guard_boot_sha256") == self._identity_hash(self.boot_id),
                "rewrite_context": isinstance(payload.get("rewrite_context_nonce_sha256"), str)
                                   and re.fullmatch(r"[0-9a-f]{64}", payload["rewrite_context_nonce_sha256"])
                                   is not None,
                "evidence": isinstance(payload.get("evidence_sha256"), str)
                            and re.fullmatch(r"[0-9a-f]{64}", payload["evidence_sha256"]) is not None,
                "time": type(payload.get("iat")) is int and type(payload.get("exp")) is int
                        and payload["iat"] <= now <= payload["exp"] <= payload["iat"] + 3600,
            }
            return {"valid": all(checks.values()), "checks": checks, "payload": payload}
        except (ValueError, TypeError, KeyError, AttributeError):
            return {"valid": False, "checks": {"rewrite_receipt": False}}

    def _verify_release(self, request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        task_kind = _exact_string(request, "task_kind")
        if task_kind == "rewrite":
            return task_kind, _exact_string(request, "source_text"), self._verify_rewrite(request)
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
        if task_kind == "response" and result.get("valid"):
            payload = result.get("payload")
            checks = result.setdefault("checks", {})
            if not isinstance(payload, dict):
                checks["response_context_current"] = False
            else:
                session_hash = payload.get("response_session_sha256")
                epoch_hash = payload.get("response_session_epoch_sha256")
                with self.delivery_lock:
                    checks["response_session_epoch_current"] = (
                        isinstance(session_hash, str)
                        and isinstance(epoch_hash, str)
                        and self.session_epochs.get(session_hash) == epoch_hash
                    )
                checks["response_guard_boot_current"] = (
                    payload.get("response_guard_boot_sha256")
                    == self._identity_hash(self.boot_id)
                )
                supplied_agent = request.get("agent_id")
                checks["response_agent_current"] = (
                    payload.get("response_agent_sha256")
                    == self._identity_hash(supplied_agent)
                ) if isinstance(supplied_agent, str) and supplied_agent else True
            result["valid"] = all(checks.values())
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
        if task_kind == "rewrite":
            payload.update(
                source_sha256=self._identity_hash(_exact_string(request, "source_text")),
                target_sha256=self._identity_hash(_exact_string(request, "target_text")),
                rewrite_profile_id=request["profile_id"],
                rewrite_profile_sha256=self._rewrite_worker(request).profile_sha256,
                rewrite_request_id_sha256=self._identity_hash(
                    _exact_string(request, "request_id")),
                rewrite_context_nonce_sha256=request["_verified_release_payload"][
                    "rewrite_context_nonce_sha256"],
            )
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
            if task_kind != "rewrite" and current_epoch is None and epoch_hash not in history:
                history.add(epoch_hash)
                self.session_epochs[session_hash] = epoch_hash
                current_epoch = epoch_hash
            if current_epoch != epoch_hash:
                return {
                    "valid": False,
                    "status": "BLOCK",
                    "checks": {"session_epoch_current": False},
                }
            if task_kind == "rewrite":
                payload = request.get("_verified_release_payload")
                if not isinstance(payload, dict):
                    return {"valid": False, "status": "BLOCK",
                            "checks": {"rewrite_context_current": False}}
                context_nonce = payload.get("rewrite_context_nonce_sha256")
                now = int(time.time())
                self.authorized_rewrite_receipts = {
                    nonce: record for nonce, record in self.authorized_rewrite_receipts.items()
                    if record["receipt_expiry"] >= now
                }
                authorization_binding = self._identity_hash(json.dumps({
                    "source_sha256": payload.get("source_sha256"),
                    "target_sha256": payload.get("target_sha256"),
                    "session_sha256": session_hash,
                    "session_epoch_sha256": epoch_hash,
                    "agent_sha256": self._identity_hash(_exact_string(request, "agent_id")),
                    "request_id_sha256": self._identity_hash(_exact_string(request, "request_id")),
                    "language": _exact_string(request, "language"),
                    "profile_id": _exact_string(request, "profile_id"),
                    "content_type": _content_type(request),
                    "short_text_reviewed": request.get("short_text_reviewed") is True,
                    "channel": _exact_string(request, "channel"),
                }, sort_keys=True, separators=(",", ":")))
                previous = self.authorized_rewrite_receipts.get(context_nonce)
                rewrite_checks = {
                    "rewrite_session": payload.get("session_sha256") == session_hash,
                    "rewrite_session_epoch": payload.get("session_epoch_sha256") == epoch_hash,
                    "rewrite_agent": payload.get("agent_sha256") == self._identity_hash(
                        _exact_string(request, "agent_id")),
                    "rewrite_request_id": payload.get("request_id_sha256") == self._identity_hash(
                        _exact_string(request, "request_id")),
                    "rewrite_guard_boot": payload.get("guard_boot_sha256") == self._identity_hash(
                        self.boot_id),
                    "rewrite_receipt_one_grant": isinstance(context_nonce, str)
                                                 and (previous is None
                                                      or previous["binding"] == authorization_binding),
                }
                if not all(rewrite_checks.values()):
                    return {"valid": False, "status": "BLOCK", "checks": rewrite_checks}
                if previous is not None:
                    if previous["grant_expiry"] < now:
                        return {"valid": False, "status": "BLOCK",
                                "checks": {"rewrite_delivery_grant_expired": False}}
                    return {"valid": True, "status": "PASS",
                            "delivery_grant": previous["delivery_grant"],
                            "expires_in": previous["grant_expiry"] - now}
                delivery_grant = self._issue_delivery_grant(request, task_kind)
                self.authorized_rewrite_receipts[context_nonce] = {
                    "receipt_expiry": int(payload["exp"]), "grant_expiry": now + 600,
                    "binding": authorization_binding,
                    "delivery_grant": delivery_grant,
                }
                return {"valid": True, "status": "PASS",
                        "delivery_grant": delivery_grant, "expires_in": 600}
            if task_kind == "response":
                payload = request.get("_verified_release_payload")
                expected = {
                    "response_session_sha256": session_hash,
                    "response_session_epoch_sha256": epoch_hash,
                    "response_agent_sha256": self._identity_hash(
                        _exact_string(request, "agent_id")
                    ),
                    "response_guard_boot_sha256": self._identity_hash(self.boot_id),
                }
                if (not isinstance(payload, dict)
                        or any(payload.get(key) != value
                               for key, value in expected.items())):
                    return {
                        "valid": False,
                        "status": "BLOCK",
                        "checks": {"response_context_current": False},
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
            if payload.get("purpose") == "rewrite":
                worker = self._rewrite_worker(request)
                checks["target"] = payload.get("target_sha256") == self._identity_hash(_exact_string(request, "target_text"))
                checks["rewrite_profile"] = (
                    payload.get("rewrite_profile_id") == request["profile_id"]
                    and payload.get("rewrite_profile_sha256") == worker.profile_sha256
                )
                checks["rewrite_request_id"] = (
                    payload.get("rewrite_request_id_sha256")
                    == self._identity_hash(_exact_string(request, "request_id"))
                )
                checks["rewrite_context"] = (
                    isinstance(payload.get("rewrite_context_nonce_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", payload["rewrite_context_nonce_sha256"])
                    is not None
                )
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
        if operation == "prepare_response_review":
            token = self._issue_response_review_context(request)
            return {"status": "PASS", "review_context_token": token,
                    "expires_in": 180}
        if operation == "prepare_rewrite_context":
            token = self._issue_rewrite_context(request)
            return {"status": "PASS", "rewrite_context_token": token,
                    "expires_in": 180}
        if operation == "rewrite_text":
            result = self._rewrite_text(request)
            AUDIT.append_audit(self.audit_path, _decision_audit(
                {**request, "task_kind": "rewrite",
                 "target_text": result.get("target_text", "")}, result, "rewrite"))
            return result
        if operation == "release":
            result = (
                self._release_reviewed_response(request)
                if request.get("task_kind") == "response" else GATEWAY.gate(request)
            )
            AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "release"))
            return result
        if operation == "verify":
            if request.get("task_kind") == "response":
                result = {
                    "valid": False,
                    "status": "BLOCK",
                    "checks": {"response_delivery_authorization_required": False},
                }
                AUDIT.append_audit(self.audit_path, _decision_audit(request, result, "verify"))
                return result
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
                request["_verified_release_payload"] = result.get("payload")
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


def _response_reviewer_from_factory(reference: str | None) -> Any | None:
    """Load one trusted host-owned reviewer factory for the service runtime."""
    if reference is None:
        return None
    if (not isinstance(reference, str) or reference.count(":") != 1
            or not all(part for part in reference.split(":"))):
        raise ValueError("response review factory must be package.module:callable")
    module_name, attribute = reference.split(":", 1)
    if (not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module_name)
            or not re.fullmatch(r"[A-Za-z_]\w*", attribute)):
        raise ValueError("response review factory must be package.module:callable")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise ValueError("response review factory is not callable")
    reviewer = factory()
    if not callable(getattr(reviewer, "review", None)):
        raise ValueError("response review factory returned an invalid reviewer")
    return reviewer


def _response_reviewer_from_config(path: Path | None) -> Any | None:
    """Build the bundled provider-neutral HTTPS reviewer from protected state."""
    if path is None:
        return None
    reviewer = RESPONSE_REVIEW_RUNTIME.build_response_reviewer_from_config(
        path, response_module=RESPONSE_REVIEW,
    )
    if not callable(getattr(reviewer, "review", None)):
        raise ValueError("response review configuration returned an invalid reviewer")
    return reviewer


def _rewrite_workers_from_factory(reference: str | None) -> dict[str, Any]:
    """Only a trusted operator may install executable model/host configuration."""
    if reference is None:
        return {}
    if (not isinstance(reference, str)
            or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", reference) is None):
        raise ValueError("rewrite factory must be package.module:callable")
    module_name, attribute = reference.split(":")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise ValueError("rewrite factory is not callable")
    workers = factory()
    if not isinstance(workers, dict) or not workers:
        raise ValueError("rewrite factory must return a nonempty profile registry")
    return workers


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
    parser.add_argument("--rewrite-worker-factory",
                        help="Trusted package.module:callable returning profile-ID to NativeRewriteWorker mapping")
    review_source = parser.add_mutually_exclusive_group()
    review_source.add_argument(
        "--response-review-factory",
        help=("Trusted host-owned package.module:callable returning a configured "
              "provider-neutral response reviewer"),
    )
    review_source.add_argument(
        "--response-review-config",
        type=Path,
        help=("Protected configuration for the bundled provider-neutral HTTPS "
              "response reviewer"),
    )
    args = parser.parse_args()
    try:
        reviewer = (
            _response_reviewer_from_config(args.response_review_config)
            if args.response_review_config is not None
            else _response_reviewer_from_factory(args.response_review_factory)
        )
        service = GuardService(
            args.key_file, args.audit_file, _token_from_file(args.token_file), reviewer,
            rewrite_workers=_rewrite_workers_from_factory(args.rewrite_worker_factory),
        )
        server = build_server(args.endpoint, service)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
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
