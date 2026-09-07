#!/usr/bin/env python3
"""WSGI adapter for authenticated, idempotent CMS localization webhooks.

This transport accepts content-change events only. It never runs a model,
approves a translation, prepares a publication, or returns customer text.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any, Callable

from website_localization_cms import (
    MAX_MESSAGE_BYTES,
    CMSBridgeBlocked,
    CMSMessageSignature,
    WebsiteLocalizationCMSBridge,
)


API_SCHEMA = "blun.website-localization-api.v1"
CHANGE_PATH = "/v1/localization/changes"


class WebsiteLocalizationAPI:
    """Dependency-injected WSGI app; the host retains verifier and database ownership."""

    def __init__(
        self,
        bridge: WebsiteLocalizationCMSBridge,
        event_verifier: Any,
        *,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 3,
        require_https: bool = True,
    ):
        if not isinstance(bridge, WebsiteLocalizationCMSBridge):
            raise TypeError("bridge must be WebsiteLocalizationCMSBridge")
        if not callable(getattr(event_verifier, "verify", None)):
            raise TypeError("event_verifier must provide verify")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts is outside the supported range")
        if not isinstance(require_https, bool):
            raise TypeError("require_https must be boolean")
        self.bridge = bridge
        self.event_verifier = event_verifier
        self.clock = clock
        self.max_attempts = max_attempts
        self.require_https = require_https

    @staticmethod
    def _json(status: str, payload: dict[str, Any], start_response):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        start_response(status, [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ])
        return [body]

    @classmethod
    def _blocked(cls, status: str, code: str, start_response):
        return cls._json(status, {"schema": API_SCHEMA, "status": "BLOCK", "error": code}, start_response)

    @staticmethod
    def _content_type(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        parts = [part.strip().lower() for part in value.split(";")]
        return parts[0] == "application/json" and all(part == "charset=utf-8" for part in parts[1:])

    @staticmethod
    def _pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    @staticmethod
    def _constant(value):
        raise ValueError("nonfinite number")

    def __call__(self, environ, start_response):
        if not isinstance(environ, dict):
            return self._blocked("500 Internal Server Error", "api.environment.invalid", start_response)
        if environ.get("PATH_INFO") != CHANGE_PATH:
            return self._blocked("404 Not Found", "api.path.not_found", start_response)
        if environ.get("REQUEST_METHOD") != "POST":
            return self._blocked("405 Method Not Allowed", "api.method.not_allowed", start_response)
        if self.require_https and environ.get("wsgi.url_scheme") != "https":
            return self._blocked("400 Bad Request", "api.https.required", start_response)
        if environ.get("HTTP_TRANSFER_ENCODING"):
            return self._blocked("400 Bad Request", "api.transfer_encoding.rejected", start_response)
        if not self._content_type(environ.get("CONTENT_TYPE")):
            return self._blocked("415 Unsupported Media Type", "api.content_type.invalid", start_response)
        length = environ.get("CONTENT_LENGTH")
        if not isinstance(length, str) or not length.isascii() or not length.isdecimal():
            return self._blocked("411 Length Required", "api.content_length.required", start_response)
        size = int(length)
        if size <= 0:
            return self._blocked("400 Bad Request", "api.body.invalid", start_response)
        if size > MAX_MESSAGE_BYTES:
            return self._blocked("413 Content Too Large", "api.body.too_large", start_response)
        stream = environ.get("wsgi.input")
        try:
            # WSGI applications must not read beyond CONTENT_LENGTH. The HTTP
            # server owns message framing and must reject request smuggling.
            raw = stream.read(size)
        except Exception:
            raw = None
        if not isinstance(raw, bytes) or len(raw) != size:
            return self._blocked("400 Bad Request", "api.body.invalid", start_response)
        try:
            text = raw.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            event = json.loads(text, object_pairs_hook=self._pairs, parse_constant=self._constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return self._blocked("400 Bad Request", "api.json.invalid", start_response)
        signature = CMSMessageSignature(
            environ.get("HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM"),
            environ.get("HTTP_X_LOCALIZATION_KEY_ID"),
            environ.get("HTTP_X_LOCALIZATION_SIGNATURE"),
        )
        try:
            ingested = self.bridge.ingest_change(
                event, signature, self.event_verifier,
                max_attempts=self.max_attempts, now=self.clock(),
            )
        except CMSBridgeBlocked as error:
            if error.code in {"cms.signature.invalid", "cms.event.signature_rejected"}:
                status = "401 Unauthorized"
            elif error.code == "cms.event.idempotency_collision":
                status = "409 Conflict"
            elif error.code in {"cms.queue.rejected", "cms.transaction.external", "cms.schema.altered"}:
                status = "503 Service Unavailable"
            else:
                status = "400 Bad Request"
            return self._blocked(status, error.code, start_response)
        except Exception:
            return self._blocked("500 Internal Server Error", "api.internal", start_response)
        payload = {"schema": API_SCHEMA, **asdict(ingested)}
        return self._json("202 Accepted" if ingested.inserted_jobs else "200 OK", payload, start_response)
