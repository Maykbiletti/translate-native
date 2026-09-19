#!/usr/bin/env python3
"""Authenticated HTTPS boundary for the hosted watcher-recovery runtime.

The sidecar authenticates the exact request bytes before JSON parsing, starts
only an explicitly named durable operation, and returns content-free runtime
projections.  Its OpenAPI document is origin-free and reconstructed locally.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


START_PATH = "/v1/benchmarks/watcher/recovery/operations"
STATUS_PATH = "/v1/benchmarks/watcher/recovery/status"
READINESS_PATH = "/v1/benchmarks/watcher/recovery/readiness"
OPENAPI_PATH = "/v1/benchmarks/watcher/recovery/openapi"
START_REQUEST_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-start-request.v1"
START_RESPONSE_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-start-response.v1"
STATUS_RESPONSE_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-status-response.v1"
STATUS_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-status.v1"
READINESS_RESPONSE_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-readiness-response.v1"
OPENAPI_RESPONSE_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-openapi-response.v1"
DOCUMENT_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-openapi.v1"
CONTRACT_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-http-contract.v1"
AUTH_REQUEST_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-http-auth.v1"
PRINCIPAL_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-operator.v1"
ERROR_SCHEMA = "blun.website-localization-benchmark-watcher-recovery-http-error.v1"
START_SCOPE = "benchmark-watcher-recovery:start"
STATUS_SCOPE = "benchmark-watcher-recovery:status:read"
READINESS_SCOPE = "benchmark-watcher-recovery:readiness:read"
OPENAPI_SCOPE = "benchmark-watcher-recovery:openapi:read"
VERSION = "6.182.0"
MAX_BODY_BYTES = 4096
MAX_RESPONSE_BYTES = 65536
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")


def _load_runtime():
    path = Path(__file__).resolve().with_name(
        "website_localization_benchmark_watcher_recovery_runtime.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_benchmark_watcher_recovery_http_runtime", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark watcher recovery runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_RUNTIME = _load_runtime()


class BenchmarkWatcherRecoveryHTTPFailed(RuntimeError):
    """Stable, content-free sidecar failure."""

    def __init__(self, code: str, *, status: int, retryable: bool = False):
        if (
            not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None
            or status not in {400, 401, 403, 404, 409, 413, 415, 503}
            or not isinstance(retryable, bool)
        ):
            raise ValueError("benchmark watcher recovery HTTP error is invalid")
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


def _failed(code: str, *, status: int, retryable: bool = False):
    return BenchmarkWatcherRecoveryHTTPFailed(code, status=status, retryable=retryable)


def _canonical(value: Any, maximum: int = MAX_RESPONSE_BYTES) -> bytes:
    try:
        result = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise _failed(
            "benchmark_watcher.recovery_http.response_invalid",
            status=503, retryable=True,
        ) from None
    if not result or len(result) > maximum:
        raise _failed(
            "benchmark_watcher.recovery_http.response_invalid",
            status=503, retryable=True,
        )
    return result


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _principal(value: Any, scope: str) -> None:
    try:
        if not isinstance(value, Mapping) or set(value) != {
            "schema", "operator_id", "credential_id", "credential_version", "scope",
        } or value["schema"] != PRINCIPAL_SCHEMA or value["scope"] != scope:
            raise ValueError
        for name in ("operator_id", "credential_id", "credential_version"):
            if not isinstance(value[name], str) or IDENTIFIER.fullmatch(value[name]) is None:
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise _failed(
            "benchmark_watcher.recovery_http.authentication_failed", status=401,
        ) from None


def _start_request(value: Any) -> str:
    try:
        if not isinstance(value, Mapping) or set(value) != {
            "schema", "operation_id",
        } or value["schema"] != START_REQUEST_SCHEMA:
            raise ValueError
        operation_id = value["operation_id"]
        if not isinstance(operation_id, str) or IDENTIFIER.fullmatch(operation_id) is None:
            raise ValueError
        return operation_id
    except (KeyError, TypeError, ValueError):
        raise _failed("benchmark_watcher.recovery_http.request_invalid", status=400) from None


def _status(value: Any) -> dict[str, Any]:
    try:
        if not isinstance(value, _RUNTIME._RUNNER.RecoveryRunnerStatus):
            raise ValueError
        if value.state not in _RUNTIME._RUNNER.STATES or value.phase not in _RUNTIME._RUNNER.PHASES:
            raise ValueError
        if (
            isinstance(value.attempts, bool) or not isinstance(value.attempts, int)
            or isinstance(value.max_attempts, bool) or not isinstance(value.max_attempts, int)
            or not 0 <= value.attempts <= value.max_attempts <= _RUNTIME._RUNNER.MAX_ATTEMPTS
            or not isinstance(value.generation_observed, bool)
            or (value.last_error_code is not None and (
                not isinstance(value.last_error_code, str)
                or ERROR_CODE.fullmatch(value.last_error_code) is None
            ))
        ):
            raise ValueError
        return {
            "schema": STATUS_SCHEMA,
            "state": value.state,
            "phase": value.phase,
            "attempts": value.attempts,
            "max_attempts": value.max_attempts,
            "generation_observed": value.generation_observed,
            "final": value.state in {"succeeded", "not_required", "failed"},
            "error_code": value.last_error_code,
        }
    except (AttributeError, TypeError, ValueError):
        raise _failed(
            "benchmark_watcher.recovery_http.runtime_response_invalid",
            status=503, retryable=False,
        ) from None


def _readiness(value: Any) -> dict[str, Any]:
    allowed_workers = {"unmanaged", "running", "stopping", "stopped", "failed", "closed"}
    try:
        if not isinstance(value, Mapping) or set(value) != {
            "schema", "status", "worker_state", "recovery_state",
            "recovery_phase", "attempts", "error_code",
        } or value["schema"] != (
            "blun.website-localization-benchmark-watcher-recovery-runtime-readiness.v1"
        ) or value["status"] not in {"ready", "not_ready"} or value["worker_state"] not in allowed_workers:
            raise ValueError
        if value["recovery_state"] is not None and value["recovery_state"] not in _RUNTIME._RUNNER.STATES:
            raise ValueError
        if value["recovery_phase"] is not None and value["recovery_phase"] not in _RUNTIME._RUNNER.PHASES:
            raise ValueError
        attempts = value["attempts"]
        if attempts is not None and (
            isinstance(attempts, bool) or not isinstance(attempts, int)
            or not 0 <= attempts <= _RUNTIME._RUNNER.MAX_ATTEMPTS
        ):
            raise ValueError
        error = value["error_code"]
        if error is not None and (
            not isinstance(error, str) or ERROR_CODE.fullmatch(error) is None
        ):
            raise ValueError
        if (value["status"] == "ready") != (
            value["worker_state"] == "running" and error is None
            and value["recovery_state"] not in {None, "succeeded", "not_required", "failed"}
        ):
            raise ValueError
        return dict(value)
    except (KeyError, TypeError, ValueError):
        raise _failed(
            "benchmark_watcher.recovery_http.runtime_response_invalid",
            status=503, retryable=False,
        ) from None


def _contract() -> dict[str, Any]:
    return {
        "schema": CONTRACT_SCHEMA,
        "version": VERSION,
        "max_request_bytes": MAX_BODY_BYTES,
        "max_response_bytes": MAX_RESPONSE_BYTES,
        "operations": [
            {"method": "GET", "path": OPENAPI_PATH, "scope": OPENAPI_SCOPE},
            {"method": "GET", "path": READINESS_PATH, "scope": READINESS_SCOPE},
            {"method": "GET", "path": STATUS_PATH, "scope": STATUS_SCOPE},
            {"method": "POST", "path": START_PATH, "scope": START_SCOPE},
        ],
        "content_free": True,
        "origin_free": True,
    }


def _openapi() -> dict[str, Any]:
    def closed(properties: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "required": sorted(properties), "properties": dict(properties),
        }

    nullable_error = {
        "oneOf": [
            {"type": "null"},
            {"type": "string", "pattern": ERROR_CODE.pattern},
        ],
    }
    status_schema = closed({
        "schema": {"const": STATUS_SCHEMA},
        "state": {"type": "string", "enum": sorted(_RUNTIME._RUNNER.STATES)},
        "phase": {"type": "string", "enum": sorted(_RUNTIME._RUNNER.PHASES)},
        "attempts": {"type": "integer", "minimum": 0, "maximum": _RUNTIME._RUNNER.MAX_ATTEMPTS},
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": _RUNTIME._RUNNER.MAX_ATTEMPTS},
        "generation_observed": {"type": "boolean"},
        "final": {"type": "boolean"},
        "error_code": nullable_error,
    })
    readiness_schema = closed({
        "schema": {"const": "blun.website-localization-benchmark-watcher-recovery-runtime-readiness.v1"},
        "status": {"type": "string", "enum": ["ready", "not_ready"]},
        "worker_state": {"type": "string", "enum": ["unmanaged", "running", "stopping", "stopped", "failed", "closed"]},
        "recovery_state": {"oneOf": [{"type": "null"}, {"type": "string", "enum": sorted(_RUNTIME._RUNNER.STATES)}]},
        "recovery_phase": {"oneOf": [{"type": "null"}, {"type": "string", "enum": sorted(_RUNTIME._RUNNER.PHASES)}]},
        "attempts": {"oneOf": [{"type": "null"}, {"type": "integer", "minimum": 0, "maximum": _RUNTIME._RUNNER.MAX_ATTEMPTS}]},
        "error_code": nullable_error,
    })
    error = {
        "type": "object", "additionalProperties": False,
        "required": ["error_code", "retryable", "schema"],
        "properties": {
            "schema": {"const": ERROR_SCHEMA},
            "error_code": {"type": "string", "pattern": ERROR_CODE.pattern},
            "retryable": {"type": "boolean"},
        },
    }
    paths = {}
    for operation in _contract()["operations"]:
        success = 200 if operation["method"] == "GET" else 202
        response_component = (
            "OpenAPIResponse" if operation["path"] == OPENAPI_PATH
            else "ReadinessResponse" if operation["path"] == READINESS_PATH
            else "StatusResponse" if operation["path"] == STATUS_PATH
            else "StartResponse"
        )
        item = {
            "operationId": (
                "discoverHostedWatcherRecovery" if operation["path"] == OPENAPI_PATH
                else "readHostedWatcherRecoveryReadiness" if operation["path"] == READINESS_PATH
                else "readHostedWatcherRecoveryStatus" if operation["path"] == STATUS_PATH
                else "startHostedWatcherRecovery"
            ),
            "security": [{"operatorAuthentication": []}],
            "x-required-scope": operation["scope"],
            "responses": {
                str(success): {
                    "description": "Authenticated content-free response.",
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/" + response_component}}},
                },
                "400": {"description": "Fail-closed request error.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                "401": {"description": "Authentication failed.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                "503": {"description": "Runtime unavailable.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
            },
        }
        if operation["method"] == "POST":
            item["parameters"] = [{
                "name": "Idempotency-Key", "in": "header", "required": True,
                "schema": {"type": "string", "pattern": IDENTIFIER.pattern},
            }]
            item["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "additionalProperties": False,
                "required": ["operation_id", "schema"],
                "properties": {"schema": {"const": START_REQUEST_SCHEMA}, "operation_id": {"type": "string", "pattern": IDENTIFIER.pattern}},
            }}}}
        paths[operation["path"]] = {operation["method"].lower(): item}
    document = {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "Hosted Benchmark Watcher Recovery API",
            "version": VERSION,
            "description": "Authenticated explicit start and content-free hosted recovery observation.",
        },
        "paths": paths,
        "components": {
            "securitySchemes": {"operatorAuthentication": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "RecoveryStatus": status_schema,
                "RuntimeReadiness": readiness_schema,
                "StartResponse": closed({
                    "schema": {"const": START_RESPONSE_SCHEMA},
                    "accepted": {"const": True},
                    "status": {"$ref": "#/components/schemas/RecoveryStatus"},
                }),
                "StatusResponse": closed({
                    "schema": {"const": STATUS_RESPONSE_SCHEMA},
                    "status": {"$ref": "#/components/schemas/RecoveryStatus"},
                }),
                "ReadinessResponse": closed({
                    "schema": {"const": READINESS_RESPONSE_SCHEMA},
                    "readiness": {"$ref": "#/components/schemas/RuntimeReadiness"},
                }),
                "OpenAPIResponse": closed({
                    "schema": {"const": OPENAPI_RESPONSE_SCHEMA},
                    "contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "openapi_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "openapi": {"type": "object"},
                }),
                "ErrorResponse": error,
            },
        },
        "x-schema": DOCUMENT_SCHEMA,
        "x-contract-schema": CONTRACT_SCHEMA,
        "x-contract-sha256": _sha(_contract()),
        "x-origin-free": True,
        "x-runtime-validation-authoritative": True,
    }
    return json.loads(_canonical(document).decode("utf-8"))


class BenchmarkWatcherRecoveryHTTPApplication:
    """Strict WSGI sidecar for one process-owned recovery runtime."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if not isinstance(runtime, _RUNTIME.DurableBenchmarkWatcherRecoveryRuntime):
            raise TypeError("runtime is invalid")
        if not callable(authenticator):
            raise TypeError("authenticator is invalid")
        self.runtime = runtime
        self.authenticator = authenticator

    @staticmethod
    def _headers(environ: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        result = []
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").lower()
            elif key in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                name = key.replace("_", "-").lower()
            else:
                continue
            if (
                HEADER_NAME.fullmatch(name) is None or not isinstance(value, str)
                or len(value) > MAX_HEADER_VALUE or "\r" in value or "\n" in value
            ):
                raise _failed("benchmark_watcher.recovery_http.headers_invalid", status=400)
            result.append((name, value))
        result.sort()
        if len(result) > MAX_HEADERS or len({name for name, _ in result}) != len(result):
            raise _failed("benchmark_watcher.recovery_http.headers_invalid", status=400)
        return tuple(result)

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
            raise _failed("benchmark_watcher.recovery_http.transfer_encoding", status=400)
        if environ.get("CONTENT_TYPE") != "application/json":
            raise _failed("benchmark_watcher.recovery_http.content_type", status=415)
        try:
            length = int(environ.get("CONTENT_LENGTH"))
        except (TypeError, ValueError):
            raise _failed("benchmark_watcher.recovery_http.body_invalid", status=400) from None
        if length < 1:
            raise _failed("benchmark_watcher.recovery_http.body_invalid", status=400)
        if length > MAX_BODY_BYTES:
            raise _failed("benchmark_watcher.recovery_http.body_too_large", status=413)
        stream = environ.get("wsgi.input")
        if not hasattr(stream, "read"):
            raise _failed("benchmark_watcher.recovery_http.body_invalid", status=400)
        body = stream.read(length)
        if not isinstance(body, bytes) or len(body) != length:
            raise _failed("benchmark_watcher.recovery_http.body_invalid", status=400)
        return body

    def _authenticate(self, method: str, path: str, headers, body: bytes, scope: str) -> None:
        request = {
            "schema": AUTH_REQUEST_SCHEMA, "method": method, "path": path,
            "headers": [list(item) for item in headers],
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        try:
            _principal(self.authenticator(request), scope)
        except BenchmarkWatcherRecoveryHTTPFailed:
            raise
        except Exception:
            raise _failed(
                "benchmark_watcher.recovery_http.authentication_unavailable",
                status=503, retryable=True,
            ) from None

    @staticmethod
    def _send(start_response, status: int, payload: Mapping[str, Any]):
        body = _canonical(dict(payload))
        phrases = {
            200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 409: "Conflict",
            413: "Content Too Large", 415: "Unsupported Media Type",
            503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
        ])
        return [body]

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            if not isinstance(environ, Mapping):
                raise _failed("benchmark_watcher.recovery_http.environment_invalid", status=400)
            if environ.get("wsgi.url_scheme") != "https":
                raise _failed("benchmark_watcher.recovery_http.https_required", status=400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise _failed("benchmark_watcher.recovery_http.query_invalid", status=400)
            method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO")
            routes = {
                ("GET", OPENAPI_PATH): OPENAPI_SCOPE,
                ("GET", READINESS_PATH): READINESS_SCOPE,
                ("GET", STATUS_PATH): STATUS_SCOPE,
                ("POST", START_PATH): START_SCOPE,
            }
            if (method, path) not in routes:
                raise _failed("benchmark_watcher.recovery_http.route_not_found", status=404)
            if method == "GET":
                if (
                    environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}
                    or environ.get("CONTENT_TYPE") not in {None, ""}
                    or environ.get("CONTENT_LENGTH") not in {None, "", "0"}
                ):
                    raise _failed("benchmark_watcher.recovery_http.body_invalid", status=400)
                headers = self._headers(environ)
                self._authenticate(method, path, headers, b"", routes[(method, path)])
                if path == OPENAPI_PATH:
                    document, contract = _openapi(), _contract()
                    return self._send(start_response, 200, {
                        "schema": OPENAPI_RESPONSE_SCHEMA,
                        "contract_sha256": _sha(contract),
                        "openapi_sha256": _sha(document), "openapi": document,
                    })
                if path == READINESS_PATH:
                    return self._send(start_response, 200, {
                        "schema": READINESS_RESPONSE_SCHEMA,
                        "readiness": _readiness(self.runtime.readiness()),
                    })
                return self._send(start_response, 200, {
                    "schema": STATUS_RESPONSE_SCHEMA,
                    "status": _status(self.runtime.status()),
                })
            body = self._body(environ)
            headers = self._headers(environ)
            self._authenticate(method, path, headers, body, START_SCOPE)
            try:
                payload = json.loads(
                    body.decode("utf-8"), object_pairs_hook=_pairs,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                raise _failed("benchmark_watcher.recovery_http.request_invalid", status=400) from None
            operation_id = _start_request(payload)
            if dict(headers).get("idempotency-key") != operation_id:
                raise _failed("benchmark_watcher.recovery_http.idempotency_key_invalid", status=400)
            snapshot = self.runtime.start(operation_id)
            projected = _status(snapshot)
            if not projected["final"]:
                self.runtime.start_worker()
            return self._send(start_response, 202, {
                "schema": START_RESPONSE_SCHEMA, "accepted": True,
                "status": projected,
            })
        except BenchmarkWatcherRecoveryHTTPFailed as error:
            return self._send(start_response, error.status, {
                "schema": ERROR_SCHEMA, "error_code": error.code,
                "retryable": error.retryable,
            })
        except _RUNTIME.BenchmarkWatcherRecoveryRuntimeBlocked as error:
            status = 409 if error.code.endswith(("operation_conflict", "operation_invalid")) else 503
            return self._send(start_response, status, {
                "schema": ERROR_SCHEMA,
                "error_code": "benchmark_watcher.recovery_http.runtime_blocked",
                "retryable": status == 503,
            })
        except Exception:
            return self._send(start_response, 503, {
                "schema": ERROR_SCHEMA,
                "error_code": "benchmark_watcher.recovery_http.internal",
                "retryable": True,
            })


def wsgi_environ(method: str, path: str, *, body: bytes = b"", headers=None):
    """Build a minimal HTTPS WSGI environment for local adapters and tests."""
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
        "wsgi.url_scheme": "https", "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)) if body else "",
    }
    if body:
        environ["CONTENT_TYPE"] = "application/json"
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    return environ
