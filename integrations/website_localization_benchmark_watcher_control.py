#!/usr/bin/env python3
"""Authenticated, idempotent operator recovery for the benchmark watcher.

The endpoint can only rearm a terminally failed report watcher. It never
starts network work, changes a live lease, or replaces a final report.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


REARM_PATH = "/v1/benchmarks/watcher/rearm"
STATUS_PATH = "/v1/benchmarks/watcher/status"
REQUEST_SCHEMA = "blun.website-localization-benchmark-watcher-rearm-request.v1"
RESPONSE_SCHEMA = "blun.website-localization-benchmark-watcher-rearm-response.v1"
RECEIPT_SCHEMA = "blun.website-localization-benchmark-watcher-rearm-receipt.v1"
STATUS_RESPONSE_SCHEMA = (
    "blun.website-localization-benchmark-watcher-control-status-response.v1"
)
STATUS_SCHEMA = "blun.website-localization-benchmark-watcher-control-status.v1"
AUTH_REQUEST_SCHEMA = "blun.website-localization-benchmark-watcher-control-auth.v1"
PRINCIPAL_SCHEMA = "blun.website-localization-benchmark-watcher-operator.v1"
ERROR_SCHEMA = "blun.website-localization-benchmark-watcher-control-error.v1"
REARM_SCOPE = "benchmark-watcher:rearm"
STATUS_SCOPE = "benchmark-watcher:status:read"
MAX_BODY_BYTES = 4096
MAX_HEADERS = 64
MAX_HEADER_VALUE = 4096
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_META_COLUMNS = ("singleton", "schema_version")
_REARM_COLUMNS = (
    "request_key_sha256", "request_sha256", "previous_attempts",
    "rearmed_at", "response_json", "response_sha256",
)


def _load_watcher():
    path = Path(__file__).resolve().with_name(
        "website_localization_benchmark_watcher.py"
    )
    spec = importlib.util.spec_from_file_location(
        "blun_website_localization_benchmark_watcher_control_watcher", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("benchmark watcher is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_WATCHER = _load_watcher()


class BenchmarkWatcherControlFailed(RuntimeError):
    """Stable, content-free operator control failure."""

    def __init__(self, code: str, *, status: int, retryable: bool = False):
        if ERROR_CODE.fullmatch(code) is None or status not in {
            400, 401, 403, 404, 409, 413, 415, 503,
        } or not isinstance(retryable, bool):
            raise ValueError("benchmark watcher control error is invalid")
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class BenchmarkWatcherOperator:
    operator_id: str
    credential_id: str
    credential_version: str
    scope: str


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise BenchmarkWatcherControlFailed(
            "benchmark_watcher.control.response_invalid",
            status=503, retryable=True,
        ) from None
    if not encoded or len(encoded) > MAX_BODY_BYTES:
        raise BenchmarkWatcherControlFailed(
            "benchmark_watcher.control.response_invalid",
            status=503, retryable=True,
        )
    return encoded


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 10**12:
        raise ValueError
    return result


def _principal(value: Any, expected_scope: str) -> BenchmarkWatcherOperator:
    try:
        if not isinstance(value, Mapping) or set(value) != {
            "schema", "operator_id", "credential_id", "credential_version",
            "scope",
        } or value["schema"] != PRINCIPAL_SCHEMA or value["scope"] != expected_scope:
            raise ValueError
        return BenchmarkWatcherOperator(
            _identifier(value["operator_id"]),
            _identifier(value["credential_id"]),
            _identifier(value["credential_version"]),
            value["scope"],
        )
    except (KeyError, TypeError, ValueError):
        raise BenchmarkWatcherControlFailed(
            "benchmark_watcher.control.authentication_failed", status=401,
        ) from None


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _request(value: Any) -> dict[str, Any]:
    try:
        if not isinstance(value, Mapping) or set(value) != {
            "schema", "request_id", "expected_attempts",
            "expected_failed_at", "expected_error_code",
        } or value["schema"] != REQUEST_SCHEMA:
            raise ValueError
        request_id = _identifier(value["request_id"])
        attempts = value["expected_attempts"]
        if (
            isinstance(attempts, bool) or not isinstance(attempts, int)
            or not 1 <= attempts <= _WATCHER.MAX_ATTEMPTS
        ):
            raise ValueError
        failed_at = _timestamp(value["expected_failed_at"])
        error_code = value["expected_error_code"]
        if not isinstance(error_code, str) or ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError
        return {
            "schema": REQUEST_SCHEMA,
            "request_id": request_id,
            "expected_attempts": attempts,
            "expected_failed_at": failed_at,
            "expected_error_code": error_code,
        }
    except (KeyError, TypeError, ValueError):
        raise BenchmarkWatcherControlFailed(
            "benchmark_watcher.control.request_invalid", status=400,
        ) from None


class DurableBenchmarkWatcherRearmController:
    """Atomically rearm one failed watcher with durable request replay."""

    def __init__(self, watcher: Any):
        if (
            not isinstance(getattr(watcher, "connection", None), sqlite3.Connection)
            or not callable(getattr(watcher, "_validate_schema", None))
            or not callable(getattr(watcher, "_validated_row", None))
            or not callable(getattr(watcher, "_row", None))
        ):
            raise TypeError("watcher must be DurableBenchmarkReportWatcher")
        self.watcher = watcher
        self.connection = watcher.connection
        self._initialize()

    def _initialize(self) -> None:
        with _WATCHER._transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_watcher_rearm_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                )
            """)
            self.connection.execute("""
                INSERT OR IGNORE INTO benchmark_watcher_rearm_meta VALUES (1, 1)
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_watcher_rearms (
                    request_key_sha256 TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    previous_attempts INTEGER NOT NULL CHECK (previous_attempts > 0),
                    rearmed_at REAL NOT NULL,
                    response_json TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL
                )
            """)
        self._validate_schema()

    def _validate_schema(self) -> None:
        try:
            self.watcher._validate_schema()
        except Exception:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.schema_invalid",
                status=503, retryable=False,
            ) from None
        meta_columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(benchmark_watcher_rearm_meta)"
        ).fetchall())
        columns = tuple(row["name"] for row in self.connection.execute(
            "PRAGMA table_info(benchmark_watcher_rearms)"
        ).fetchall())
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM benchmark_watcher_rearm_meta"
        ).fetchall()
        if (
            meta_columns != _META_COLUMNS or columns != _REARM_COLUMNS
            or len(meta) != 1 or tuple(meta[0]) != (1, 1)
        ):
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.schema_invalid",
                status=503, retryable=False,
            )

    @staticmethod
    def _stored_receipt(row: sqlite3.Row, request_sha256: str) -> dict[str, Any]:
        try:
            if (
                not isinstance(row["request_sha256"], str)
                or SHA256.fullmatch(row["request_sha256"]) is None
                or not isinstance(row["response_sha256"], str)
                or SHA256.fullmatch(row["response_sha256"]) is None
                or not isinstance(row["response_json"], str)
                or isinstance(row["previous_attempts"], bool)
                or not isinstance(row["previous_attempts"], int)
                or row["previous_attempts"] < 1
                or _timestamp(row["rearmed_at"]) != row["rearmed_at"]
            ):
                raise ValueError
            if row["request_sha256"] != request_sha256:
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.idempotency_conflict", status=409,
                )
            encoded = row["response_json"].encode("utf-8")
            if hashlib.sha256(encoded).hexdigest() != row["response_sha256"]:
                raise ValueError
            receipt = json.loads(row["response_json"], object_pairs_hook=_pairs)
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {
                    "schema", "request_sha256", "previous_state",
                    "previous_attempts", "previous_error_code",
                    "failed_at", "state", "rearmed_at",
                }
                or receipt["schema"] != RECEIPT_SCHEMA
                or receipt["request_sha256"] != request_sha256
                or SHA256.fullmatch(receipt["request_sha256"]) is None
                or receipt["previous_state"] != "failed"
                or receipt["state"] != "pending"
                or isinstance(receipt["previous_attempts"], bool)
                or not isinstance(receipt["previous_attempts"], int)
                or receipt["previous_attempts"] != row["previous_attempts"]
                or not isinstance(receipt["previous_error_code"], str)
                or ERROR_CODE.fullmatch(receipt["previous_error_code"]) is None
                or _timestamp(receipt["failed_at"]) > receipt["rearmed_at"]
                or _timestamp(receipt["rearmed_at"]) != row["rearmed_at"]
            ):
                raise ValueError
            return receipt
        except BenchmarkWatcherControlFailed:
            raise
        except Exception:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.evidence_invalid",
                status=503, retryable=False,
            ) from None

    def rearm(self, request: Mapping[str, Any], *, now: float | int) -> dict[str, Any]:
        request = _request(request)
        try:
            now = _timestamp(now)
        except ValueError:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.clock_invalid",
                status=503, retryable=True,
            ) from None
        request_bytes = _canonical_json(request)
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        request_key_sha256 = hashlib.sha256(
            request["request_id"].encode("utf-8")
        ).hexdigest()
        with _WATCHER._transaction(self.connection):
            self._validate_schema()
            stored = self.connection.execute(
                "SELECT * FROM benchmark_watcher_rearms WHERE request_key_sha256 = ?",
                (request_key_sha256,),
            ).fetchone()
            if stored is not None:
                return self._stored_receipt(stored, request_sha256)
            try:
                row = self.watcher._row()
                self.watcher._validated_row(row)
            except Exception:
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.state_invalid",
                    status=503, retryable=False,
                ) from None
            if row["state"] == "leased":
                code = "benchmark_watcher.control.lease_active"
            elif row["state"] == "succeeded":
                code = "benchmark_watcher.control.result_final"
            elif row["state"] != "failed":
                code = "benchmark_watcher.control.rearm_not_required"
            elif (
                row["attempts"] != request["expected_attempts"]
                or float(row["updated_at"]) != request["expected_failed_at"]
                or row["last_error_code"] != request["expected_error_code"]
            ):
                code = "benchmark_watcher.control.state_conflict"
            elif now < float(row["updated_at"]):
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.clock_invalid",
                    status=503, retryable=True,
                )
            else:
                code = None
            if code is not None:
                raise BenchmarkWatcherControlFailed(code, status=409)
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "request_sha256": request_sha256,
                "previous_state": "failed",
                "previous_attempts": row["attempts"],
                "previous_error_code": row["last_error_code"],
                "failed_at": float(row["updated_at"]),
                "state": "pending",
                "rearmed_at": now,
            }
            response_json = _canonical_json(receipt).decode("utf-8")
            response_sha256 = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
            self.connection.execute("""
                UPDATE benchmark_report_watcher
                SET state = 'pending', attempts = 0, next_attempt_at = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE singleton = 1
            """, (now, now))
            self.connection.execute("""
                INSERT INTO benchmark_watcher_rearms (
                    request_key_sha256, request_sha256, previous_attempts,
                    rearmed_at, response_json, response_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                request_key_sha256, request_sha256, row["attempts"], now,
                response_json, response_sha256,
            ))
            return receipt

    def status(self, *, now: float | int) -> dict[str, Any]:
        """Return only the exact content-free generation needed for recovery."""
        try:
            now = _timestamp(now)
        except ValueError:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.clock_invalid",
                status=503, retryable=True,
            ) from None
        try:
            self._validate_schema()
            row = self.watcher._row()
            self.watcher._validated_row(row)
            updated_at = _timestamp(row["updated_at"])
            if updated_at > now:
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.clock_invalid",
                    status=503, retryable=True,
                )
            state = row["state"]
            generation = None
            if state == "failed":
                generation = {
                    "attempts": row["attempts"],
                    "failed_at": updated_at,
                    "error_code": row["last_error_code"],
                }
            return {
                "schema": STATUS_SCHEMA,
                "checked_at": now,
                "state": state,
                "rearmable": state == "failed",
                "generation": generation,
            }
        except BenchmarkWatcherControlFailed:
            raise
        except Exception:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.state_invalid",
                status=503, retryable=False,
            ) from None


class BenchmarkWatcherControlHTTPApplication:
    """Strict authenticated WSGI endpoints for watcher status and rearm."""

    def __init__(
        self,
        controller: DurableBenchmarkWatcherRearmController,
        authenticator: Callable[[dict[str, Any]], Any],
        *,
        clock: Callable[[], float],
    ):
        if not isinstance(controller, DurableBenchmarkWatcherRearmController):
            raise TypeError("controller is invalid")
        if not callable(authenticator) or not callable(clock):
            raise TypeError("control dependencies are invalid")
        self.controller = controller
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
                HEADER_NAME.fullmatch(name) is None or not isinstance(value, str)
                or len(value) > MAX_HEADER_VALUE or "\r" in value or "\n" in value
            ):
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.headers_invalid", status=400,
                )
            headers.append((name, value))
        headers.sort()
        if len(headers) > MAX_HEADERS or len({item[0] for item in headers}) != len(headers):
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.headers_invalid", status=400,
            )
        return tuple(headers)

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.transfer_encoding", status=400,
            )
        if environ.get("CONTENT_TYPE") != "application/json":
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.content_type", status=415,
            )
        raw_length = environ.get("CONTENT_LENGTH")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.body_invalid", status=400,
            ) from None
        if length < 1:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.body_invalid", status=400,
            )
        if length > MAX_BODY_BYTES:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.body_too_large", status=413,
            )
        stream = environ.get("wsgi.input")
        if not hasattr(stream, "read"):
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.body_invalid", status=400,
            )
        body = stream.read(length)
        if not isinstance(body, bytes) or len(body) != length:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.body_invalid", status=400,
            )
        return body

    def _authenticate(
        self, method: str, path: str, headers: tuple[tuple[str, str], ...],
        body: bytes, expected_scope: str,
    ) -> None:
        request = {
            "schema": AUTH_REQUEST_SCHEMA,
            "method": method,
            "path": path,
            "headers": [list(item) for item in headers],
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        try:
            _principal(self.authenticator(request), expected_scope)
        except BenchmarkWatcherControlFailed:
            raise
        except Exception:
            raise BenchmarkWatcherControlFailed(
                "benchmark_watcher.control.authentication_unavailable",
                status=503, retryable=True,
            ) from None

    @staticmethod
    def _send(start_response, status: int, payload: Mapping[str, Any]):
        body = _canonical_json(dict(payload))
        phrases = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 409: "Conflict",
            413: "Content Too Large", 415: "Unsupported Media Type",
            503: "Service Unavailable",
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
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.environment_invalid", status=400,
                )
            if environ.get("wsgi.url_scheme") != "https":
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.https_required", status=400,
                )
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.query_invalid", status=400,
                )
            method = environ.get("REQUEST_METHOD")
            path = environ.get("PATH_INFO")
            if (method, path) not in {
                ("GET", STATUS_PATH), ("POST", REARM_PATH),
            }:
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.route_not_found", status=404,
                )
            if method == "GET":
                if (
                    environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}
                    or environ.get("CONTENT_TYPE") not in {None, ""}
                    or environ.get("CONTENT_LENGTH") not in {None, "", "0"}
                ):
                    raise BenchmarkWatcherControlFailed(
                        "benchmark_watcher.control.body_invalid", status=400,
                    )
                headers = self._headers(environ)
                self._authenticate(
                    "GET", STATUS_PATH, headers, b"", STATUS_SCOPE,
                )
                status = self.controller.status(now=self.clock())
                return self._send(start_response, 200, {
                    "schema": STATUS_RESPONSE_SCHEMA,
                    "status": status,
                })
            body = self._body(environ)
            headers = self._headers(environ)
            self._authenticate("POST", REARM_PATH, headers, body, REARM_SCOPE)
            try:
                decoded = body.decode("utf-8")
                payload = json.loads(
                    decoded, object_pairs_hook=_pairs,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.request_invalid", status=400,
                ) from None
            request = _request(payload)
            header_map = dict(headers)
            if header_map.get("idempotency-key") != request["request_id"]:
                raise BenchmarkWatcherControlFailed(
                    "benchmark_watcher.control.idempotency_key_invalid", status=400,
                )
            receipt = self.controller.rearm(request, now=self.clock())
            return self._send(start_response, 200, {
                "schema": RESPONSE_SCHEMA,
                "receipt": receipt,
            })
        except BenchmarkWatcherControlFailed as error:
            return self._send(start_response, error.status, {
                "schema": ERROR_SCHEMA,
                "error_code": error.code,
                "retryable": error.retryable,
            })
        except Exception:
            return self._send(start_response, 503, {
                "schema": ERROR_SCHEMA,
                "error_code": "benchmark_watcher.control.internal",
                "retryable": True,
            })
