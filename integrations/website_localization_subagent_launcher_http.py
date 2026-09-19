#!/usr/bin/env python3
"""Provider-neutral HTTPS launcher for isolated review subagents.

The trusted review host assigns the reviewer and supplies the already reduced
model input.  This adapter forwards that exact assignment to a separately
operated execution service.  The service must atomically deduplicate physical
starts by ``execution_key`` and expose the same operation through reconciliation.
Authentication never enters the JSON body or the model-visible input.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.parse
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol


SETTINGS_SCHEMA = "translate-native.subagent-review-http-launcher.v1"
REQUEST_SCHEMA = "translate-native.subagent-review-launcher-request.v1"
RESPONSE_SCHEMA = "translate-native.subagent-review-launcher-response.v1"
WORKER_SCHEMA = "translate-native.subagent-review-http-worker.v1"
MAX_ENDPOINT_LENGTH = 2048
MAX_REQUEST_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 4_500_000
MAX_WORKER_BYTES = 6_500_000
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BEARER = re.compile(r"^[A-Za-z0-9._~+/=-]{32,2048}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,117}$")
STATUSES = {"completed", "not_started", "running", "unknown", "cancel_pending"}
ACTIVE = {"running", "unknown", "cancel_pending"}


class SubagentLauncherFailed(RuntimeError):
    """Content-free failure understood by the trusted review host."""

    host_subagent_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("launcher error code is invalid")
        if type(retryable) is not bool:
            raise ValueError("launcher retryability must be boolean")
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


_TRANSPORT_WORKER_CODE = r'''
import base64,json,socket,sys,urllib.error,urllib.request
SCHEMA="translate-native.subagent-review-http-worker.v1"
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
        emit({"schema":SCHEMA,"ok":True,"status":int(error.code),"headers":[list(x) for x in error.headers.items()] if error.headers else [],"body":""})
    else:
        try:
            response_body=response.read(MAX_RESPONSE+1)
            emit({"schema":SCHEMA,"ok":True,"status":int(response.status),"headers":[list(x) for x in response.headers.items()],"body":base64.b64encode(response_body).decode("ascii")})
        finally: response.close()
except (urllib.error.URLError,TimeoutError,socket.timeout,OSError):
    emit({"schema":SCHEMA,"ok":False,"code":"launcher.network","retryable":True})
except Exception:
    emit({"schema":SCHEMA,"ok":False,"code":"launcher.transport_invalid","retryable":True})
'''


class URLTransport:
    """One-request subprocess transport with a parent-enforced wall timeout."""

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
                timeout=max(0.001, float(timeout)), check=False,
                env={}, start_new_session=os.name != "nt",
            )
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise SubagentLauncherFailed("launcher.network", retryable=True) from None
        if completed.returncode != 0 or len(completed.stdout) > MAX_WORKER_BYTES:
            raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True)
        try:
            reply = json.loads(
                completed.stdout.decode("utf-8"), object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True) from None
        if (not isinstance(reply, dict) or reply.get("schema") != WORKER_SCHEMA
                or type(reply.get("ok")) is not bool):
            raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True)
        if reply["ok"] is False:
            if set(reply) != {"schema", "ok", "code", "retryable"}:
                raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True)
            raise SubagentLauncherFailed(
                reply["code"], retryable=reply["retryable"],
            )
        if (set(reply) != {"schema", "ok", "status", "headers", "body"}
                or type(reply.get("status")) is not int
                or not isinstance(reply.get("headers"), list)
                or any(not isinstance(pair, list) or len(pair) != 2
                       or not all(isinstance(item, str) for item in pair)
                       for pair in reply["headers"])
                or not isinstance(reply.get("body"), str)):
            raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True)
        try:
            response_body = base64.b64decode(reply["body"], validate=True)
        except (ValueError, TypeError):
            raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True) from None
        return HTTPResult(
            reply["status"], tuple(tuple(pair) for pair in reply["headers"]),
            response_body,
        )


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
        raise SubagentLauncherFailed("launcher.payload_invalid", retryable=False) from None
    if not encoded or len(encoded) > maximum:
        raise SubagentLauncherFailed("launcher.payload_invalid", retryable=False)
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
            or any(ord(character) <= 32 or ord(character) == 127 for character in value)):
        raise ValueError("launcher endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("launcher endpoint is invalid") from None
    hostname = parsed.hostname
    if (not hostname or not hostname.isascii() or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.path.startswith("/") or parsed.path.startswith("//")):
        raise ValueError("launcher endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif not (parsed.scheme == "http" and allow_loopback_http
              and hostname.lower() in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("launcher endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("launcher endpoint is invalid")
    return value


def _assignment(value: Any) -> dict:
    fields = (
        "execution_key", "route_id", "phase", "reviewer_role",
        "reviewer_agent_id", "reviewer_session_id", "model_id",
        "model_version", "host_policy_version", "deadline_seconds",
        "max_output_tokens", "max_input_bytes", "cost_unit",
        "max_cost_units",
    )
    try:
        result = {name: getattr(value, name) for name in fields}
    except (AttributeError, TypeError):
        raise SubagentLauncherFailed("launcher.assignment_invalid", retryable=False) from None
    string_fields = fields[:9] + ("cost_unit",)
    if (not all(isinstance(result[name], str) for name in string_fields)
            or SHA256.fullmatch(result["execution_key"]) is None
            or result["phase"] not in {"target_native", "source_fidelity"}
            or any(TOKEN.fullmatch(result[name]) is None for name in string_fields[1:]
                   if name != "phase")
            or type(result["deadline_seconds"]) is not int
            or not 1 <= result["deadline_seconds"] <= 300
            or type(result["max_output_tokens"]) is not int
            or not 128 <= result["max_output_tokens"] <= 32768
            or type(result["max_input_bytes"]) is not int
            or not 1024 <= result["max_input_bytes"] <= MAX_REQUEST_BYTES
            or type(result["max_cost_units"]) is not int
            or not 1 <= result["max_cost_units"] <= 1_000_000_000):
        raise SubagentLauncherFailed("launcher.assignment_invalid", retryable=False)
    return result


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True)
    result = {}
    for item in value:
        if (not isinstance(item, tuple) or len(item) != 2
                or not all(isinstance(part, str) for part in item)):
            raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                raise SubagentLauncherFailed("launcher.response_headers", retryable=False)
            result[name] = content
    return result


class HTTPSSubagentLauncher:
    """Bounded adapter for a host-owned idempotent subagent execution service."""

    def __init__(self, endpoint: str, authentication_headers: Callable[[], Mapping[str, str]],
                 *, launcher_id: str, launcher_version: str, executor_id: str,
                 transport: HTTPTransport | None = None,
                 request_timeout_seconds: int = 60,
                 poll_interval_milliseconds: int = 250,
                 max_status_polls: int = 120,
                 max_input_bytes: int = 2_000_000,
                 cost_unit: str = "deployment-cost-unit",
                 max_cost_units: int = 100_000,
                 max_concurrent_executions: int = 4,
                 allow_loopback_http: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        if type(allow_loopback_http) is not bool:
            raise TypeError("allow_loopback_http must be boolean")
        self.endpoint = _endpoint(endpoint, allow_loopback_http)
        self.launcher_id = _identifier(launcher_id, "launcher_id")
        self.launcher_version = _identifier(launcher_version, "launcher_version")
        self.executor_id = _identifier(executor_id, "executor_id")
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if (type(request_timeout_seconds) is not int
                or not 1 <= request_timeout_seconds <= 300
                or type(poll_interval_milliseconds) is not int
                or not 10 <= poll_interval_milliseconds <= 5000
                or type(max_status_polls) is not int
                or not 1 <= max_status_polls <= 1000
                or type(max_input_bytes) is not int
                or not 1024 <= max_input_bytes <= MAX_REQUEST_BYTES
                or type(max_cost_units) is not int
                or not 1 <= max_cost_units <= 1_000_000_000
                or type(max_concurrent_executions) is not int
                or not 1 <= max_concurrent_executions <= 256
                or not callable(clock) or not callable(sleeper)):
            raise ValueError("launcher budgets are invalid")
        self.cost_unit = _identifier(cost_unit, "cost_unit")
        self.authentication_headers = authentication_headers
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.request_timeout_seconds = request_timeout_seconds
        self.poll_interval_seconds = poll_interval_milliseconds / 1000.0
        self.max_status_polls = max_status_polls
        self.max_input_bytes = max_input_bytes
        self.max_cost_units = max_cost_units
        self.max_concurrent_executions = max_concurrent_executions
        self._capacity_lock = threading.RLock()
        self._active_executions: set[str] = set()
        self.clock, self.sleeper = clock, sleeper

    def _reserve_execution(self, execution_key: str) -> None:
        with self._capacity_lock:
            if execution_key in self._active_executions:
                return
            if len(self._active_executions) >= self.max_concurrent_executions:
                raise SubagentLauncherFailed("launcher.capacity", retryable=True)
            self._active_executions.add(execution_key)

    def _release_execution(self, execution_key: str) -> None:
        with self._capacity_lock:
            self._active_executions.discard(execution_key)

    def _auth(self) -> dict[str, str]:
        try:
            supplied = self.authentication_headers()
        except Exception:
            raise SubagentLauncherFailed("launcher.authentication", retryable=False) from None
        if (not isinstance(supplied, Mapping)
                or set(name.lower() for name in supplied) != {"authorization"}):
            raise SubagentLauncherFailed("launcher.authentication", retryable=False)
        name, value = next(iter(supplied.items()))
        if (not isinstance(name, str) or not isinstance(value, str)
                or not value.startswith("Bearer ")
                or BEARER.fullmatch(value[7:]) is None
                or "\r" in value or "\n" in value):
            raise SubagentLauncherFailed("launcher.authentication", retryable=False)
        return {name: value}

    def _execute_unsigned(self, assignment: Any, *,
                          model_input: Mapping[str, Any],
                          deadline_seconds: int,
                          max_output_tokens: int) -> tuple[dict, dict, int]:
        assigned = _assignment(assignment)
        if (not isinstance(model_input, Mapping)
                or deadline_seconds != assigned["deadline_seconds"]
                or max_output_tokens != assigned["max_output_tokens"]
                or assigned["max_input_bytes"] > self.max_input_bytes
                or assigned["cost_unit"] != self.cost_unit
                or assigned["max_cost_units"] > self.max_cost_units):
            raise SubagentLauncherFailed("launcher.request_invalid", retryable=False)
        copied_input = _copy(model_input)
        input_bytes = len(_raw(copied_input))
        if input_bytes > assigned["max_input_bytes"]:
            raise SubagentLauncherFailed("launcher.input_budget", retryable=False)
        unsigned = {
            "schema": REQUEST_SCHEMA, "operation": "execute",
            "launcher_id": self.launcher_id,
            "launcher_version": self.launcher_version,
            "executor_id": self.executor_id, "assignment": assigned,
            "model_input": copied_input,
            "budgets": {
                "deadline_seconds": assigned["deadline_seconds"],
                "max_output_tokens": assigned["max_output_tokens"],
                "max_input_bytes": assigned["max_input_bytes"],
                "cost_unit": assigned["cost_unit"],
                "max_cost_units": assigned["max_cost_units"],
                "max_concurrent_executions": self.max_concurrent_executions,
            },
        }
        return assigned, unsigned, input_bytes

    def _request(self, operation: str, assignment: Any, *,
                 model_input: Mapping[str, Any] | None = None,
                 deadline_seconds: int | None = None,
                 max_output_tokens: int | None = None) -> tuple[dict, bytes, dict[str, str]]:
        if operation == "execute":
            assigned, unsigned, _input_bytes = self._execute_unsigned(
                assignment, model_input=model_input,
                deadline_seconds=deadline_seconds,
                max_output_tokens=max_output_tokens,
            )
        elif operation == "reconcile":
            if any(value is not None for value in (
                    model_input, deadline_seconds, max_output_tokens)):
                raise SubagentLauncherFailed("launcher.request_invalid", retryable=False)
            assigned = _assignment(assignment)
            unsigned = {
                "schema": REQUEST_SCHEMA, "operation": operation,
                "launcher_id": self.launcher_id,
                "launcher_version": self.launcher_version,
                "executor_id": self.executor_id, "assignment": assigned,
            }
        else:
            raise SubagentLauncherFailed("launcher.request_invalid", retryable=False)
        request_sha256 = _sha(unsigned)
        envelope = {**unsigned, "request_sha256": request_sha256}
        body = _raw(envelope)
        headers = self._auth()
        headers.update({
            "Accept": "application/json", "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": assigned["execution_key"],
            "X-Subagent-Execution-Key": assigned["execution_key"],
            "X-Subagent-Operation": operation,
            "X-Subagent-Request-Sha256": request_sha256,
        })
        return envelope, body, headers

    def _post(self, operation: str, assignment: Any, *, timeout: float,
              model_input: Mapping[str, Any] | None = None,
              deadline_seconds: int | None = None,
              max_output_tokens: int | None = None,
              expected_execute_sha256: str | None = None,
              expected_input_bytes: int | None = None,
              expected_max_output_tokens: int | None = None) -> dict:
        envelope, body, headers = self._request(
            operation, assignment, model_input=model_input,
            deadline_seconds=deadline_seconds, max_output_tokens=max_output_tokens,
        )
        try:
            result = self.transport.post(
                self.endpoint, headers, body,
                timeout=min(float(self.request_timeout_seconds), max(0.001, timeout)),
            )
        except SubagentLauncherFailed:
            raise
        except Exception:
            raise SubagentLauncherFailed("launcher.network", retryable=True) from None
        if _raw(envelope) != body:
            raise SubagentLauncherFailed("launcher.request_mutated", retryable=False)
        if (not isinstance(result, HTTPResult) or type(result.status) is not int
                or not 100 <= result.status <= 599):
            raise SubagentLauncherFailed("launcher.transport_invalid", retryable=True)
        if result.status not in {200, 202}:
            if 300 <= result.status <= 399:
                code, retryable = "launcher.redirect", False
            elif result.status in {401, 403}:
                code, retryable = "launcher.authentication_rejected", False
            elif result.status == 409:
                code, retryable = "launcher.idempotency_conflict", False
            else:
                code = "launcher.status"
                retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            raise SubagentLauncherFailed(code, retryable=retryable)
        response_headers = _headers(result.headers)
        if response_headers.get("content-type", "").lower().replace(" ", "") not in {
                "application/json", "application/json;charset=utf-8"}:
            raise SubagentLauncherFailed("launcher.response_content_type", retryable=False)
        if (not isinstance(result.body, bytes) or not result.body
                or len(result.body) > MAX_RESPONSE_BYTES):
            raise SubagentLauncherFailed("launcher.response_size", retryable=False)
        declared = response_headers.get("content-length")
        if declared is not None and (
                not declared.isascii() or not declared.isdecimal()
                or int(declared) != len(result.body)):
            raise SubagentLauncherFailed("launcher.response_size", retryable=False)
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError
            reply = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise SubagentLauncherFailed("launcher.response_json", retryable=False) from None
        expected = {
            "schema", "operation", "launcher_id", "launcher_version",
            "executor_id", "execution_key", "request_sha256", "status", "execution",
            "usage",
        }
        assigned = envelope["assignment"]
        if (not isinstance(reply, dict) or set(reply) != expected
                or reply.get("schema") != RESPONSE_SCHEMA
                or reply.get("operation") != operation
                or reply.get("launcher_id") != self.launcher_id
                or reply.get("launcher_version") != self.launcher_version
                or reply.get("executor_id") != self.executor_id
                or reply.get("execution_key") != assigned["execution_key"]
                or reply.get("request_sha256") != envelope["request_sha256"]
                or reply.get("status") not in STATUSES
                or (reply["status"] == "completed") != isinstance(reply["execution"], dict)
                or (reply["status"] != "completed" and reply["execution"] is not None)
                or (reply["status"] == "completed") != isinstance(reply["usage"], dict)
                or (reply["status"] != "completed" and reply["usage"] is not None)
                or (result.status == 202) != (reply["status"] in ACTIVE)):
            raise SubagentLauncherFailed("launcher.response_invalid", retryable=False)
        if reply["status"] == "completed":
            usage = reply["usage"]
            if operation == "execute":
                expected_execute_sha256 = envelope["request_sha256"]
                expected_input_bytes = len(_raw(envelope["model_input"]))
                expected_max_output_tokens = envelope["budgets"]["max_output_tokens"]
            if (not isinstance(expected_execute_sha256, str)
                    or SHA256.fullmatch(expected_execute_sha256) is None
                    or type(expected_input_bytes) is not int
                    or type(expected_max_output_tokens) is not int
                    or set(usage) != {
                        "execute_request_sha256", "cost_unit", "cost_units",
                        "input_bytes", "output_tokens",
                    }
                    or usage.get("execute_request_sha256") != expected_execute_sha256
                    or usage.get("cost_unit") != assigned["cost_unit"]
                    or type(usage.get("cost_units")) is not int
                    or not 0 <= usage["cost_units"] <= assigned["max_cost_units"]
                    or type(usage.get("input_bytes")) is not int
                    or usage["input_bytes"] != expected_input_bytes
                    or usage["input_bytes"] > assigned["max_input_bytes"]
                    or type(usage.get("output_tokens")) is not int
                    or not 0 <= usage["output_tokens"] <= expected_max_output_tokens):
                raise SubagentLauncherFailed("launcher.usage_invalid", retryable=False)
        return _copy(reply)

    @staticmethod
    def _completed(reply: Mapping[str, Any]) -> dict:
        return {**_copy(reply["execution"]), "usage": _copy(reply["usage"])}

    def execute_idempotent(self, assignment: Any, model_input: Mapping[str, Any], *,
                           deadline_seconds: int,
                           max_output_tokens: int) -> Mapping[str, Any]:
        assigned, execute_unsigned, input_bytes = self._execute_unsigned(
            assignment, model_input=model_input,
            deadline_seconds=deadline_seconds,
            max_output_tokens=max_output_tokens,
        )
        execution_key = assigned["execution_key"]
        self._reserve_execution(execution_key)
        try:
            execute_sha256 = _sha(execute_unsigned)
            started = self.clock()
            reply = self._post(
                "execute", assignment, timeout=float(deadline_seconds),
                model_input=model_input, deadline_seconds=deadline_seconds,
                max_output_tokens=max_output_tokens,
            )
            if reply["status"] == "completed":
                self._release_execution(execution_key)
                return self._completed(reply)
            if reply["status"] == "not_started":
                self._release_execution(execution_key)
            if reply["status"] != "running":
                raise SubagentLauncherFailed("launcher.start_invalid", retryable=False)
            for _attempt in range(self.max_status_polls):
                remaining = float(deadline_seconds) - (self.clock() - started)
                if remaining <= self.poll_interval_seconds:
                    break
                self.sleeper(self.poll_interval_seconds)
                remaining = float(deadline_seconds) - (self.clock() - started)
                if remaining <= 0:
                    break
                reconciled = self._post(
                    "reconcile", assignment, timeout=remaining,
                    expected_execute_sha256=execute_sha256,
                    expected_input_bytes=input_bytes,
                    expected_max_output_tokens=max_output_tokens,
                )
                if reconciled["status"] == "completed":
                    self._release_execution(execution_key)
                    return self._completed(reconciled)
                if reconciled["status"] == "not_started":
                    self._release_execution(execution_key)
                if reconciled["status"] != "running":
                    raise SubagentLauncherFailed(
                        "launcher.reconcile_" + reconciled["status"], retryable=False,
                    )
            raise SubagentLauncherFailed("launcher.running", retryable=True)
        except SubagentLauncherFailed as error:
            if error.code in {
                    "launcher.authentication", "launcher.authentication_rejected",
                    "launcher.redirect", "launcher.request_invalid",
                    "launcher.input_budget",
            }:
                self._release_execution(execution_key)
            raise

    def reconcile(self, assignment: Any, model_input: Mapping[str, Any], *,
                  deadline_seconds: int,
                  max_output_tokens: int) -> Mapping[str, Any]:
        assigned, execute_unsigned, input_bytes = self._execute_unsigned(
            assignment, model_input=model_input,
            deadline_seconds=deadline_seconds,
            max_output_tokens=max_output_tokens,
        )
        reply = self._post(
            "reconcile", assignment, timeout=float(self.request_timeout_seconds),
            expected_execute_sha256=_sha(execute_unsigned),
            expected_input_bytes=input_bytes,
            expected_max_output_tokens=max_output_tokens,
        )
        execution = (self._completed(reply)
                     if reply["status"] == "completed" else None)
        if reply["status"] in {"completed", "not_started"}:
            self._release_execution(assigned["execution_key"])
        return {"status": reply["status"], "execution": execution}

    def close(self):
        closer = getattr(self.transport, "close", None)
        if callable(closer):
            closer()


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev, details.st_ino, details.st_nlink, details.st_size,
        details.st_ctime_ns, details.st_mtime_ns,
    )


def _directory_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev, details.st_ino, details.st_mode,
        details.st_uid, details.st_gid,
    )


def _validate_directory(path: Path, details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError("launcher authentication directory is invalid")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o022:
        raise ValueError("launcher authentication directory is unsafe")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ValueError("launcher authentication directory has the wrong owner")


@contextmanager
def _open_directory(path: Path) -> Iterator[int | None]:
    if os.name == "nt":
        yield None
        return
    anchor = Path(path.anchor)
    relative = path.parent.relative_to(anchor)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    descriptor = None
    try:
        descriptor = os.open(anchor, flags)
        for component in relative.parts:
            child = os.open(component, flags, dir_fd=descriptor)
            try:
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise ValueError("launcher authentication directory is invalid")
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        _validate_directory(path.parent, os.fstat(descriptor))
        expected = _directory_identity(os.fstat(descriptor))
        try:
            yield descriptor
        finally:
            after = path.parent.lstat()
            _validate_directory(path.parent, after)
            if _directory_identity(after) != expected:
                raise ValueError("launcher authentication directory changed")
    except ValueError:
        raise
    except OSError:
        raise ValueError("launcher authentication directory is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_protected(path: Path, maximum: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("launcher authentication path is invalid")
    with _open_directory(path) as directory:
        try:
            before = (os.lstat(path) if directory is None else os.stat(
                path.name, dir_fd=directory, follow_symlinks=False,
            ))
            if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                    or before.st_nlink != 1 or not 1 <= before.st_size <= maximum
                    or (os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077)
                    or (hasattr(os, "getuid") and before.st_uid != os.getuid())):
                raise ValueError("launcher authentication file is unsafe")
            flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
            descriptor = (os.open(path, flags) if directory is None else os.open(
                path.name, flags, dir_fd=directory,
            ))
        except ValueError:
            raise
        except OSError:
            raise ValueError("launcher authentication file is unavailable") from None
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before):
                raise ValueError("launcher authentication file changed")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(maximum + 1)
            after = os.fstat(descriptor)
            after_path = (os.lstat(path) if directory is None else os.stat(
                path.name, dir_fd=directory, follow_symlinks=False,
            ))
            if (_file_identity(after) != _file_identity(opened)
                    or _file_identity(after_path) != _file_identity(opened)):
                raise ValueError("launcher authentication file changed")
        finally:
            os.close(descriptor)
    if not 1 <= len(raw) <= maximum:
        raise ValueError("launcher authentication file is invalid")
    return raw


def _protected_token(path_value: Any, expected_sha256: Any) -> str:
    if (not isinstance(path_value, str) or len(path_value) > 4096
            or not Path(path_value).is_absolute()
            or not isinstance(expected_sha256, str)
            or SHA256.fullmatch(expected_sha256) is None):
        raise ValueError("launcher authentication configuration is invalid")
    try:
        raw = _read_protected(Path(path_value), 4096)
        token = raw.decode("ascii")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("launcher authentication token is unavailable") from None
    if (BEARER.fullmatch(token) is None
            or not hashlib.sha256(raw).hexdigest() == expected_sha256):
        raise ValueError("launcher authentication token is invalid")
    return token


def build_launcher(settings: Mapping[str, Any]) -> HTTPSSubagentLauncher:
    """Protected runtime factory for the bundled HTTPS launcher."""
    settings = _copy(settings)
    expected = {
        "schema", "launcher_id", "launcher_version", "executor_id", "endpoint",
        "authentication", "request_timeout_seconds", "poll_interval_milliseconds",
        "max_status_polls", "max_input_bytes", "cost_unit", "max_cost_units",
        "max_concurrent_executions", "allow_loopback_http",
    }
    if (not isinstance(settings, dict) or set(settings) != expected
            or settings.get("schema") != SETTINGS_SCHEMA
            or not isinstance(settings.get("authentication"), dict)
            or set(settings["authentication"]) != {
                "scheme", "token_file", "token_sha256",
            }
            or settings["authentication"].get("scheme") != "bearer"):
        raise ValueError("launcher settings are invalid")
    authentication = settings["authentication"]
    token = _protected_token(
        authentication["token_file"], authentication["token_sha256"],
    )
    return HTTPSSubagentLauncher(
        settings["endpoint"], lambda: {"Authorization": "Bearer " + token},
        launcher_id=settings["launcher_id"],
        launcher_version=settings["launcher_version"],
        executor_id=settings["executor_id"],
        request_timeout_seconds=settings["request_timeout_seconds"],
        poll_interval_milliseconds=settings["poll_interval_milliseconds"],
        max_status_polls=settings["max_status_polls"],
        max_input_bytes=settings["max_input_bytes"],
        cost_unit=settings["cost_unit"],
        max_cost_units=settings["max_cost_units"],
        max_concurrent_executions=settings["max_concurrent_executions"],
        allow_loopback_http=settings["allow_loopback_http"],
    )
