"""Trusted-host rewrite API adapter. Models never receive this client's credentials.

The operator owns profile selection and delivery context. Results remain internal
until deliver() authorizes and consumes a fresh one-time grant for exact bytes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable


class RewriteDeliveryBlocked(RuntimeError):
    pass


class NativeRewriteClient:
    def __init__(self, call_service: Callable[[dict], dict]):
        if not callable(call_service):
            raise TypeError("trusted service transport is required")
        self._call = call_service

    def rewrite(self, *, source_text: str, language: str, profile_id: str,
                request_id: str, content_type: str = "prose") -> dict:
        request = {"operation": "rewrite_text", "source_text": source_text,
                   "language": language, "profile_id": profile_id,
                   "request_id": request_id, "content_type": content_type}
        try:
            result = self._call(request)
            if (not isinstance(result, dict) or result.get("release_allowed") is not True
                    or result.get("task_kind") != "rewrite"
                    or not isinstance(result.get("target_text"), str)
                    or not isinstance(result.get("release_token"), str)):
                raise RewriteDeliveryBlocked("rewrite_not_released")
            return result
        except Exception:
            raise RewriteDeliveryBlocked("rewrite_unavailable_or_blocked") from None

    def deliver(self, result: dict, *, source_text: str, language: str,
                profile_id: str, content_type: str, session_id: str,
                session_epoch: str, agent_id: str, channel: str,
                send: Callable[[str], None]) -> None:
        if not callable(send):
            raise RewriteDeliveryBlocked("delivery_transport_missing")
        try:
            # Freeze all bytes before authorization; never trim or normalize.
            frozen = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
            target = frozen["target_text"]
            common = {"task_kind": "rewrite", "source_text": source_text,
                      "target_text": target, "language": language,
                      "profile_id": profile_id, "content_type": content_type,
                      "session_id": session_id, "session_epoch": session_epoch,
                      "agent_id": agent_id, "channel": channel}
            authorized = self._call({**common, "operation": "authorize_delivery",
                                     "release_token": frozen["release_token"]})
            if authorized.get("valid") is not True:
                raise RewriteDeliveryBlocked("authorization_failed")
            consume = {key: value for key, value in common.items() if key != "source_text"}
            consumed = self._call({**consume, "operation": "consume_delivery",
                                  "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                                  "delivery_grant": authorized["delivery_grant"]})
            if consumed.get("valid") is not True:
                raise RewriteDeliveryBlocked("consumption_failed")
        except Exception:
            raise RewriteDeliveryBlocked("delivery_blocked") from None
        # No retry after a possibly completed send; the caller reconciles transport state.
        send(target)
