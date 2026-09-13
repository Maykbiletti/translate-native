#!/usr/bin/env python3
"""Authenticated, content-free HTTP discovery for the website submission edge.

The WSGI boundary exposes only the complete capability projection of an owned
website submission runtime. Authentication receives canonical request metadata
and the empty-body digest before the runtime or its downstream sidecar is read.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "cannot load submission-capabilities HTTP dependency"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_SUBMISSION = _load_module(
    "blun_website_localization_submission_capabilities_http_runtime",
    _ROOT
    / "integrations"
    / "website_localization_cms_source_delivery_submission_runtime.py",
)

API_SCHEMA = (
    "blun.cms-source-delivery-submission-capabilities-http.v1"
)
ERROR_SCHEMA = (
    "blun.cms-source-delivery-submission-capabilities-http-error.v1"
)
AUTH_REQUEST_SCHEMA = _SUBMISSION.CAPABILITIES_HTTP_AUTH_REQUEST_SCHEMA
PRINCIPAL_SCHEMA = _SUBMISSION.CAPABILITIES_HTTP_PRINCIPAL_SCHEMA
CAPABILITIES_RESPONSE_SCHEMA = _SUBMISSION.CAPABILITIES_HTTP_RESPONSE_SCHEMA
CAPABILITIES_PATH = _SUBMISSION.CAPABILITIES_HTTP_PATH
CAPABILITIES_SCOPE = _SUBMISSION.CAPABILITIES_HTTP_SCOPE

MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class SubmissionCapabilitiesHTTPBlocked(RuntimeError):
    """Stable, content-free public boundary failure."""

    def __init__(self, code: str, status: int):
        if ERROR_CODE.fullmatch(code) is None or status not in {
            400, 401, 403, 404, 405, 503,
        }:
            raise ValueError("invalid submission-capabilities HTTP failure")
        super().__init__(code)
        self.code = code
        self.status = status


def _blocked(code: str, status: int) -> SubmissionCapabilitiesHTTPBlocked:
    return SubmissionCapabilitiesHTTPBlocked(
        "submission_capabilities_http." + code, status,
    )


def _canonical(value: Any) -> bytes:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise _blocked("response_invalid", 503) from None
    if not result or len(result) > 1_000_000:
        raise _blocked("response_invalid", 503)
    return result


def _token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or TOKEN.fullmatch(value) is None
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise ValueError
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ValueError
    return value


def _principal(value: Any) -> dict[str, str]:
    fields = {
        "schema", "principal_id", "credential_id", "credential_version",
        "scope",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _blocked("authentication_failed", 401)
    try:
        if value["scope"] != CAPABILITIES_SCOPE:
            raise _blocked("scope_rejected", 403)
        if value["schema"] != PRINCIPAL_SCHEMA:
            raise ValueError
        for name in fields - {"schema"}:
            _token(value[name])
    except SubmissionCapabilitiesHTTPBlocked:
        raise
    except (KeyError, TypeError, ValueError):
        raise _blocked("authentication_failed", 401) from None
    return dict(value)


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
            raise _blocked("headers_invalid", 400)
        headers.append((name, value))
    headers.sort()
    if (
        len(headers) > MAX_HEADERS
        or len({name for name, _ in headers}) != len(headers)
    ):
        raise _blocked("headers_invalid", 400)
    return tuple(headers)


def _binding(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "status", "database_role",
        "delivery_capabilities_sha256", "runtime_capabilities_sha256",
        "commercial_rendering_registry_sha256", "binding_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError
    if (
        value["schema"]
        != "blun.cms-source-delivery-runtime-capability-binding.v1"
        or value["status"] != "verified"
        or value["database_role"] != "source_delivery"
    ):
        raise ValueError
    for name in fields - {"schema", "status", "database_role"}:
        _sha256(value[name])
    return dict(value)


def _capabilities(value: Any) -> dict[str, Any]:
    try:
        payload = value.as_payload()
        fields = {
            "schema", "operations", "retry_budgets", "semantics",
            "sidecar_capabilities_sha256", "source_capabilities_sha256",
            "website_capability_binding", "sha256",
        }
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError
        if payload["schema"] != _SUBMISSION.CAPABILITIES_SCHEMA:
            raise ValueError
        operations = payload["operations"]
        expected_operations = {
            "capabilities_http": {
                "kind": "read",
                "method": "GET",
                "path": CAPABILITIES_PATH,
                "scope": CAPABILITIES_SCOPE,
                "principal_schema": PRINCIPAL_SCHEMA,
                "request_schema": None,
                "response_schema": CAPABILITIES_RESPONSE_SCHEMA,
            },
            "enqueue_change": {
                "kind": "write",
                "method": "POST",
                "path": _SUBMISSION.CHANGE_HTTP_PATH,
                "scope": _SUBMISSION.CHANGE_HTTP_SCOPE,
                "principal_schema": _SUBMISSION.WRITE_HTTP_PRINCIPAL_SCHEMA,
                "request_schema": _SUBMISSION.CHANGE_HTTP_REQUEST_SCHEMA,
                "payload_schemas": [_SUBMISSION._ADAPTER.CHANGE_SCHEMA],
                "response_schema": _SUBMISSION.CHANGE_HTTP_RESPONSE_SCHEMA,
            },
            "enqueue_removal": {
                "kind": "write",
                "method": "POST",
                "path": _SUBMISSION.REMOVAL_HTTP_PATH,
                "scope": _SUBMISSION.REMOVAL_HTTP_SCOPE,
                "principal_schema": _SUBMISSION.WRITE_HTTP_PRINCIPAL_SCHEMA,
                "request_schema": _SUBMISSION.REMOVAL_HTTP_REQUEST_SCHEMA,
                "payload_schemas": [
                    _SUBMISSION._ADAPTER.CANCELLATION_SCHEMA,
                    _SUBMISSION._ADAPTER.TOMBSTONE_SCHEMA,
                ],
                "response_schema": _SUBMISSION.REMOVAL_HTTP_RESPONSE_SCHEMA,
            },
            "submission_status": {
                "kind": "read",
                "method": "POST",
                "path": _SUBMISSION.STATUS_HTTP_PATH,
                "scope": _SUBMISSION.STATUS_HTTP_SCOPE,
                "principal_schema": _SUBMISSION.READ_HTTP_PRINCIPAL_SCHEMA,
                "request_schema": _SUBMISSION.STATUS_HTTP_REQUEST_SCHEMA,
                "result_schema": _SUBMISSION.STATUS_SCHEMA,
                "response_schema": _SUBMISSION.STATUS_HTTP_RESPONSE_SCHEMA,
            },
            "submission_lifecycle": {
                "kind": "read",
                "method": "POST",
                "path": _SUBMISSION.LIFECYCLE_HTTP_PATH,
                "scope": _SUBMISSION.LIFECYCLE_HTTP_SCOPE,
                "principal_schema": _SUBMISSION.READ_HTTP_PRINCIPAL_SCHEMA,
                "request_schema": _SUBMISSION.LIFECYCLE_HTTP_REQUEST_SCHEMA,
                "result_schema": _SUBMISSION.LIFECYCLE_SCHEMA,
                "response_schema": _SUBMISSION.LIFECYCLE_HTTP_RESPONSE_SCHEMA,
            },
            "submission_health": {
                "kind": "read",
                "response_schema": _SUBMISSION.HEALTH_SCHEMA,
            },
            "submission_pipeline_health": {
                "kind": "read",
                "method": "GET",
                "path": _SUBMISSION.PIPELINE_HEALTH_HTTP_PATH,
                "scope": _SUBMISSION.PIPELINE_HEALTH_HTTP_SCOPE,
                "principal_schema": _SUBMISSION.OPERATOR_HTTP_PRINCIPAL_SCHEMA,
                "request_schema": None,
                "result_schema": _SUBMISSION.PIPELINE_HEALTH_SCHEMA,
                "response_schema": (
                    _SUBMISSION.PIPELINE_HEALTH_HTTP_RESPONSE_SCHEMA
                ),
            },
            "submission_readiness": {
                "kind": "read",
                "response_schema": _SUBMISSION.READINESS_SCHEMA,
            },
            "submission_pipeline_readiness": {
                "kind": "read",
                "method": "GET",
                "path": _SUBMISSION.PIPELINE_READINESS_HTTP_PATH,
                "scope": _SUBMISSION.PIPELINE_READINESS_HTTP_SCOPE,
                "principal_schema": _SUBMISSION.OPERATOR_HTTP_PRINCIPAL_SCHEMA,
                "request_schema": None,
                "result_schema": _SUBMISSION.PIPELINE_READINESS_SCHEMA,
                "response_schema": (
                    _SUBMISSION.PIPELINE_READINESS_HTTP_RESPONSE_SCHEMA
                ),
            },
        }
        if not isinstance(operations, Mapping) or operations != expected_operations:
            raise ValueError
        retry_budgets = payload["retry_budgets"]
        if not isinstance(retry_budgets, Mapping):
            raise ValueError
        middle = retry_budgets.get("sidecar_to_source")
        if (
            not isinstance(middle, Mapping)
            or set(middle) != {"configured_per_runtime", "maximum_attempts"}
            or middle["configured_per_runtime"] is not True
            or isinstance(middle["maximum_attempts"], bool)
            or not isinstance(middle["maximum_attempts"], int)
            or not 1 <= middle["maximum_attempts"] <= 20
            or retry_budgets != {
                "website_to_sidecar": {
                    "configured_per_request": True,
                    "minimum": 1,
                    "maximum": 20,
                },
                "sidecar_to_source": dict(middle),
                "source_processing": {
                    "configured_per_request": True,
                    "minimum": 1,
                    "maximum": 20,
                },
            }
        ):
            raise ValueError
        if payload["semantics"] != {
            "content_free": True,
            "accepted_means": "durable_website_outbox_acceptance",
            "accepted_implies_publication": False,
            "translation_generation": False,
            "publication_authority": False,
        }:
            raise ValueError
        _sha256(payload["sidecar_capabilities_sha256"])
        _sha256(payload["source_capabilities_sha256"])
        payload = dict(payload)
        payload["website_capability_binding"] = _binding(
            payload["website_capability_binding"]
        )
        claimed = _sha256(payload.pop("sha256"))
        actual = hashlib.sha256(_canonical(payload)).hexdigest()
        if claimed != actual:
            raise ValueError
        payload["sha256"] = claimed
        return payload
    except SubmissionCapabilitiesHTTPBlocked:
        raise
    except Exception:
        raise _blocked("runtime_response_invalid", 503) from None


class SubmissionCapabilitiesHTTPApplication:
    """Strict authenticated WSGI projection of one live website edge."""

    def __init__(
        self,
        runtime: Any,
        authenticator: Callable[[dict[str, Any]], Any],
    ):
        if not callable(getattr(runtime, "submission_capabilities", None)):
            raise TypeError("runtime must provide submission_capabilities")
        if not callable(authenticator):
            raise TypeError("authenticator must be callable")
        self.runtime = runtime
        self.authenticator = authenticator

    @staticmethod
    def _send(start_response, status: int, payload: Mapping[str, Any]):
        body = _canonical(dict(payload))
        phrases = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return [body]

    @classmethod
    def _error(cls, start_response, failure):
        return cls._send(start_response, failure.status, {
            "schema": ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": failure.code,
        })

    def __call__(
        self,
        environ: Mapping[str, Any],
        start_response: Callable[..., Any],
    ):
        try:
            if not isinstance(environ, Mapping):
                raise _blocked("environment_invalid", 400)
            if environ.get("PATH_INFO") != CAPABILITIES_PATH:
                raise _blocked("route_not_found", 404)
            if environ.get("REQUEST_METHOD") != "GET":
                raise _blocked("method_not_allowed", 405)
            if environ.get("wsgi.url_scheme") != "https":
                raise _blocked("https_required", 400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise _blocked("query_rejected", 400)
            if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
                raise _blocked("transfer_encoding_rejected", 400)
            if environ.get("CONTENT_LENGTH") not in {None, "", "0"}:
                raise _blocked("body_not_allowed", 400)
            if environ.get("CONTENT_TYPE") not in {None, ""}:
                raise _blocked("body_not_allowed", 400)

            request = {
                "schema": AUTH_REQUEST_SCHEMA,
                "method": "GET",
                "path": CAPABILITIES_PATH,
                "headers": [list(item) for item in _headers(environ)],
                "body_sha256": EMPTY_SHA256,
            }
            try:
                principal = self.authenticator(request)
            except Exception:
                raise _blocked("authentication_unavailable", 503) from None
            _principal(principal)

            try:
                capabilities = _capabilities(
                    self.runtime.submission_capabilities()
                )
            except SubmissionCapabilitiesHTTPBlocked:
                raise
            except Exception:
                raise _blocked("runtime_blocked", 503) from None
            return self._send(start_response, 200, {
                "schema": CAPABILITIES_RESPONSE_SCHEMA,
                "api_schema": API_SCHEMA,
                "capabilities": capabilities,
            })
        except SubmissionCapabilitiesHTTPBlocked as failure:
            return self._error(start_response, failure)


def build_submission_capabilities_http(
    runtime: Any,
    authenticator: Callable[[dict[str, Any]], Any],
) -> SubmissionCapabilitiesHTTPApplication:
    """Build the public read-only adapter without altering runtime ownership."""

    return SubmissionCapabilitiesHTTPApplication(runtime, authenticator)
