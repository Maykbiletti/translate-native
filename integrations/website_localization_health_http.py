#!/usr/bin/env python3
"""Authenticated, content-free HTTP reader for localization service health.

The host supplies authentication and the already-composed read-only health
callback. This adapter performs no repair, retry, signing, model, or CMS work.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping


HEALTH_PATH = "/v2/localization/health"
AUTH_REQUEST_SCHEMA = "blun.localization-health-http-auth-request.v1"
PRINCIPAL_SCHEMA = "blun.localization-health-reader-principal.v1"
RESPONSE_SCHEMA = "blun.website-localization-health-http.v1"
ERROR_SCHEMA = "blun.website-localization-health-http-error.v1"
HEALTH_SCHEMA = "blun.website-localization-health.v1"
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
MAX_RESPONSE_BYTES = 4_000_000
HEALTH_STATUSES = {"healthy", "degraded", "blocked"}
PROVIDER_STATUSES = {"healthy", "blocked"}
WEBSITE_STATUSES = {
    "processing", "localization_failed", "awaiting_approval", "ready",
    "publishing", "publication_failed", "published",
}


class HealthHTTPFailed(RuntimeError):
    """Stable content-free failure returned by the HTTP boundary."""

    def __init__(self, code: str, *, status: int, retryable: bool = False):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("health HTTP error code is invalid")
        if status not in {400, 401, 403, 404, 503}:
            raise ValueError("health HTTP status is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("health HTTP retryability is invalid")
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class HealthReaderPrincipal:
    reader_id: str
    credential_id: str
    credential_version: str
    scope: str


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise HealthHTTPFailed(
            "health.http.response_invalid", status=503, retryable=True,
        ) from None
    if not encoded or len(encoded) > MAX_RESPONSE_BYTES:
        raise HealthHTTPFailed(
            "health.http.response_invalid", status=503, retryable=True,
        )
    return encoded


def _token(value: Any) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise ValueError
    return value


def _reason(value: Any) -> str:
    if not isinstance(value, str) or ERROR_CODE.fullmatch(value) is None:
        raise ValueError
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError
    return result


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _exact_mapping(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError
    return value


def _counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError
    return {_token(key): _count(item) for key, item in value.items()}


def _reasons(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError
    result = [_reason(item) for item in value]
    if result != sorted(set(result)):
        raise ValueError
    return result


def _principal(value: Any) -> HealthReaderPrincipal:
    try:
        value = _exact_mapping(value, {
            "schema", "reader_id", "credential_id", "credential_version",
            "scope",
        })
        if value["schema"] != PRINCIPAL_SCHEMA or value["scope"] != "service-health":
            raise ValueError
        return HealthReaderPrincipal(
            _token(value["reader_id"]),
            _token(value["credential_id"]),
            _token(value["credential_version"]),
            value["scope"],
        )
    except (KeyError, ValueError, TypeError):
        raise HealthHTTPFailed(
            "health.http.authentication_failed", status=401,
        ) from None


def _report_payload(report: Any, *, expected_checked_at: float) -> dict[str, Any]:
    try:
        payload = report.as_payload()
        payload = _exact_mapping(payload, {
            "schema", "checked_at", "status", "components", "providers",
            "website_versions",
        })
        if payload["schema"] != HEALTH_SCHEMA or payload["status"] not in HEALTH_STATUSES:
            raise ValueError
        if _timestamp(payload["checked_at"]) != expected_checked_at:
            raise ValueError

        components = payload["components"]
        if not isinstance(components, list) or not components or len(components) > 16:
            raise ValueError
        component_names = []
        for component in components:
            component = _exact_mapping(
                component, {"component", "status", "reasons", "counts"},
            )
            component_names.append(_token(component["component"]))
            if component["status"] not in HEALTH_STATUSES:
                raise ValueError
            _reasons(component["reasons"])
            _counts(component["counts"])
        if len(component_names) != len(set(component_names)):
            raise ValueError
        expected_status = (
            "blocked" if any(
                item["status"] == "blocked" for item in components
            ) else "degraded" if any(
                item["status"] == "degraded" for item in components
            ) else "healthy"
        )
        if payload["status"] != expected_status:
            raise ValueError

        providers = payload["providers"]
        if not isinstance(providers, list) or len(providers) > 1024:
            raise ValueError
        for provider in providers:
            provider = _exact_mapping(provider, {
                "provider_id", "model_id", "model_version", "status", "reason",
            })
            _token(provider["provider_id"])
            _token(provider["model_id"])
            _token(provider["model_version"])
            if provider["status"] not in PROVIDER_STATUSES:
                raise ValueError
            if provider["reason"] is not None:
                _reason(provider["reason"])

        versions = payload["website_versions"]
        if not isinstance(versions, list) or len(versions) > 100_000:
            raise ValueError
        version_ids = []
        for version in versions:
            version = _exact_mapping(version, {
                "event_id", "site_id", "website_version", "plan_id", "status",
                "required_locales", "approved_locales", "queue_counts",
                "blocked_locales",
            })
            version_ids.append(_token(version["event_id"]))
            for name in ("site_id", "website_version", "plan_id"):
                _token(version[name])
            if version["status"] not in WEBSITE_STATUSES:
                raise ValueError
            required = _count(version["required_locales"])
            approved = _count(version["approved_locales"])
            if approved > required:
                raise ValueError
            _counts(version["queue_counts"])
            blocked = version["blocked_locales"]
            if not isinstance(blocked, list) or len(blocked) > required:
                raise ValueError
            observed = []
            for item in blocked:
                if not isinstance(item, list) or len(item) != 2:
                    raise ValueError
                observed.append((_token(item[0]), _reason(item[1])))
            if observed != sorted(set(observed)):
                raise ValueError
        if len(version_ids) != len(set(version_ids)):
            raise ValueError

        _canonical_json(payload)
        return payload
    except HealthHTTPFailed:
        raise
    except Exception:
        raise HealthHTTPFailed(
            "health.http.response_invalid", status=503, retryable=True,
        ) from None


class WebsiteLocalizationHealthHTTPApplication:
    """Strict authenticated WSGI reader for one composed health monitor."""

    def __init__(
        self,
        health_provider: Callable[..., Any],
        authenticator: Callable[[dict[str, Any]], Any],
        *,
        clock: Callable[[], float],
    ):
        if not callable(health_provider):
            raise ValueError("health provider must be callable")
        if not callable(authenticator):
            raise ValueError("health authenticator must be callable")
        if not callable(clock):
            raise ValueError("health clock must be callable")
        self.health_provider = health_provider
        self.authenticator = authenticator
        self.clock = clock

    @staticmethod
    def _headers(environ: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        headers = []
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").lower()
            elif key in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                name = key.replace("_", "-").lower()
            else:
                continue
            if (
                HEADER_NAME.fullmatch(name) is None
                or not isinstance(value, str)
                or len(value) > MAX_HEADER_VALUE
                or "\r" in value
                or "\n" in value
            ):
                raise HealthHTTPFailed("health.http.headers_invalid", status=400)
            headers.append((name, value))
        headers.sort()
        if len(headers) > MAX_HEADERS or len({name for name, _ in headers}) != len(headers):
            raise HealthHTTPFailed("health.http.headers_invalid", status=400)
        return tuple(headers)

    @staticmethod
    def _empty_body(environ: Mapping[str, Any]) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
            raise HealthHTTPFailed("health.http.transfer_encoding", status=400)
        if environ.get("CONTENT_LENGTH") not in {None, "", "0"}:
            raise HealthHTTPFailed("health.http.body_not_allowed", status=400)
        return b""

    def _authenticate(self, headers: tuple[tuple[str, str], ...], body: bytes) -> None:
        request = {
            "schema": AUTH_REQUEST_SCHEMA,
            "method": "GET",
            "path": HEALTH_PATH,
            "headers": [list(item) for item in headers],
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        try:
            _principal(self.authenticator(request))
        except HealthHTTPFailed:
            raise
        except Exception:
            raise HealthHTTPFailed(
                "health.http.authentication_unavailable",
                status=503,
                retryable=True,
            ) from None

    @staticmethod
    def _send(start_response, status: int, payload: Mapping[str, Any]):
        body = _canonical_json(dict(payload))
        phrases = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return [body]

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            if not isinstance(environ, Mapping):
                raise HealthHTTPFailed("health.http.environment_invalid", status=400)
            if environ.get("wsgi.url_scheme") != "https":
                raise HealthHTTPFailed("health.http.https_required", status=400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise HealthHTTPFailed("health.http.query_invalid", status=400)
            if environ.get("REQUEST_METHOD") != "GET" or environ.get("PATH_INFO") != HEALTH_PATH:
                raise HealthHTTPFailed("health.http.route_not_found", status=404)
            body = self._empty_body(environ)
            self._authenticate(self._headers(environ), body)
            try:
                now = _timestamp(self.clock())
                report = _report_payload(
                    self.health_provider(now=now), expected_checked_at=now,
                )
            except HealthHTTPFailed:
                raise
            except Exception:
                raise HealthHTTPFailed(
                    "health.http.monitor_unavailable", status=503, retryable=True,
                ) from None
            status = 503 if report["status"] == "blocked" else 200
            return self._send(start_response, status, {
                "schema": RESPONSE_SCHEMA,
                "report": report,
            })
        except HealthHTTPFailed as error:
            return self._send(start_response, error.status, {
                "schema": ERROR_SCHEMA,
                "error_code": error.code,
                "retryable": error.retryable,
            })
        except Exception:
            return self._send(start_response, 503, {
                "schema": ERROR_SCHEMA,
                "error_code": "health.http.internal",
                "retryable": True,
            })
