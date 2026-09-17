#!/usr/bin/env python3
"""Provider-neutral HTTPS backend for a host subagent facility.

This module is a digest-pinnable factory for the durable executor runtime.  It
performs exactly one authenticated HTTP exchange per execute or reconcile call,
never retries a physical start, and accepts only responses bound to the exact
host-owned execution identity and upstream execute digest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


SETTINGS_SCHEMA = "translate-native.subagent-review-http-backend.v1"
REQUEST_SCHEMA = "translate-native.subagent-review-facility-request.v1"
RESPONSE_SCHEMA = "translate-native.subagent-review-facility-response.v1"
WORKER_SCHEMA = "translate-native.subagent-review-facility-http-worker.v1"
MAX_ENDPOINT_LENGTH = 2048
MAX_REQUEST_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 4_500_000
MAX_WORKER_BYTES = 6_500_000
MAX_SECRET_BYTES = 16_384
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BEARER = re.compile(r"^[A-Za-z0-9._~+/=-]{32,2048}$")
STATUSES = {"completed", "not_started", "running", "unknown", "cancel_pending"}
ACTIVE = {"running", "unknown", "cancel_pending"}
ASSIGNMENT_FIELDS = {
    "execution_key", "route_id", "phase", "reviewer_role",
    "reviewer_agent_id", "reviewer_session_id", "model_id",
    "model_version", "host_policy_version", "deadline_seconds",
    "max_output_tokens", "max_input_bytes", "cost_unit", "max_cost_units",
}
BUDGET_FIELDS = {
    "deadline_seconds", "max_output_tokens", "max_input_bytes", "cost_unit",
    "max_cost_units", "max_concurrent_executions",
}
NATIVE_FORBIDDEN_KEYS = {
    "source", "source_text", "source_locale", "source_language", "messages",
    "history", "creator", "creator_id", "creator_session", "tools", "tool",
    "credentials", "credential", "secret", "signature", "signing_key",
    "publication", "publish", "previous_receipt", "previous_receipt_sha256",
    "inherited_context", "conversation", "conversation_history",
}


class SubagentHTTPBackendFailed(RuntimeError):
    """Content-free backend failure understood by the durable executor."""

    def __init__(self, code: str, *, retryable: bool):
        if (not isinstance(code, str)
                or re.fullmatch(r"backend_http\.[a-z0-9_.-]{1,100}", code) is None
                or type(retryable) is not bool):
            raise ValueError("backend HTTP failure is invalid")
        self.code, self.retryable = code, retryable
        super().__init__(code)


def _failed(code: str, *, retryable: bool = False):
    return SubagentHTTPBackendFailed("backend_http." + code, retryable=retryable)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _raw(value: Any, *, maximum: int = MAX_REQUEST_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _failed("payload_invalid") from None
    if not encoded or len(encoded) > maximum:
        raise _failed("payload_invalid")
    return encoded


def _copy(value: Any) -> Any:
    return json.loads(_raw(value))


def _sha(value: Any) -> str:
    return hashlib.sha256(_raw(value)).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (not isinstance(value, str) or value != value.strip() or not value
            or not value.isascii() or len(value) > MAX_ENDPOINT_LENGTH
            or any(ord(character) <= 32 or ord(character) == 127
                   for character in value)):
        raise ValueError("backend endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("backend endpoint is invalid") from None
    hostname = parsed.hostname
    if (not hostname or not hostname.isascii() or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.path.startswith("/") or parsed.path.startswith("//")):
        raise ValueError("backend endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif not (parsed.scheme == "http" and allow_loopback_http
              and hostname.lower() in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("backend endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("backend endpoint is invalid")
    return value


def _assignment(value: Any) -> dict:
    if not isinstance(value, Mapping) or set(value) != ASSIGNMENT_FIELDS:
        raise _failed("assignment_invalid")
    result = _copy(value)
    strings = {
        "route_id", "phase", "reviewer_role", "reviewer_agent_id",
        "reviewer_session_id", "model_id", "model_version",
        "host_policy_version", "cost_unit",
    }
    if (not isinstance(result.get("execution_key"), str)
            or SHA256.fullmatch(result["execution_key"]) is None
            or result.get("phase") not in {"target_native", "source_fidelity"}
            or any(not isinstance(result.get(name), str)
                   or TOKEN.fullmatch(result[name]) is None for name in strings)
            or type(result.get("deadline_seconds")) is not int
            or not 1 <= result["deadline_seconds"] <= 300
            or type(result.get("max_output_tokens")) is not int
            or not 128 <= result["max_output_tokens"] <= 32768
            or type(result.get("max_input_bytes")) is not int
            or not 1024 <= result["max_input_bytes"] <= MAX_REQUEST_BYTES
            or type(result.get("max_cost_units")) is not int
            or not 1 <= result["max_cost_units"] <= 1_000_000_000):
        raise _failed("assignment_invalid")
    expected_role = ("target-native-reviewer"
                     if result["phase"] == "target_native"
                     else "source-fidelity-reviewer")
    if result["reviewer_role"] != expected_role:
        raise _failed("assignment_invalid")
    return result


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _failed("model_input_invalid")
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _model_input(value: Any, assignment: Mapping[str, Any]) -> dict:
    copied = _copy(value)
    if (not isinstance(copied, dict)
            or set(copied) != {"schema", "phase", "system_instruction", "input"}
            or copied.get("phase") != assignment["phase"]
            or not isinstance(copied.get("schema"), str)
            or TOKEN.fullmatch(copied["schema"]) is None
            or not isinstance(copied.get("system_instruction"), str)
            or not copied["system_instruction"].strip()
            or not isinstance(copied.get("input"), dict)
            or not isinstance(copied["input"].get("candidate"), str)
            or not copied["input"]["candidate"]):
        raise _failed("model_input_invalid")
    if assignment["phase"] == "target_native":
        for key in _walk_keys(copied["input"]):
            if key.casefold().replace("-", "_") in NATIVE_FORBIDDEN_KEYS:
                raise _failed("native_source_isolation")
    elif "source" not in copied["input"]:
        raise _failed("model_input_invalid")
    return copied


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, *,
             timeout: float) -> HTTPResult: ...


_TRANSPORT_WORKER_CODE = r'''
import base64,json,socket,sys,urllib.error,urllib.request
SCHEMA="translate-native.subagent-review-facility-http-worker.v1"
MAX_REQUEST=4500000
MAX_RESPONSE=4500000
MAX_WORKER=6500000
def pairs(items):
    result={}
    for key,value in items:
        if key in result: raise ValueError()
        result[key]=value
    return result
def constant(_value): raise ValueError()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl): return None
def emit(value):
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(",",":")).encode("utf-8")
    if not raw or len(raw)>MAX_WORKER: raise ValueError()
    sys.stdout.buffer.write(raw);sys.stdout.buffer.flush()
try:
    raw=sys.stdin.buffer.read(MAX_WORKER+1)
    if not raw or len(raw)>MAX_WORKER: raise ValueError()
    request=json.loads(raw.decode("utf-8"),object_pairs_hook=pairs,parse_constant=constant)
    if (not isinstance(request,dict) or set(request)!={"schema","url","headers","body","timeout"}
        or request.get("schema")!=SCHEMA or not isinstance(request.get("url"),str)
        or not isinstance(request.get("headers"),dict)
        or not all(isinstance(k,str) and isinstance(v,str) for k,v in request["headers"].items())
        or not isinstance(request.get("body"),str)
        or not isinstance(request.get("timeout"),(int,float)) or isinstance(request.get("timeout"),bool)
        or not 0<request["timeout"]<=300): raise ValueError()
    body=base64.b64decode(request["body"],validate=True)
    if not body or len(body)>MAX_REQUEST: raise ValueError()
    http_request=urllib.request.Request(request["url"],data=body,headers=request["headers"],method="POST")
    try:
        response=urllib.request.build_opener(NoRedirect).open(http_request,timeout=float(request["timeout"]))
    except urllib.error.HTTPError as error:
        response_body=error.read(MAX_RESPONSE+1)
        emit({"schema":SCHEMA,"ok":True,"status":int(error.code),"headers":[list(x) for x in error.headers.items()] if error.headers else [],"body":base64.b64encode(response_body).decode("ascii")})
    else:
        try:
            response_body=response.read(MAX_RESPONSE+1)
            emit({"schema":SCHEMA,"ok":True,"status":int(response.status),"headers":[list(x) for x in response.headers.items()],"body":base64.b64encode(response_body).decode("ascii")})
        finally: response.close()
except (urllib.error.URLError,TimeoutError,socket.timeout,OSError):
    emit({"schema":SCHEMA,"ok":False,"code":"backend_http.network","retryable":True})
except Exception:
    emit({"schema":SCHEMA,"ok":False,"code":"backend_http.transport_invalid","retryable":True})
'''


class URLTransport:
    """One request in an isolated child with a parent-enforced wall timeout."""

    def post(self, url: str, headers: Mapping[str, str], body: bytes, *,
             timeout: float) -> HTTPResult:
        worker_request = {
            "schema": WORKER_SCHEMA, "url": url, "headers": dict(headers),
            "body": base64.b64encode(body).decode("ascii"), "timeout": timeout,
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", _TRANSPORT_WORKER_CODE],
                input=_raw(worker_request, maximum=MAX_WORKER_BYTES),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=max(0.001, float(timeout)), check=False, env={},
                start_new_session=os.name != "nt",
            )
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise _failed("network", retryable=True) from None
        if completed.returncode != 0 or len(completed.stdout) > MAX_WORKER_BYTES:
            raise _failed("transport_invalid", retryable=True)
        try:
            reply = json.loads(
                completed.stdout.decode("utf-8"), object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise _failed("transport_invalid", retryable=True) from None
        if (not isinstance(reply, dict) or reply.get("schema") != WORKER_SCHEMA
                or type(reply.get("ok")) is not bool):
            raise _failed("transport_invalid", retryable=True)
        if reply["ok"] is False:
            if set(reply) != {"schema", "ok", "code", "retryable"}:
                raise _failed("transport_invalid", retryable=True)
            raise SubagentHTTPBackendFailed(
                reply["code"], retryable=reply["retryable"],
            )
        if (set(reply) != {"schema", "ok", "status", "headers", "body"}
                or type(reply.get("status")) is not int
                or not isinstance(reply.get("headers"), list)
                or any(not isinstance(pair, list) or len(pair) != 2
                       or not all(isinstance(item, str) for item in pair)
                       for pair in reply["headers"])
                or not isinstance(reply.get("body"), str)):
            raise _failed("transport_invalid", retryable=True)
        try:
            response_body = base64.b64decode(reply["body"], validate=True)
        except (ValueError, TypeError):
            raise _failed("transport_invalid", retryable=True) from None
        return HTTPResult(
            reply["status"], tuple(tuple(pair) for pair in reply["headers"]),
            response_body,
        )


def _response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise _failed("transport_invalid", retryable=True)
    result = {}
    for item in value:
        if (not isinstance(item, tuple) or len(item) != 2
                or not all(isinstance(part, str) for part in item)):
            raise _failed("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                raise _failed("response_headers")
            result[name] = content
    return result


class HTTPSExecutionBackend:
    """Exact one-shot client for a remote idempotent subagent facility."""

    def __init__(self, endpoint: str, authentication_headers: Callable[[], Mapping[str, str]],
                 *, backend_id: str, backend_version: str, facility_id: str,
                 facility_version: str, transport: HTTPTransport | None = None,
                 request_timeout_seconds: int = 60,
                 max_input_bytes: int = 2_000_000,
                 max_output_tokens: int = 4096,
                 cost_unit: str = "deployment-cost-unit",
                 max_cost_units: int = 100_000,
                 allow_loopback_http: bool = False):
        if type(allow_loopback_http) is not bool:
            raise TypeError("allow_loopback_http must be boolean")
        self.endpoint = _endpoint(endpoint, allow_loopback_http)
        self.backend_id = _identifier(backend_id, "backend_id")
        self.backend_version = _identifier(backend_version, "backend_version")
        self.facility_id = _identifier(facility_id, "facility_id")
        self.facility_version = _identifier(facility_version, "facility_version")
        self.cost_unit = _identifier(cost_unit, "cost_unit")
        if (not callable(authentication_headers)
                or type(request_timeout_seconds) is not int
                or not 1 <= request_timeout_seconds <= 300
                or type(max_input_bytes) is not int
                or not 1024 <= max_input_bytes <= MAX_REQUEST_BYTES
                or type(max_output_tokens) is not int
                or not 128 <= max_output_tokens <= 32768
                or type(max_cost_units) is not int
                or not 1 <= max_cost_units <= 1_000_000_000):
            raise ValueError("backend configuration is invalid")
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.request_timeout_seconds = request_timeout_seconds
        self.max_input_bytes = max_input_bytes
        self.max_output_tokens = max_output_tokens
        self.max_cost_units = max_cost_units

    def _auth(self) -> dict[str, str]:
        try:
            supplied = self.authentication_headers()
        except Exception:
            raise _failed("authentication") from None
        if (not isinstance(supplied, Mapping)
                or set(name.lower() for name in supplied) != {"authorization"}):
            raise _failed("authentication")
        name, value = next(iter(supplied.items()))
        if (not isinstance(name, str) or not isinstance(value, str)
                or not value.startswith("Bearer ")
                or BEARER.fullmatch(value[7:]) is None
                or "\r" in value or "\n" in value):
            raise _failed("authentication")
        return {name: value}

    def _budgets(self, value: Any, assignment: Mapping[str, Any]) -> dict:
        if not isinstance(value, Mapping) or set(value) != BUDGET_FIELDS:
            raise _failed("budget_invalid")
        budgets = _copy(value)
        if (any(budgets.get(name) != assignment[name] for name in (
                "deadline_seconds", "max_output_tokens", "max_input_bytes",
                "cost_unit", "max_cost_units"))
                or assignment["max_input_bytes"] > self.max_input_bytes
                or assignment["max_output_tokens"] > self.max_output_tokens
                or assignment["max_cost_units"] > self.max_cost_units
                or assignment["cost_unit"] != self.cost_unit
                or type(budgets.get("max_concurrent_executions")) is not int
                or not 1 <= budgets["max_concurrent_executions"] <= 256):
            raise _failed("budget_invalid")
        return budgets

    def _request(self, operation: str, assignment: Mapping[str, Any], *,
                 execute_request_sha256: str,
                 model_input: Mapping[str, Any] | None = None,
                 budgets: Mapping[str, Any] | None = None) -> tuple[dict, bytes, dict]:
        assigned = _assignment(assignment)
        if (not isinstance(execute_request_sha256, str)
                or SHA256.fullmatch(execute_request_sha256) is None):
            raise _failed("request_invalid")
        unsigned = {
            "schema": REQUEST_SCHEMA, "operation": operation,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "facility_id": self.facility_id,
            "facility_version": self.facility_version,
            "assignment": assigned,
            "execute_request_sha256": execute_request_sha256,
            "isolation": {
                "inherit_context": False, "tools": [],
                "max_delegation_depth": 0,
            },
        }
        if operation == "execute":
            if model_input is None or budgets is None:
                raise _failed("request_invalid")
            copied_input = _model_input(model_input, assigned)
            bound_budgets = self._budgets(budgets, assigned)
            if len(_raw(copied_input)) > assigned["max_input_bytes"]:
                raise _failed("input_budget")
            unsigned.update(model_input=copied_input, budgets=bound_budgets)
        elif operation == "reconcile":
            if model_input is not None or budgets is not None:
                raise _failed("request_invalid")
        else:
            raise _failed("request_invalid")
        request_sha256 = _sha(unsigned)
        envelope = {**unsigned, "request_sha256": request_sha256}
        body = _raw(envelope)
        headers = self._auth()
        headers.update({
            "Accept": "application/json", "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": assigned["execution_key"],
            "X-Subagent-Facility-Operation": operation,
            "X-Subagent-Execution-Key": assigned["execution_key"],
            "X-Subagent-Request-SHA256": request_sha256,
            "X-Upstream-Execute-Request-SHA256": execute_request_sha256,
        })
        return envelope, body, headers

    def _parse(self, result: Any, request: Mapping[str, Any]) -> dict:
        if (not isinstance(result, HTTPResult) or type(result.status) is not int
                or not isinstance(result.body, bytes)
                or len(result.body) > MAX_RESPONSE_BYTES):
            raise _failed("transport_invalid", retryable=True)
        if result.status not in {200, 202}:
            retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            if result.status in {301, 302, 303, 307, 308}:
                raise _failed("redirect")
            if result.status in {401, 403}:
                raise _failed("authentication_rejected")
            if result.status == 409:
                raise _failed("idempotency_conflict")
            raise _failed("status", retryable=retryable)
        headers = _response_headers(result.headers)
        if headers.get("content-type", "").lower().replace(" ", "") \
                != "application/json;charset=utf-8":
            raise _failed("response_headers")
        if ("content-length" not in headers
                or not headers["content-length"].isascii()
                or not headers["content-length"].isdecimal()
                or int(headers["content-length"]) != len(result.body)):
            raise _failed("response_headers")
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError
            reply = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise _failed("response_invalid") from None
        expected = {
            "schema", "operation", "backend_id", "backend_version",
            "facility_id", "facility_version", "execution_key",
            "execute_request_sha256", "request_sha256", "status",
            "execution", "usage",
        }
        assignment = request["assignment"]
        if (not isinstance(reply, dict) or set(reply) != expected
                or reply.get("schema") != RESPONSE_SCHEMA
                or reply.get("operation") != request["operation"]
                or reply.get("backend_id") != self.backend_id
                or reply.get("backend_version") != self.backend_version
                or reply.get("facility_id") != self.facility_id
                or reply.get("facility_version") != self.facility_version
                or reply.get("execution_key") != assignment["execution_key"]
                or reply.get("execute_request_sha256")
                != request["execute_request_sha256"]
                or reply.get("request_sha256") != request["request_sha256"]
                or reply.get("status") not in STATUSES
                or (reply["status"] in ACTIVE) != (result.status == 202)
                or (reply["status"] in {"completed", "not_started"})
                != (result.status == 200)
                or request["operation"] == "execute"
                and reply["status"] == "not_started"):
            raise _failed("response_binding")
        completed = reply["status"] == "completed"
        if (completed != isinstance(reply.get("execution"), dict)
                or completed != isinstance(reply.get("usage"), dict)):
            raise _failed("response_invalid")
        if completed:
            execution, usage = reply["execution"], reply["usage"]
            if (set(execution) != {
                    "response", "execution_key", "phase", "reviewer_role",
                    "agent_id", "session_id", "model_id", "model_version",
                    "inherit_context", "tools", "max_delegation_depth",
            } or not isinstance(execution.get("response"), dict)
                    or execution.get("execution_key") != assignment["execution_key"]
                    or execution.get("phase") != assignment["phase"]
                    or execution.get("reviewer_role") != assignment["reviewer_role"]
                    or execution.get("agent_id") != assignment["reviewer_agent_id"]
                    or execution.get("session_id") != assignment["reviewer_session_id"]
                    or execution.get("model_id") != assignment["model_id"]
                    or execution.get("model_version") != assignment["model_version"]
                    or execution.get("inherit_context") is not False
                    or execution.get("tools") != []
                    or execution.get("max_delegation_depth") != 0):
                raise _failed("execution_invalid")
            if (set(usage) != {
                    "execute_request_sha256", "cost_unit", "cost_units",
                    "input_bytes", "output_tokens",
            } or usage.get("execute_request_sha256")
                    != request["execute_request_sha256"]
                    or usage.get("cost_unit") != assignment["cost_unit"]
                    or type(usage.get("cost_units")) is not int
                    or not 0 <= usage["cost_units"] <= assignment["max_cost_units"]
                    or type(usage.get("input_bytes")) is not int
                    or not 0 <= usage["input_bytes"] <= assignment["max_input_bytes"]
                    or type(usage.get("output_tokens")) is not int
                    or not 0 <= usage["output_tokens"]
                    <= assignment["max_output_tokens"]):
                raise _failed("usage_invalid")
        return {
            "status": reply["status"],
            "execution": _copy(reply["execution"]) if completed else None,
            "usage": _copy(reply["usage"]) if completed else None,
        }

    def _post(self, operation: str, assignment: Mapping[str, Any], *,
              execute_request_sha256: str,
              model_input: Mapping[str, Any] | None = None,
              budgets: Mapping[str, Any] | None = None) -> dict:
        request, body, headers = self._request(
            operation, assignment,
            execute_request_sha256=execute_request_sha256,
            model_input=model_input, budgets=budgets,
        )
        timeout = min(
            self.request_timeout_seconds,
            request["assignment"]["deadline_seconds"],
        )
        try:
            result = self.transport.post(
                self.endpoint, headers, body, timeout=float(timeout),
            )
        except SubagentHTTPBackendFailed:
            raise
        except Exception:
            raise _failed("network", retryable=True) from None
        return self._parse(result, request)

    def execute_idempotent(self, assignment: Mapping[str, Any],
                           model_input: Mapping[str, Any], *,
                           execute_request_sha256: str,
                           budgets: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._post(
            "execute", assignment,
            execute_request_sha256=execute_request_sha256,
            model_input=model_input, budgets=budgets,
        )

    def reconcile(self, assignment: Mapping[str, Any], *,
                  execute_request_sha256: str) -> Mapping[str, Any]:
        return self._post(
            "reconcile", assignment,
            execute_request_sha256=execute_request_sha256,
        )

    def close(self):
        closer = getattr(self.transport, "close", None)
        if callable(closer):
            closer()


def _protected_token(path_value: Any, expected_sha256: str) -> str:
    if (not isinstance(path_value, str) or len(path_value) > 4096
            or not Path(path_value).is_absolute()
            or not isinstance(expected_sha256, str)
            or SHA256.fullmatch(expected_sha256) is None):
        raise ValueError("facility token configuration is invalid")
    path, parent = Path(path_value), Path(path_value).parent
    directory = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
            | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(parent, flags)
        parent_before = os.fstat(directory)
        if (not stat.S_ISDIR(parent_before.st_mode)
                or (os.name != "nt" and stat.S_IMODE(parent_before.st_mode) & 0o077)
                or (hasattr(os, "getuid") and parent_before.st_uid != os.getuid())):
            raise ValueError("facility token directory is unsafe")
        details = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode)
                or details.st_nlink != 1 or details.st_size > MAX_SECRET_BYTES
                or (os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077)
                or (hasattr(os, "getuid") and details.st_uid != os.getuid())):
            raise ValueError("facility token file is unsafe")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        try:
            opened = os.fstat(descriptor)
            if ((opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size,
                 opened.st_ctime_ns, opened.st_mtime_ns)
                    != (details.st_dev, details.st_ino, details.st_nlink,
                        details.st_size, details.st_ctime_ns, details.st_mtime_ns)):
                raise ValueError("facility token changed while opening")
            raw = os.read(descriptor, MAX_SECRET_BYTES + 1)
            if len(raw) != opened.st_size:
                raise ValueError("facility token changed while reading")
        finally:
            os.close(descriptor)
        parent_after = os.fstat(directory)
        if ((parent_before.st_dev, parent_before.st_ino, parent_before.st_mode,
             parent_before.st_uid, parent_before.st_gid)
                != (parent_after.st_dev, parent_after.st_ino,
                    parent_after.st_mode, parent_after.st_uid,
                    parent_after.st_gid)):
            raise ValueError("facility token directory changed while reading")
        token = raw.decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("facility token is unavailable") from error
    finally:
        if directory is not None:
            os.close(directory)
    if (BEARER.fullmatch(token) is None
            or not hmac.compare_digest(
                hashlib.sha256(token.encode("ascii")).hexdigest(),
                expected_sha256,
            )):
        raise ValueError("facility token is invalid")
    return token


def build_backend(settings: Mapping[str, Any]) -> HTTPSExecutionBackend:
    """Build the standard digest-pinned backend used by executor runtime."""
    expected = {
        "schema", "backend_id", "backend_version", "facility_id",
        "facility_version", "endpoint", "authentication",
        "request_timeout_seconds", "max_input_bytes", "max_output_tokens",
        "cost_unit", "max_cost_units", "allow_loopback_http",
    }
    if not isinstance(settings, Mapping) or set(settings) != expected:
        raise ValueError("HTTP backend settings are invalid")
    copied = _copy(settings)
    authentication = copied["authentication"]
    if (not isinstance(authentication, dict)
            or set(authentication) != {"scheme", "token_file", "token_sha256"}
            or authentication.get("scheme") != "bearer"):
        raise ValueError("HTTP backend authentication settings are invalid")
    token_file, token_sha256 = (
        authentication.get("token_file"), authentication.get("token_sha256"),
    )

    # Fail deployment readiness before a ledger or dispatch slot can be
    # created.  Requests deliberately re-read and revalidate the file below so
    # post-start replacement or permission drift also fails closed.
    _protected_token(token_file, token_sha256)

    def authentication_headers():
        token = _protected_token(token_file, token_sha256)
        return {"Authorization": "Bearer " + token}

    return HTTPSExecutionBackend(
        copied["endpoint"], authentication_headers,
        backend_id=copied["backend_id"],
        backend_version=copied["backend_version"],
        facility_id=copied["facility_id"],
        facility_version=copied["facility_version"],
        request_timeout_seconds=copied["request_timeout_seconds"],
        max_input_bytes=copied["max_input_bytes"],
        max_output_tokens=copied["max_output_tokens"],
        cost_unit=copied["cost_unit"],
        max_cost_units=copied["max_cost_units"],
        allow_loopback_http=copied["allow_loopback_http"],
    )
