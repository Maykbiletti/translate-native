#!/usr/bin/env python3
"""Portable fail-closed hook for exact translation and rewrite delivery."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATH = ROOT / "translate-native" / "scripts" / "language_quality.py"
CLIENT_PATH = ROOT / "translate-native" / "scripts" / "guard_service_client.py"
SPEC = importlib.util.spec_from_file_location("blun_hook_quality", QUALITY_PATH)
assert SPEC and SPEC.loader
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)

CLIENT_SPEC = importlib.util.spec_from_file_location("blun_hook_service_client", CLIENT_PATH)
assert CLIENT_SPEC and CLIENT_SPEC.loader
SERVICE_CLIENT = importlib.util.module_from_spec(CLIENT_SPEC)
CLIENT_SPEC.loader.exec_module(SERVICE_CLIENT)


def _service_token() -> str:
    direct = os.environ.get("BLUN_LANGUAGE_GUARD_SERVICE_TOKEN", "").strip()
    if direct:
        return direct
    token_file = os.environ.get("BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE", "").strip()
    return SERVICE_CLIENT.load_service_token(Path(token_file)) if token_file else ""


def verify_rewrite(request: dict) -> dict:
    endpoint = os.environ.get("BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT", "").strip()
    if not endpoint:
        raise ValueError("rewrite receipts require the isolated guard service")
    required = (
        "source_text", "target_text", "language", "release_token", "profile_id",
        "request_id", "session_id", "session_epoch", "agent_id", "channel",
    )
    if any(not isinstance(request.get(name), str) or not request[name] for name in required):
        raise ValueError("rewrite delivery context is incomplete")
    common = {
        "task_kind": "rewrite",
        "source_text": request["source_text"],
        "target_text": request["target_text"],
        "language": request["language"],
        "profile_id": request["profile_id"],
        "request_id": request["request_id"],
        "content_type": request.get("content_type", "prose"),
        "short_text_reviewed": request.get("short_text_reviewed") is True,
        "session_id": request["session_id"],
        "session_epoch": request["session_epoch"],
        "agent_id": request["agent_id"],
        "channel": request["channel"],
    }
    token = _service_token()
    authorized = SERVICE_CLIENT.call_guard_service(
        endpoint,
        {**common, "operation": "authorize_delivery",
         "release_token": request["release_token"]},
        auth_token=token,
        timeout=10.0,
    )
    grant = authorized.get("delivery_grant")
    if authorized.get("valid") is not True or not isinstance(grant, str) or not grant:
        raise ValueError("isolated guard rejected rewrite delivery authorization")
    consume = {key: value for key, value in common.items() if key != "source_text"}
    consumed = SERVICE_CLIENT.call_guard_service(
        endpoint,
        {**consume, "operation": "consume_delivery",
         "source_sha256": hashlib.sha256(request["source_text"].encode("utf-8")).hexdigest(),
         "delivery_grant": grant},
        auth_token=token,
        timeout=10.0,
    )
    if consumed.get("valid") is not True:
        raise ValueError("isolated guard rejected rewrite delivery grant consumption")
    return {"valid": True, "status": "PASS",
            "checks": {"authorization": True, "one_time_consumption": True}}


def load_verification_key(path: Path) -> bytes:
    """Load an existing verifier key without creating or replacing trust state."""
    environment_key = os.environ.get("BLUN_LANGUAGE_GUARD_KEY")
    if environment_key:
        return hashlib.sha256(environment_key.encode("utf-8")).digest()
    try:
        return QUALITY.load_existing_key(path)
    except FileNotFoundError as error:
        raise ValueError("signing key is missing; verification fails closed") from error
    except OSError as error:
        raise ValueError("signing key cannot be read") from error
    except ValueError as error:
        if "permissions" in str(error):
            raise ValueError("signing key permissions are broader than owner-only") from error
        raise ValueError("signing key is invalid") from error


def main() -> int:
    try:
        request = json.loads(sys.stdin.read().lstrip("\ufeff"))
        task_kind = request["task_kind"]
        source = request.get("source_text", "")
        if task_kind not in {"translation", "response", "rewrite"}:
            raise ValueError("task_kind must be translation, response, or rewrite")
        if task_kind == "translation" and not source.strip():
            raise ValueError("translation receipts require source_text")
        if task_kind == "response" and source.strip():
            raise ValueError("response receipts cannot carry source_text")
        if task_kind == "rewrite":
            result = verify_rewrite(request)
            print(json.dumps({"allow": True, "verification": result}))
            return 0
        key_path = Path(os.environ.get("BLUN_LANGUAGE_GUARD_KEY_FILE", Path.home() / ".config" / "blun-language-guard" / "signing.key"))
        key = load_verification_key(key_path)
        if task_kind == "response":
            raise ValueError(
                "response receipts require isolated current-session verification"
            )
        result = QUALITY.verify_receipt(
            request["release_token"], source, request["target_text"],
            request["language"], key,
            request.get("content_type", "prose"), request.get("short_text_reviewed") is True,
            purpose=task_kind,
        )
    except (KeyError, ValueError, json.JSONDecodeError, OSError,
            SERVICE_CLIENT.GuardServiceError) as error:
        print(json.dumps({"allow": False, "error": str(error)}))
        return 1
    print(json.dumps({"allow": result["valid"], "verification": result}))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
