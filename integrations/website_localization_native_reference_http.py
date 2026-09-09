#!/usr/bin/env python3
"""Authenticated WSGI boundary for qualified-native benchmark references.

The application is transport-only: it authenticates an editor identity and
one exact locale, then delegates every lease and evidence decision to the
durable localization runtime.  It never verifies a qualification receipt or
creates reference text itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping


CLAIM_REQUEST_SCHEMA = "blun.website-localization-native-reference-http-claim.v1"
CLAIM_RESPONSE_SCHEMA = "blun.website-localization-native-reference-http-lease.v1"
RENEW_REQUEST_SCHEMA = "blun.website-localization-native-reference-http-renew.v1"
SUBMIT_REQUEST_SCHEMA = "blun.website-localization-native-reference-http-submit.v1"
OUTCOME_RESPONSE_SCHEMA = "blun.website-localization-native-reference-http-outcome.v1"
STATUS_RESPONSE_SCHEMA = "blun.website-localization-native-reference-http-status.v1"
ERROR_RESPONSE_SCHEMA = "blun.website-localization-native-reference-http-error.v1"
AUTH_REQUEST_SCHEMA = "blun.website-localization-native-reference-http-auth.v1"
PRINCIPAL_SCHEMA = "blun.website-localization-native-reference-editor.v1"
CLAIM_PATH = "/v1/native-references/claim"
RENEW_PATH = "/v1/native-references/renew"
SUBMIT_PATH = "/v1/native-references/submit"
STATUS_PATH = "/v1/native-references/status"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
LOCALE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class NativeReferenceHTTPFailed(RuntimeError):
    """Stable content-free HTTP adapter failure."""

    def __init__(self, code: str, *, status: int, retryable: bool = False):
        if (
            not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None
            or isinstance(status, bool) or not isinstance(status, int)
            or not 400 <= status <= 599 or not isinstance(retryable, bool)
        ):
            raise ValueError("native-reference HTTP failure is invalid")
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class EditorPrincipal:
    editor_id: str
    target_locale: str
    credential_id: str
    credential_version: str

    @property
    def lease_owner(self) -> str:
        binding = "\n".join((
            self.editor_id,
            self.target_locale,
            self.credential_id,
            self.credential_version,
        )).encode("utf-8")
        return "native-editor-auth:" + hashlib.sha256(binding).hexdigest()


def _duplicates_rejected(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _json_bytes(value: Any, *, maximum: int = MAX_RESPONSE_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise NativeReferenceHTTPFailed(
            "native_reference.http.response_invalid", status=503,
        ) from None
    if not encoded or len(encoded) > maximum:
        raise NativeReferenceHTTPFailed(
            "native_reference.http.response_invalid", status=503,
        )
    return encoded


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise NativeReferenceHTTPFailed(
            "native_reference.http.request_invalid", status=400,
        )
    return value


def _lease_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeReferenceHTTPFailed(
            "native_reference.http.request_invalid", status=400,
        )
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= 86_400:
        raise NativeReferenceHTTPFailed(
            "native_reference.http.request_invalid", status=400,
        )
    return value


def _principal(value: Any) -> EditorPrincipal:
    keys = {
        "schema", "editor_id", "target_locale",
        "credential_id", "credential_version",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or value.get("schema") != PRINCIPAL_SCHEMA
    ):
        raise NativeReferenceHTTPFailed(
            "native_reference.http.authentication_failed", status=401,
        )
    try:
        editor_id = _identifier(value["editor_id"])
        credential_id = _identifier(value["credential_id"])
        credential_version = _identifier(value["credential_version"])
        target_locale = value["target_locale"]
        if not isinstance(target_locale, str) or LOCALE.fullmatch(target_locale) is None:
            raise ValueError
    except Exception:
        raise NativeReferenceHTTPFailed(
            "native_reference.http.authentication_failed", status=401,
        ) from None
    return EditorPrincipal(
        editor_id=editor_id,
        target_locale=target_locale,
        credential_id=credential_id,
        credential_version=credential_version,
    )


class NativeReferenceHTTPApplication:
    """Strict authenticated WSGI API over one localization runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        methods = (
            "claim_native_reference_work_order",
            "native_reference_lease_from_payload",
            "native_reference_http_request_replay",
            "renew_native_reference_work_order",
            "accept_native_reference_submission",
            "native_reference_queue_status",
        )
        if any(not callable(getattr(runtime, name, None)) for name in methods):
            raise ValueError("native-reference runtime is incomplete")
        if not callable(authenticator):
            raise ValueError("native-reference authenticator must be callable")
        self.runtime = runtime
        self.authenticator = authenticator

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
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise NativeReferenceHTTPFailed(
                    "native_reference.http.headers_invalid", status=400,
                )
            headers.append((name, value))
        headers.sort()
        return tuple(headers)

    @staticmethod
    def _read_body(environ: Mapping[str, Any], *, required: bool) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
            raise NativeReferenceHTTPFailed(
                "native_reference.http.transfer_encoding", status=400,
            )
        raw_length = environ.get("CONTENT_LENGTH")
        if not required and raw_length in {None, "", "0"}:
            return b""
        if not isinstance(raw_length, str) or not raw_length.isdigit():
            raise NativeReferenceHTTPFailed(
                "native_reference.http.content_length", status=400,
            )
        try:
            length = int(raw_length)
        except ValueError:
            raise NativeReferenceHTTPFailed(
                "native_reference.http.content_length", status=400,
            ) from None
        if (required and length <= 0) or length > MAX_REQUEST_BYTES:
            raise NativeReferenceHTTPFailed(
                "native_reference.http.request_size", status=413,
            )
        stream = environ.get("wsgi.input")
        if not callable(getattr(stream, "read", None)):
            raise NativeReferenceHTTPFailed(
                "native_reference.http.body_unavailable", status=400,
            )
        try:
            body = stream.read(length)
        except Exception:
            raise NativeReferenceHTTPFailed(
                "native_reference.http.body_unavailable", status=400,
            ) from None
        if not isinstance(body, bytes) or len(body) != length:
            raise NativeReferenceHTTPFailed(
                "native_reference.http.content_length", status=400,
            )
        return body

    @staticmethod
    def _decode_body(body: bytes, environ: Mapping[str, Any]) -> dict[str, Any]:
        content_type = environ.get("CONTENT_TYPE")
        if content_type != "application/json":
            raise NativeReferenceHTTPFailed(
                "native_reference.http.content_type", status=415,
            )
        if body.startswith(b"\xef\xbb\xbf"):
            raise NativeReferenceHTTPFailed(
                "native_reference.http.json_invalid", status=400,
            )
        try:
            value = json.loads(
                body.decode("utf-8", "strict"),
                object_pairs_hook=_duplicates_rejected,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            raise NativeReferenceHTTPFailed(
                "native_reference.http.json_invalid", status=400,
            ) from None
        if not isinstance(value, dict):
            raise NativeReferenceHTTPFailed(
                "native_reference.http.request_invalid", status=400,
            )
        return value

    def _authenticate(
        self, method: str, path: str, headers: tuple[tuple[str, str], ...], body: bytes,
    ) -> EditorPrincipal:
        request = {
            "schema": AUTH_REQUEST_SCHEMA,
            "method": method,
            "path": path,
            "headers": [list(item) for item in headers],
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        try:
            return _principal(self.authenticator(request))
        except NativeReferenceHTTPFailed:
            raise
        except Exception:
            raise NativeReferenceHTTPFailed(
                "native_reference.http.authentication_unavailable",
                status=503,
                retryable=True,
            ) from None

    @staticmethod
    def _runtime_error(error: Exception) -> NativeReferenceHTTPFailed:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            code = "native_reference.http.runtime_unavailable"
        retryable = getattr(error, "retryable", False)
        retryable = retryable if isinstance(retryable, bool) else False
        if code.endswith("http_request_in_progress"):
            retryable = True
        if code.endswith("locale_not_allowed"):
            status = 403
        elif code.endswith((
            "lease_lost", "claim_replay_stale", "http_request_conflict",
            "http_request_in_progress", "http_request_replay_stale",
        )):
            status = 409
        elif ".submission_" in code or code.endswith("submission_invalid"):
            status = 422
        elif retryable or code.endswith(("unavailable", "operation_guard_failed")):
            status = 503
        else:
            status = 400
        return NativeReferenceHTTPFailed(code, status=status, retryable=retryable)

    @staticmethod
    def _outcome(value: Any) -> dict[str, Any]:
        fields = (
            "work_id", "target_locale", "suite_case_key", "status",
            "attempt", "max_attempts", "next_attempt_at", "error_code",
            "error_detail_hash", "artifact_sha256",
        )
        try:
            payload = {name: getattr(value, name) for name in fields}
        except Exception:
            raise NativeReferenceHTTPFailed(
                "native_reference.http.response_invalid", status=503,
            ) from None
        return payload

    @staticmethod
    def _send(start_response: Callable[..., Any], status: int, payload: Any):
        body = b"" if payload is None else _json_bytes(payload)
        phrases = {
            200: "OK", 204: "No Content", 400: "Bad Request",
            401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            409: "Conflict", 413: "Content Too Large",
            415: "Unsupported Media Type", 422: "Unprocessable Content",
            503: "Service Unavailable",
        }
        headers = [
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if body:
            headers.append(("Content-Type", "application/json; charset=utf-8"))
        start_response(f"{status} {phrases.get(status, 'Error')}", headers)
        return [body]

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            method = environ.get("REQUEST_METHOD")
            path = environ.get("PATH_INFO")
            if environ.get("wsgi.url_scheme") != "https":
                raise NativeReferenceHTTPFailed(
                    "native_reference.http.https_required", status=400,
                )
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise NativeReferenceHTTPFailed(
                    "native_reference.http.query_invalid", status=400,
                )
            allowed = {
                CLAIM_PATH: "POST", RENEW_PATH: "POST",
                SUBMIT_PATH: "POST", STATUS_PATH: "GET",
            }
            if path not in allowed or method != allowed[path]:
                raise NativeReferenceHTTPFailed(
                    "native_reference.http.route_not_found", status=404,
                )
            body = self._read_body(environ, required=method == "POST")
            principal = self._authenticate(method, path, self._headers(environ), body)
            if method == "GET":
                status = self.runtime.native_reference_queue_status()
                return self._send(start_response, 200, {
                    "schema": STATUS_RESPONSE_SCHEMA,
                    "editor_locale": principal.target_locale,
                    "queue": status,
                })
            request = self._decode_body(body, environ)
            request_id = _identifier(request.get("request_id"))
            request_sha256 = hashlib.sha256(body).hexdigest()
            if path == CLAIM_PATH:
                if set(request) != {"schema", "request_id", "lease_seconds"} or (
                    request.get("schema") != CLAIM_REQUEST_SCHEMA
                ):
                    raise NativeReferenceHTTPFailed(
                        "native_reference.http.request_invalid", status=400,
                    )
                lease = self.runtime.claim_native_reference_work_order(
                    principal.lease_owner,
                    target_locale=principal.target_locale,
                    request_id=request_id,
                    lease_seconds=_lease_seconds(request["lease_seconds"]),
                )
                if lease is None:
                    return self._send(start_response, 204, None)
                return self._send(start_response, 200, {
                    "schema": CLAIM_RESPONSE_SCHEMA,
                    "request_id": request_id,
                    "lease": lease.as_payload(),
                })
            if path == RENEW_PATH:
                if set(request) != {
                    "schema", "request_id", "lease", "lease_seconds",
                } or request.get("schema") != RENEW_REQUEST_SCHEMA:
                    raise NativeReferenceHTTPFailed(
                        "native_reference.http.request_invalid", status=400,
                    )
                replay = self.runtime.native_reference_http_request_replay(
                    editor_id=principal.lease_owner,
                    target_locale=principal.target_locale,
                    operation="renew",
                    request_id=request_id,
                    request_sha256=request_sha256,
                )
                if replay is not None:
                    lease_payload = dict(request["lease"])
                    lease_payload["lease_expires_at"] = replay[
                        "lease_expires_at"
                    ]
                    return self._send(start_response, 200, {
                        "schema": CLAIM_RESPONSE_SCHEMA,
                        "request_id": request_id,
                        "lease": lease_payload,
                    })
                lease = self.runtime.native_reference_lease_from_payload(
                    request["lease"],
                    editor_id=principal.lease_owner,
                    target_locale=principal.target_locale,
                )
                renewed = self.runtime.renew_native_reference_work_order(
                    lease,
                    lease_seconds=_lease_seconds(request["lease_seconds"]),
                    request_id=request_id,
                    request_sha256=request_sha256,
                )
                return self._send(start_response, 200, {
                    "schema": CLAIM_RESPONSE_SCHEMA,
                    "request_id": request_id,
                    "lease": renewed.as_payload(),
                })
            if set(request) != {
                "schema", "request_id", "lease", "submission",
            } or request.get("schema") != SUBMIT_REQUEST_SCHEMA:
                raise NativeReferenceHTTPFailed(
                    "native_reference.http.request_invalid", status=400,
                )
            replay = self.runtime.native_reference_http_request_replay(
                editor_id=principal.lease_owner,
                target_locale=principal.target_locale,
                operation="submit",
                request_id=request_id,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return self._send(start_response, 200, {
                    "schema": OUTCOME_RESPONSE_SCHEMA,
                    "request_id": request_id,
                    "outcome": replay,
                })
            lease = self.runtime.native_reference_lease_from_payload(
                request["lease"],
                editor_id=principal.lease_owner,
                target_locale=principal.target_locale,
            )
            outcome = self.runtime.accept_native_reference_submission(
                lease,
                request["submission"],
                request_id=request_id,
                request_sha256=request_sha256,
            )
            return self._send(start_response, 200, {
                "schema": OUTCOME_RESPONSE_SCHEMA,
                "request_id": request_id,
                "outcome": self._outcome(outcome),
            })
        except NativeReferenceHTTPFailed as error:
            return self._send(start_response, error.status, {
                "schema": ERROR_RESPONSE_SCHEMA,
                "error_code": error.code,
                "retryable": error.retryable,
            })
        except Exception as error:
            failure = self._runtime_error(error)
            return self._send(start_response, failure.status, {
                "schema": ERROR_RESPONSE_SCHEMA,
                "error_code": failure.code,
                "retryable": failure.retryable,
            })
