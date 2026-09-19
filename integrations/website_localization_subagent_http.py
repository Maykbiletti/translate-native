#!/usr/bin/env python3
"""Authenticated HTTPS bridge for host-isolated website review agents.

The remote service is a trusted host boundary, not an LLM provider endpoint.
It must execute the supplied task in a fresh context and construct its receipt
from host execution facts. This client makes one bounded request; durable queue
retries and the host's execution-key ledger provide crash-safe idempotency.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


REQUEST_SCHEMA = "translate-native.host-subagent-http-request.v1"
RESPONSE_SCHEMA = "translate-native.host-subagent-http-response.v1"
ATTESTATION_SCHEMA = "translate-native.host-subagent-http-attestation.v1"
ATTESTATION_PAYLOAD_SCHEMA = "translate-native.host-subagent-http-attestation-payload.v1"
EVIDENCE_SCHEMA = "translate-native.host-subagent-http-evidence.v1"
REVIEW_SCHEMA = "translate-native.host-subagent-review.v1"
RESPONSE_REVIEW_SCHEMA = "translate-native.response-subagent-review.v1"
MAX_ENDPOINT_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 4096
MAX_REQUEST_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 4_500_000
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,117}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE = re.compile(r"^[A-Za-z0-9+/=_:.-]{16,16384}$")
RESERVED_HEADERS = {
    "accept", "accept-encoding", "connection", "content-length", "content-type",
    "host", "idempotency-key", "transfer-encoding", "x-subagent-execution-key",
    "x-subagent-phase", "x-subagent-request-sha256",
}
CONTROL_FIELDS = {
    "schema", "creator_id", "creator_session_id", "model_id", "model_version",
    "host_policy_version", "native_brief", "timeout_seconds", "max_output_tokens",
    "inherit_context", "tools", "max_delegation_depth", "provider_id",
    "execution_key", "request_sha256", "task_sha256", "phase",
    "previous_receipt_sha256",
}
RESPONSE_CONTROL_FIELDS = (
    CONTROL_FIELDS - {"creator_id", "creator_session_id"}
) | {
    "creator_id_sha256", "creator_session_id_sha256", "reviewer_role",
    "assignment_id",
}
NATIVE_INPUT_FIELDS = {
    "candidate", "target", "content_type", "quality_profile", "response_schema",
    "audience", "tone_profile", "target_terms",
}


class HTTPReviewHostFailed(RuntimeError):
    """Content-free failure consumed by ``HostSubagentProvider``."""

    host_subagent_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("host error code is invalid")
        if type(retryable) is not bool:
            raise ValueError("host retryability must be boolean")
        self.code, self.retryable = code, retryable
        super().__init__(code)


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, *,
             timeout: float) -> HTTPResult: ...


class AttestationVerifier(Protocol):
    def verify(self, payload: bytes, attestation: Mapping[str, str]) -> bool: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class URLTransport:
    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def post(self, url: str, headers: Mapping[str, str], body: bytes, *,
             timeout: float) -> HTTPResult:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return HTTPResult(int(error.code), tuple(error.headers.items()) if error.headers else (), b"")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise HTTPReviewHostFailed("http.network", retryable=True) from None
        try:
            return HTTPResult(int(response.status), tuple(response.headers.items()),
                              response.read(MAX_RESPONSE_BYTES + 1))
        except (TimeoutError, socket.timeout, OSError):
            raise HTTPReviewHostFailed("http.network", retryable=True) from None
        finally:
            response.close()


def _raw(value: Any, *, code: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise HTTPReviewHostFailed(code, retryable=False) from None
    if not encoded or len(encoded) > maximum:
        raise HTTPReviewHostFailed(code, retryable=False)
    return encoded


def _copy(value: Any) -> Any:
    return json.loads(_raw(value, code="http.payload_invalid", maximum=MAX_REQUEST_BYTES))


def _sha(value: Any) -> str:
    return hashlib.sha256(_raw(value, code="http.payload_invalid",
                               maximum=MAX_REQUEST_BYTES)).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (not isinstance(value, str) or value != value.strip() or not value
            or not value.isascii() or len(value) > MAX_ENDPOINT_LENGTH
            or any(ord(character) <= 32 or ord(character) == 127 for character in value)):
        raise ValueError("endpoint is invalid")
    try:
        parsed, port = urllib.parse.urlsplit(value), urllib.parse.urlsplit(value).port
    except ValueError:
        raise ValueError("endpoint is invalid") from None
    hostname = parsed.hostname
    if (not hostname or not hostname.isascii() or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.path.startswith("/") or parsed.path.startswith("//")):
        raise ValueError("endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif not (parsed.scheme == "http" and allow_loopback_http
              and hostname.lower() in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint is invalid")
    return value


def _authentication_headers(provider: Callable[[], Mapping[str, str]]) -> dict[str, str]:
    try:
        supplied = provider()
    except Exception:
        raise HTTPReviewHostFailed("http.authentication", retryable=False) from None
    if not isinstance(supplied, Mapping) or not supplied:
        raise HTTPReviewHostFailed("http.authentication", retryable=False)
    result, normalized_names = {}, set()
    for name, value in supplied.items():
        normalized = name.lower() if isinstance(name, str) else ""
        if (not isinstance(name, str) or HEADER_NAME.fullmatch(name) is None
                or normalized in RESERVED_HEADERS or normalized in normalized_names
                or not isinstance(value, str) or not value or not value.isascii()
                or len(value) > MAX_HEADER_VALUE_LENGTH or "\r" in value or "\n" in value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise HTTPReviewHostFailed("http.authentication", retryable=False)
        normalized_names.add(normalized)
        result[name] = value
    return result


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise HTTPReviewHostFailed("http.transport_invalid", retryable=True)
    result = {}
    for item in value:
        if (not isinstance(item, tuple) or len(item) != 2
                or not all(isinstance(part, str) for part in item)):
            raise HTTPReviewHostFailed("http.transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                raise HTTPReviewHostFailed("http.response_headers", retryable=False)
            result[name] = content
    return result


def _validate_task(task: Any, control: Any) -> tuple[dict, dict]:
    task, control = _copy(task), _validate_control(control)
    if (not isinstance(task, dict) or set(task) != {"schema", "phase", "system_instruction", "input"}
            or task.get("schema") not in {REVIEW_SCHEMA, RESPONSE_REVIEW_SCHEMA}
            or task.get("phase") not in {"target_native", "source_fidelity"}
            or (task.get("schema") == RESPONSE_REVIEW_SCHEMA
                and task.get("phase") != "target_native")
            or not isinstance(task.get("system_instruction"), str) or not task["system_instruction"]
            or not isinstance(task.get("input"), dict)
            or control.get("schema") != task["schema"] or control.get("phase") != task["phase"]
            or control["task_sha256"] != _sha(task)):
        raise HTTPReviewHostFailed("http.request_invalid", retryable=False)
    expected_inputs = set(NATIVE_INPUT_FIELDS)
    if "commercial_quality_profile" in task["input"]:
        expected_inputs.add("commercial_quality_profile")
    if task["phase"] == "target_native" and set(task["input"]) != expected_inputs:
        raise HTTPReviewHostFailed("http.source_isolation", retryable=False)
    return task, control


def _validate_control(control: Any) -> dict:
    control = _copy(control)
    schema = control.get("schema") if isinstance(control, dict) else None
    expected_fields = RESPONSE_CONTROL_FIELDS if schema == RESPONSE_REVIEW_SCHEMA else CONTROL_FIELDS
    if (not isinstance(control, dict) or set(control) != expected_fields
            or schema not in {REVIEW_SCHEMA, RESPONSE_REVIEW_SCHEMA}
            or control.get("phase") not in {"target_native", "source_fidelity"}
            or (schema == RESPONSE_REVIEW_SCHEMA
                and (control.get("phase") != "target_native"
                     or control.get("reviewer_role") != "target-native-reviewer"
                     or not isinstance(control.get("assignment_id"), str)
                     or SHA256.fullmatch(control["assignment_id"]) is None))
            or control.get("inherit_context") is not False or control.get("tools") != []
            or type(control.get("max_delegation_depth")) is not int
            or control["max_delegation_depth"] != 0
            or type(control.get("timeout_seconds")) is not int
            or not 1 <= control["timeout_seconds"] <= 300
            or type(control.get("max_output_tokens")) is not int
            or not 128 <= control["max_output_tokens"] <= 32768
            or any(not isinstance(control.get(key), str) or TOKEN.fullmatch(control[key]) is None
                   for key in ("model_id", "model_version", "host_policy_version", "provider_id"))
            or (schema == RESPONSE_REVIEW_SCHEMA and any(
                not isinstance(control.get(key), str)
                or SHA256.fullmatch(control[key]) is None
                for key in ("creator_id_sha256", "creator_session_id_sha256")
            ))
            or (schema == REVIEW_SCHEMA and any(
                not isinstance(control.get(key), str)
                or TOKEN.fullmatch(control[key]) is None
                for key in ("creator_id", "creator_session_id")
            ))
            or not isinstance(control.get("execution_key"), str)
            or SHA256.fullmatch(control["execution_key"]) is None
            or any(not isinstance(control.get(key), str) or SHA256.fullmatch(control[key]) is None
                   for key in ("request_sha256", "task_sha256"))
            or (control["previous_receipt_sha256"] is not None
                and (not isinstance(control["previous_receipt_sha256"], str)
                     or SHA256.fullmatch(control["previous_receipt_sha256"]) is None))):
        raise HTTPReviewHostFailed("http.request_invalid", retryable=False)
    return control


class HTTPSReviewHost:
    """One-request authenticated bridge implementing the ``ReviewHost`` protocol."""

    def __init__(self, endpoint: str, authentication_headers: Callable[[], Mapping[str, str]],
                 attestation_verifier: AttestationVerifier, *, host_id: str,
                 transport: HTTPTransport | None = None, timeout: float = 60.0,
                 allow_loopback_http: bool = False):
        if type(allow_loopback_http) is not bool:
            raise TypeError("allow_loopback_http must be boolean")
        self.endpoint = _endpoint(endpoint, allow_loopback_http)
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if not callable(getattr(attestation_verifier, "verify", None)):
            raise TypeError("attestation_verifier must provide verify")
        if not isinstance(host_id, str) or TOKEN.fullmatch(host_id) is None:
            raise ValueError("host_id is invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("timeout is outside the supported range")
        self.authentication_headers = authentication_headers
        self.attestation_verifier = attestation_verifier
        self.host_id = host_id
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.timeout = float(timeout)
        self._verified: dict[str, dict[str, Any]] = {}
        self._verified_lock = threading.RLock()

    def run_isolated(self, task: dict, *, control: dict) -> Mapping[str, Any]:
        task, control = _validate_task(task, control)
        unsigned = {"schema": REQUEST_SCHEMA, "host_id": self.host_id,
                    "execution_key": control["execution_key"], "task": task, "control": control}
        request_sha256 = _sha(unsigned)
        envelope = {**unsigned, "request_sha256": request_sha256}
        body = _raw(envelope, code="http.request_invalid", maximum=MAX_REQUEST_BYTES)
        headers = _authentication_headers(self.authentication_headers)
        headers.update({
            "Accept": "application/json", "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": control["execution_key"],
            "X-Subagent-Execution-Key": control["execution_key"],
            "X-Subagent-Phase": control["phase"],
            "X-Subagent-Request-Sha256": request_sha256,
        })
        try:
            result = self.transport.post(self.endpoint, headers, body,
                                         timeout=min(self.timeout, float(control["timeout_seconds"])))
        except HTTPReviewHostFailed:
            raise
        except Exception:
            raise HTTPReviewHostFailed("http.network", retryable=True) from None
        if _raw(envelope, code="http.request_mutated", maximum=MAX_REQUEST_BYTES) != body:
            raise HTTPReviewHostFailed("http.request_mutated", retryable=False)
        if (not isinstance(result, HTTPResult) or type(result.status) is not int
                or not 100 <= result.status <= 599):
            raise HTTPReviewHostFailed("http.transport_invalid", retryable=True)
        if result.status != 200:
            if 300 <= result.status <= 399:
                code, retryable = "http.redirect", False
            elif result.status in {401, 403}:
                code, retryable = "http.authentication_rejected", False
            elif result.status == 409:
                code, retryable = "http.idempotency_conflict", False
            else:
                code = "http.status"
                retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            raise HTTPReviewHostFailed(code, retryable=retryable)
        response_headers = _headers(result.headers)
        if response_headers.get("content-type", "").lower().replace(" ", "") not in {
                "application/json", "application/json;charset=utf-8"}:
            raise HTTPReviewHostFailed("http.response_content_type", retryable=False)
        if not isinstance(result.body, bytes) or not result.body or len(result.body) > MAX_RESPONSE_BYTES:
            raise HTTPReviewHostFailed("http.response_size", retryable=False)
        declared = response_headers.get("content-length")
        if declared is not None and (not declared.isascii() or not declared.isdecimal()
                                     or int(declared) != len(result.body)):
            raise HTTPReviewHostFailed("http.response_size", retryable=False)
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError
            reply = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise HTTPReviewHostFailed("http.response_json", retryable=False) from None
        if (not isinstance(reply, dict) or set(reply) != {"schema", "host_id", "execution_key",
                                                          "request_sha256", "result", "attestation"}
                or reply.get("schema") != RESPONSE_SCHEMA or reply.get("host_id") != self.host_id
                or reply.get("execution_key") != control["execution_key"]
                or reply.get("request_sha256") != request_sha256
                or not isinstance(reply.get("result"), dict)
                or set(reply["result"]) != {"response", "receipt"}):
            raise HTTPReviewHostFailed("http.response_binding", retryable=False)
        attestation = reply["attestation"]
        if (not isinstance(attestation, dict)
                or set(attestation) != {"schema", "algorithm", "key_id", "signature"}
                or attestation.get("schema") != ATTESTATION_SCHEMA
                or any(not isinstance(attestation.get(key), str) or TOKEN.fullmatch(attestation[key]) is None
                       for key in ("algorithm", "key_id"))
                or not isinstance(attestation.get("signature"), str)
                or SIGNATURE.fullmatch(attestation["signature"]) is None):
            raise HTTPReviewHostFailed("http.attestation_invalid", retryable=False)
        signed = {"schema": ATTESTATION_PAYLOAD_SCHEMA, "host_id": self.host_id,
                  "execution_key": control["execution_key"], "request_sha256": request_sha256,
                  "result_sha256": _sha(reply["result"]), "completed": True}
        signed_bytes = _raw(signed, code="http.attestation_invalid", maximum=MAX_RESPONSE_BYTES)
        try:
            accepted = self.attestation_verifier.verify(signed_bytes, _copy(attestation))
        except Exception:
            raise HTTPReviewHostFailed("http.attestation_unavailable", retryable=True) from None
        if accepted is not True:
            raise HTTPReviewHostFailed("http.attestation_rejected", retryable=False)
        receipt_copy = _copy(reply["result"]["receipt"])
        verified = {
            "control_sha256": _sha(control), "receipt": receipt_copy,
            "receipt_sha256": _sha(receipt_copy),
            "signed": signed, "attestation": _copy(attestation),
        }
        with self._verified_lock:
            previous = self._verified.get(control["execution_key"])
            if previous is not None:
                stable = {"control_sha256", "receipt_sha256", "signed"}
                if any(previous[key] != verified[key] for key in stable):
                    raise HTTPReviewHostFailed("http.idempotency_conflict", retryable=False)
            self._verified[control["execution_key"]] = verified
        return _copy(reply["result"])

    def verify_execution(self, receipt: dict, *, control: dict) -> bool:
        try:
            control, receipt = _validate_control(control), _copy(receipt)
        except Exception:
            return False
        with self._verified_lock:
            saved = (self._verified.get(control.get("execution_key"))
                     if isinstance(control, dict) else None)
        if (saved is None or saved["control_sha256"] != _sha(control)
                or saved["receipt_sha256"] != _sha(receipt)):
            return False
        signed_bytes = _raw(saved["signed"], code="http.attestation_invalid",
                            maximum=MAX_RESPONSE_BYTES)
        try:
            return self.attestation_verifier.verify(
                signed_bytes, _copy(saved["attestation"]),
            ) is True
        except Exception:
            raise HTTPReviewHostFailed("http.attestation_unavailable", retryable=True) from None

    def verified_execution_evidence(self, receipt: dict, *, control: dict) -> dict:
        """Return the exact attested snapshot committed by the worker result."""
        try:
            control, receipt = _validate_control(control), _copy(receipt)
        except Exception:
            raise HTTPReviewHostFailed("http.evidence_invalid", retryable=False) from None
        with self._verified_lock:
            saved = self._verified.get(control["execution_key"])
        if (saved is None or saved["control_sha256"] != _sha(control)
                or saved["receipt_sha256"] != _sha(receipt)):
            raise HTTPReviewHostFailed("http.evidence_missing", retryable=False)
        if self.verify_execution(receipt, control=control) is not True:
            raise HTTPReviewHostFailed("http.attestation_rejected", retryable=False)
        return {
            "schema": EVIDENCE_SCHEMA,
            "host_id": saved["signed"]["host_id"],
            "execution_key": saved["signed"]["execution_key"],
            "request_sha256": saved["signed"]["request_sha256"],
            "result_sha256": saved["signed"]["result_sha256"],
            "receipt": _copy(saved["receipt"]),
            "attestation": _copy(saved["attestation"]),
        }
