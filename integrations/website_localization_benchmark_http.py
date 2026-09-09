"""Authenticated read-only HTTP boundary for benchmark evidence.

The application exposes only content-free campaign progress and an already
stored, reverified, signed benchmark report. It never starts work, signs a
report, or repairs durable state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping


STATUS_PATH = "/v1/benchmarks/status"
REPORT_PATH = "/v1/benchmarks/report"
AUTH_REQUEST_SCHEMA = "blun.website-localization-benchmark-http-auth.v1"
PRINCIPAL_SCHEMA = "blun.website-localization-benchmark-reader.v1"
STATUS_RESPONSE_SCHEMA = "blun.website-localization-benchmark-http-status.v1"
REPORT_RESPONSE_SCHEMA = "blun.website-localization-benchmark-http-report.v1"
ERROR_RESPONSE_SCHEMA = "blun.website-localization-benchmark-http-error.v1"
BENCHMARK_REPORT_SCHEMA = "blun.website-localization-benchmark-report.v11"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}")
ERROR_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CAMPAIGN_ID = re.compile(r"benchmark-campaign-[0-9a-f]{64}")
CAMPAIGN_STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")


class BenchmarkHTTPFailed(RuntimeError):
    """Content-free HTTP boundary failure."""

    def __init__(self, code: str, *, status: int, retryable: bool = False):
        if ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool):
            raise ValueError("benchmark HTTP error is invalid")
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class BenchmarkReaderPrincipal:
    reader_id: str
    campaign_id: str
    credential_id: str
    credential_version: str


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise BenchmarkHTTPFailed(
            "benchmark.http.authentication_failed", status=401,
        )
    return value


def _principal(value: Any) -> BenchmarkReaderPrincipal:
    keys = {
        "schema", "reader_id", "campaign_id",
        "credential_id", "credential_version",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or value.get("schema") != PRINCIPAL_SCHEMA
    ):
        raise BenchmarkHTTPFailed(
            "benchmark.http.authentication_failed", status=401,
        )
    campaign_id = value.get("campaign_id")
    if not isinstance(campaign_id, str) or CAMPAIGN_ID.fullmatch(campaign_id) is None:
        raise BenchmarkHTTPFailed(
            "benchmark.http.authentication_failed", status=401,
        )
    return BenchmarkReaderPrincipal(
        reader_id=_identifier(value.get("reader_id")),
        campaign_id=campaign_id,
        credential_id=_identifier(value.get("credential_id")),
        credential_version=_identifier(value.get("credential_version")),
    )


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise BenchmarkHTTPFailed(
            "benchmark.http.response_invalid", status=503,
        ) from None
    if not encoded or len(encoded) > MAX_RESPONSE_BYTES:
        raise BenchmarkHTTPFailed(
            "benchmark.http.response_invalid", status=503,
        )
    return encoded


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError
    return value


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _status_payload(value: Any) -> dict[str, Any]:
    keys = {
        "campaign_id", "policy_sha256", "suite_sha256", "valid_until",
        "work_count", "counts", "error_counts", "complete", "blocked",
        "report_finalization",
    }
    try:
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ValueError
        payload = dict(value)
        if CAMPAIGN_ID.fullmatch(payload["campaign_id"]) is None:
            raise ValueError
        if (
            SHA256.fullmatch(payload["policy_sha256"]) is None
            or SHA256.fullmatch(payload["suite_sha256"]) is None
        ):
            raise ValueError
        _timestamp(payload["valid_until"])
        work_count = _count(payload["work_count"])
        if work_count < 1:
            raise ValueError
        counts = payload["counts"]
        if not isinstance(counts, Mapping) or set(counts) != set(CAMPAIGN_STATUSES):
            raise ValueError
        counts = {name: _count(counts[name]) for name in CAMPAIGN_STATUSES}
        if sum(counts.values()) != work_count:
            raise ValueError
        errors = payload["error_counts"]
        if not isinstance(errors, Mapping):
            raise ValueError
        errors = dict(errors)
        for code, count in errors.items():
            if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                raise ValueError
            _count(count)
        if sum(errors.values()) > work_count:
            raise ValueError
        if not isinstance(payload["complete"], bool) or not isinstance(
            payload["blocked"], bool,
        ):
            raise ValueError
        if payload["complete"] != (counts["succeeded"] == work_count):
            raise ValueError
        finalization = payload["report_finalization"]
        if not isinstance(finalization, Mapping) or set(finalization) != {
            "status", "attempt", "max_attempts", "next_attempt_at", "error_code",
        }:
            raise ValueError
        finalization = dict(finalization)
        if finalization["status"] not in CAMPAIGN_STATUSES:
            raise ValueError
        attempt = _count(finalization["attempt"])
        maximum = _count(finalization["max_attempts"])
        if not 0 <= attempt <= maximum or maximum < 1:
            raise ValueError
        _timestamp(finalization["next_attempt_at"])
        error = finalization["error_code"]
        if error is not None and (
            not isinstance(error, str) or ERROR_CODE.fullmatch(error) is None
        ):
            raise ValueError
        if finalization["status"] == "pending" and (
            attempt != 0 or error is not None
        ):
            raise ValueError
        if finalization["status"] != "pending" and attempt < 1:
            raise ValueError
        if finalization["status"] in {"retry_wait", "failed"}:
            if error is None:
                raise ValueError
        elif error is not None:
            raise ValueError
        if payload["blocked"] != (
            counts["failed"] > 0 or finalization["status"] == "failed"
        ):
            raise ValueError
        payload["counts"] = counts
        payload["error_counts"] = errors
        payload["report_finalization"] = finalization
        return json.loads(_canonical_json(payload))
    except BenchmarkHTTPFailed:
        raise
    except Exception:
        raise BenchmarkHTTPFailed(
            "benchmark.http.response_invalid", status=503,
        ) from None


def _report_payload(value: Any, status: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        report = json.loads(_canonical_json(dict(value)))
        if (
            report.get("schema") != BENCHMARK_REPORT_SCHEMA
            or report.get("status") not in {"PASS", "BLOCK"}
            or not isinstance(report.get("superiority_claim_allowed"), bool)
            or report["superiority_claim_allowed"] != (report["status"] == "PASS")
            or report.get("valid_until") != status["valid_until"]
            or not isinstance(report.get("suite"), dict)
            or report["suite"].get("sha256") != status["suite_sha256"]
            or not isinstance(report.get("attestation"), dict)
        ):
            raise ValueError
        encoded = _canonical_json(report)
        return report, hashlib.sha256(encoded).hexdigest()
    except BenchmarkHTTPFailed:
        raise
    except Exception:
        raise BenchmarkHTTPFailed(
            "benchmark.http.response_invalid", status=503,
        ) from None


class BenchmarkReportHTTPApplication:
    """Strict WSGI reader for one configured benchmark campaign."""

    def __init__(self, runtime: Any, authenticator: Callable[[dict[str, Any]], Any]):
        if any(not callable(getattr(runtime, method, None)) for method in (
            "benchmark_campaign_status", "load_benchmark_report",
        )):
            raise ValueError("benchmark HTTP runtime is incomplete")
        if not callable(authenticator):
            raise ValueError("benchmark HTTP authenticator must be callable")
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
                raise BenchmarkHTTPFailed(
                    "benchmark.http.headers_invalid", status=400,
                )
            headers.append((name, value))
        headers.sort()
        return tuple(headers)

    @staticmethod
    def _require_empty_body(environ: Mapping[str, Any]) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
            raise BenchmarkHTTPFailed(
                "benchmark.http.transfer_encoding", status=400,
            )
        length = environ.get("CONTENT_LENGTH")
        if length not in {None, "", "0"}:
            raise BenchmarkHTTPFailed(
                "benchmark.http.body_not_allowed", status=400,
            )
        return b""

    def _authenticate(
        self,
        method: str,
        path: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
    ) -> BenchmarkReaderPrincipal:
        request = {
            "schema": AUTH_REQUEST_SCHEMA,
            "method": method,
            "path": path,
            "headers": [list(item) for item in headers],
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        try:
            return _principal(self.authenticator(request))
        except BenchmarkHTTPFailed:
            raise
        except Exception:
            raise BenchmarkHTTPFailed(
                "benchmark.http.authentication_unavailable",
                status=503,
                retryable=True,
            ) from None

    @staticmethod
    def _runtime_error(error: Exception) -> BenchmarkHTTPFailed:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            code = "benchmark.http.runtime_unavailable"
        retryable = getattr(error, "retryable", False)
        retryable = retryable if isinstance(retryable, bool) else False
        if code.endswith((
            "report_missing", "report_state_invalid", "incomplete",
            "validity_expired", "policy_expired",
        )):
            status = 409
        elif retryable or code.endswith(("unavailable", "operation_guard_failed")):
            status = 503
        else:
            status = 400
        return BenchmarkHTTPFailed(code, status=status, retryable=retryable)

    @staticmethod
    def _send(start_response: Callable[..., Any], status: int, payload: Any):
        body = b"" if payload is None else _canonical_json(payload)
        phrases = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 409: "Conflict",
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
                raise BenchmarkHTTPFailed(
                    "benchmark.http.https_required", status=400,
                )
            if environ.get("QUERY_STRING") not in {None, ""}:
                raise BenchmarkHTTPFailed(
                    "benchmark.http.query_invalid", status=400,
                )
            if path not in {STATUS_PATH, REPORT_PATH} or method != "GET":
                raise BenchmarkHTTPFailed(
                    "benchmark.http.route_not_found", status=404,
                )
            body = self._require_empty_body(environ)
            principal = self._authenticate(
                method, path, self._headers(environ), body,
            )
            status = _status_payload(self.runtime.benchmark_campaign_status())
            if status["campaign_id"] != principal.campaign_id:
                raise BenchmarkHTTPFailed(
                    "benchmark.http.campaign_forbidden", status=403,
                )
            if path == STATUS_PATH:
                return self._send(start_response, 200, {
                    "schema": STATUS_RESPONSE_SCHEMA,
                    "campaign": status,
                })
            if (
                not status["complete"]
                or status["report_finalization"]["status"] != "succeeded"
            ):
                raise BenchmarkHTTPFailed(
                    "benchmark.http.report_unavailable", status=409,
                )
            report, report_sha256 = _report_payload(
                self.runtime.load_benchmark_report(), status,
            )
            return self._send(start_response, 200, {
                "schema": REPORT_RESPONSE_SCHEMA,
                "campaign_id": status["campaign_id"],
                "report_sha256": report_sha256,
                "report": report,
            })
        except BenchmarkHTTPFailed as error:
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
