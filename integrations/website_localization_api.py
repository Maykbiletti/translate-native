#!/usr/bin/env python3
"""Authenticated WSGI ingress for the provider-neutral localization runtime.

The API accepts signed CMS change and cancellation events and exposes
content-free capabilities, per-locale progress, and verified lifecycle state.
It never runs a model, approves text, or returns source/target prose.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping


API_SCHEMA = "blun.website-localization-api.v2"
CHANGE_PATH = "/v2/localization/changes"
CANCELLATION_PATH = "/v2/localization/cancellations"
TOMBSTONE_PATH = "/v2/localization/tombstones"
STATUS_PATH = "/v2/localization/status"
LIFECYCLE_PATH = "/v2/localization/lifecycle"
CAPABILITIES_PATH = "/v2/localization/capabilities"
STATUS_REQUEST_SCHEMA = "blun.cms-localization-status-request.v2"
LIFECYCLE_REQUEST_SCHEMA = "blun.cms-localization-lifecycle-request.v1"
LIFECYCLE_RESPONSE_SCHEMA = "blun.cms-localization-lifecycle.v2"
CAPABILITIES_REQUEST_SCHEMA = "blun.cms-localization-capabilities-request.v1"
MAX_MESSAGE_BYTES = 4_000_000
MAX_STATUS_CLOCK_SKEW = 300.0
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SIGNATURE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+=-]{1,4096}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class CMSMessageSignature:
    """Cross-module value normalized by the CMS bridge before persistence."""

    algorithm: Any
    key_id: Any
    signature: Any


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("invalid json") from None
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("json too large")
    return encoded


def _token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or TOKEN.fullmatch(value) is None
        or "\x00" in value
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise ValueError("invalid token")
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid timestamp")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid timestamp")
    return number


def _bridge_code(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    if type(error).__name__ not in {"CMSBridgeBlocked", "_APIRequestBlocked"}:
        return None
    if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
        return None
    return code


class WebsiteLocalizationAPI:
    """Strict WSGI adapter over one already-composed CMS bridge."""

    def __init__(
        self,
        bridge: Any,
        event_verifier: Any,
        *,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 3,
        require_https: bool = True,
        approval_authority: Any | None = None,
        publication_authority: Any | None = None,
    ):
        if not all(callable(getattr(bridge, name, None)) for name in (
            "ingest_change", "cancel_change", "change_progress",
            "localization_capabilities", "request_tombstone",
        )):
            raise TypeError("bridge must provide CMS ingress, capabilities, and progress")
        if not callable(getattr(event_verifier, "verify", None)):
            raise TypeError("event_verifier must provide verify")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 20
        ):
            raise ValueError("max_attempts is outside the supported range")
        if not isinstance(require_https, bool):
            raise TypeError("require_https must be boolean")
        if (approval_authority is None) != (publication_authority is None):
            raise TypeError("lifecycle authorities must be configured together")
        if approval_authority is not None and not callable(
            getattr(approval_authority, "verify", None)
        ):
            raise TypeError("approval_authority must provide verify")
        if publication_authority is not None and not callable(
            getattr(publication_authority, "verify", None)
        ):
            raise TypeError("publication_authority must provide verify")
        if approval_authority is not None and not callable(
            getattr(bridge, "change_lifecycle", None)
        ):
            raise TypeError("bridge must provide lifecycle status")
        self.bridge = bridge
        self.event_verifier = event_verifier
        self.clock = clock
        self.max_attempts = max_attempts
        self.require_https = require_https
        self.approval_authority = approval_authority
        self.publication_authority = publication_authority

    @staticmethod
    def _json(status: str, payload: Mapping[str, Any], start_response):
        body = _canonical_json(dict(payload)).encode("utf-8")
        start_response(status, [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return [body]

    @classmethod
    def _blocked(cls, status: str, code: str, start_response):
        return cls._json(status, {
            "schema": API_SCHEMA,
            "status": "BLOCK",
            "error": code,
        }, start_response)

    @staticmethod
    def _content_type(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        parts = [part.strip().lower() for part in value.split(";")]
        return parts in (["application/json"], ["application/json", "charset=utf-8"])

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

    @staticmethod
    def _status_for(code: str) -> str:
        if code in {
            "cms.signature.invalid", "cms.event.signature_rejected",
            "cms.cancellation.signature_rejected",
            "cms.cancellation.scope_rejected",
            "cms.tombstone.signature_rejected",
            "cms.tombstone.scope_rejected",
            "cms.status.signature_rejected", "cms.status.request_expired",
            "cms.status.scope_rejected", "cms.lifecycle.signature_rejected",
            "cms.lifecycle.request_expired", "cms.lifecycle.scope_rejected",
            "cms.capabilities.signature_rejected",
            "cms.capabilities.request_expired",
        }:
            return "401 Unauthorized"
        if code in {
            "cms.event.idempotency_collision", "cms.event.sequence_collision",
            "cms.event.superseded", "cms.event.legacy_replay_only",
            "cms.event.cancelled", "cms.cancellation.idempotency_collision",
            "cms.cancellation.already_published",
            "cms.cancellation.delivery_in_flight",
            "cms.tombstone.idempotency_collision",
            "cms.tombstone.not_published",
        }:
            return "409 Conflict"
        if code == "cms.event.not_enqueued":
            return "404 Not Found"
        if code in {
            "cms.queue.rejected", "cms.transaction.external", "cms.schema.altered",
            "cms.queue.integrity_failed", "cms.queue.identity_lost",
            "cms.event.tampered", "cms.event.topic_invalid",
            "cms.lifecycle.unavailable", "cms.release.integrity_failed",
            "cms.delivery.tampered", "cms.delivery.signature_invalid",
            "cms.capabilities.registry_invalid",
            "cms.tombstone.tampered", "cms.tombstone.publication_invalid",
            "cms.tombstone.delivery_signature_invalid",
        }:
            return "503 Service Unavailable"
        return "400 Bad Request"

    def __call__(self, environ, start_response):
        if not isinstance(environ, dict):
            return self._blocked(
                "500 Internal Server Error", "api.environment.invalid", start_response,
            )
        path = environ.get("PATH_INFO")
        if path not in {
            CHANGE_PATH, CANCELLATION_PATH, TOMBSTONE_PATH,
            STATUS_PATH, LIFECYCLE_PATH,
            CAPABILITIES_PATH,
        }:
            return self._blocked("404 Not Found", "api.path.not_found", start_response)
        if environ.get("REQUEST_METHOD") != "POST":
            return self._blocked(
                "405 Method Not Allowed", "api.method.not_allowed", start_response,
            )
        if self.require_https and environ.get("wsgi.url_scheme") != "https":
            return self._blocked("400 Bad Request", "api.https.required", start_response)
        if environ.get("QUERY_STRING") not in {None, ""}:
            return self._blocked("400 Bad Request", "api.query.rejected", start_response)
        if environ.get("HTTP_TRANSFER_ENCODING"):
            return self._blocked(
                "400 Bad Request", "api.transfer_encoding.rejected", start_response,
            )
        if not self._content_type(environ.get("CONTENT_TYPE")):
            return self._blocked(
                "415 Unsupported Media Type", "api.content_type.invalid", start_response,
            )
        length = environ.get("CONTENT_LENGTH")
        if not isinstance(length, str) or not length.isascii() or not length.isdecimal():
            return self._blocked(
                "411 Length Required", "api.content_length.required", start_response,
            )
        size = int(length)
        if size <= 0:
            return self._blocked("400 Bad Request", "api.body.invalid", start_response)
        if size > MAX_MESSAGE_BYTES:
            return self._blocked("413 Content Too Large", "api.body.too_large", start_response)
        stream = environ.get("wsgi.input")
        try:
            raw = stream.read(size)
        except Exception:
            raw = None
        if not isinstance(raw, bytes) or len(raw) != size:
            return self._blocked("400 Bad Request", "api.body.invalid", start_response)
        try:
            text = raw.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            request = json.loads(
                text,
                object_pairs_hook=self._pairs,
                parse_constant=self._constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return self._blocked("400 Bad Request", "api.json.invalid", start_response)

        signature = CMSMessageSignature(
            environ.get("HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM"),
            environ.get("HTTP_X_LOCALIZATION_KEY_ID"),
            environ.get("HTTP_X_LOCALIZATION_SIGNATURE"),
        )
        try:
            now = _timestamp(self.clock())
            if path == STATUS_PATH:
                return self._status(request, signature, now, start_response)
            if path == LIFECYCLE_PATH:
                return self._lifecycle(request, signature, now, start_response)
            if path == CAPABILITIES_PATH:
                return self._capabilities(request, signature, now, start_response)
            if path == CANCELLATION_PATH:
                cancelled = self.bridge.cancel_change(
                    request,
                    signature,
                    self.event_verifier,
                    now=now,
                )
                return self._json(
                    "202 Accepted" if cancelled.newly_cancelled else "200 OK",
                    {"schema": API_SCHEMA, **asdict(cancelled)},
                    start_response,
                )
            if path == TOMBSTONE_PATH:
                if self.publication_authority is None:
                    raise _APIRequestBlocked("cms.lifecycle.unavailable")
                accepted = self.bridge.request_tombstone(
                    request,
                    signature,
                    self.event_verifier,
                    self.publication_authority,
                    now=now,
                    max_attempts=self.max_attempts,
                )
                return self._json(
                    "202 Accepted" if accepted.newly_requested else "200 OK",
                    {"schema": API_SCHEMA, **asdict(accepted)},
                    start_response,
                )
            ingested = self.bridge.ingest_change(
                request,
                signature,
                self.event_verifier,
                max_attempts=self.max_attempts,
                now=now,
            )
        except Exception as error:
            code = _bridge_code(error)
            if code is None:
                return self._blocked(
                    "500 Internal Server Error", "api.internal", start_response,
                )
            return self._blocked(self._status_for(code), code, start_response)
        response_status = (
            "202 Accepted"
            if ingested.inserted_jobs and ingested.status == "enqueued"
            else "200 OK"
        )
        return self._json(response_status, {
            "schema": API_SCHEMA,
            **asdict(ingested),
        }, start_response)

    def _status(self, request, signature, now, start_response):
        expected = {"schema", "request_id", "event_id", "site_id", "requested_at"}
        if (
            not isinstance(request, dict)
            or set(request) != expected
            or request.get("schema") != STATUS_REQUEST_SCHEMA
        ):
            raise _APIRequestBlocked("cms.status.request_invalid")
        try:
            request_id = _token(request.get("request_id"))
            event_id = _token(request.get("event_id"))
            site_id = _token(request.get("site_id"))
            requested_at = _timestamp(request.get("requested_at"))
        except ValueError:
            raise _APIRequestBlocked("cms.status.request_invalid") from None
        try:
            _token(signature.algorithm)
            key_id = _token(signature.key_id)
            if (
                not isinstance(signature.signature, str)
                or SIGNATURE_VALUE.fullmatch(signature.signature) is None
            ):
                raise ValueError("invalid signature")
        except ValueError:
            raise _APIRequestBlocked("cms.status.signature_rejected") from None
        try:
            accepted = self.event_verifier.verify(
                _canonical_json(request).encode("utf-8"), signature,
            ) is True
        except Exception:
            accepted = False
        if not accepted:
            raise _APIRequestBlocked("cms.status.signature_rejected")
        if abs(now - requested_at) > MAX_STATUS_CLOCK_SKEW:
            raise _APIRequestBlocked("cms.status.request_expired")
        progress = self.bridge.change_progress(
            event_id,
            self.event_verifier,
            site_id=site_id,
            requester_key_id=key_id,
            now=now,
        )
        return self._json("200 OK", {
            "schema": API_SCHEMA,
            "status": "PROGRESS",
            "request_id": request_id,
            **asdict(progress),
        }, start_response)

    def _lifecycle(self, request, signature, now, start_response):
        expected = {"schema", "request_id", "event_id", "site_id", "requested_at"}
        if (
            not isinstance(request, dict)
            or set(request) != expected
            or request.get("schema") != LIFECYCLE_REQUEST_SCHEMA
        ):
            raise _APIRequestBlocked("cms.lifecycle.request_invalid")
        try:
            request_id = _token(request.get("request_id"))
            event_id = _token(request.get("event_id"))
            site_id = _token(request.get("site_id"))
            requested_at = _timestamp(request.get("requested_at"))
        except ValueError:
            raise _APIRequestBlocked("cms.lifecycle.request_invalid") from None
        try:
            _token(signature.algorithm)
            key_id = _token(signature.key_id)
            if (
                not isinstance(signature.signature, str)
                or SIGNATURE_VALUE.fullmatch(signature.signature) is None
            ):
                raise ValueError("invalid signature")
        except ValueError:
            raise _APIRequestBlocked("cms.lifecycle.signature_rejected") from None
        try:
            accepted = self.event_verifier.verify(
                _canonical_json(request).encode("utf-8"), signature,
            ) is True
        except Exception:
            accepted = False
        if not accepted:
            raise _APIRequestBlocked("cms.lifecycle.signature_rejected")
        if abs(now - requested_at) > MAX_STATUS_CLOCK_SKEW:
            raise _APIRequestBlocked("cms.lifecycle.request_expired")
        if self.approval_authority is None or self.publication_authority is None:
            raise _APIRequestBlocked("cms.lifecycle.unavailable")
        try:
            lifecycle = self.bridge.change_lifecycle(
                event_id,
                self.event_verifier,
                self.approval_authority,
                self.publication_authority,
                site_id=site_id,
                requester_key_id=key_id,
                now=now,
            )
        except Exception as error:
            if _bridge_code(error) == "cms.status.scope_rejected":
                raise _APIRequestBlocked("cms.lifecycle.scope_rejected") from None
            raise
        return self._json("200 OK", {
            "schema": LIFECYCLE_RESPONSE_SCHEMA,
            "request_id": request_id,
            **asdict(lifecycle),
        }, start_response)

    def _capabilities(self, request, signature, now, start_response):
        expected = {"schema", "request_id", "requested_at"}
        if (
            not isinstance(request, dict)
            or set(request) != expected
            or request.get("schema") != CAPABILITIES_REQUEST_SCHEMA
        ):
            raise _APIRequestBlocked("cms.capabilities.request_invalid")
        try:
            request_id = _token(request.get("request_id"))
            requested_at = _timestamp(request.get("requested_at"))
        except ValueError:
            raise _APIRequestBlocked("cms.capabilities.request_invalid") from None
        try:
            _token(signature.algorithm)
            _token(signature.key_id)
            if (
                not isinstance(signature.signature, str)
                or SIGNATURE_VALUE.fullmatch(signature.signature) is None
            ):
                raise ValueError("invalid signature")
        except ValueError:
            raise _APIRequestBlocked("cms.capabilities.signature_rejected") from None
        try:
            accepted = self.event_verifier.verify(
                _canonical_json(request).encode("utf-8"), signature,
            ) is True
        except Exception:
            accepted = False
        if not accepted:
            raise _APIRequestBlocked("cms.capabilities.signature_rejected")
        if abs(now - requested_at) > MAX_STATUS_CLOCK_SKEW:
            raise _APIRequestBlocked("cms.capabilities.request_expired")
        capabilities = self.bridge.localization_capabilities()
        if not isinstance(capabilities, dict):
            raise _APIRequestBlocked("cms.capabilities.registry_invalid")
        return self._json("200 OK", {
            "schema": API_SCHEMA,
            "status": "CAPABILITIES",
            "request_id": request_id,
            "capabilities": capabilities,
        }, start_response)


class _APIRequestBlocked(RuntimeError):
    """Internal stable request failure normalized like CMS bridge failures."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
