#!/usr/bin/env python3
"""Durable provider-neutral executor for isolated review subagents.

The executor is the server half of ``website_localization_subagent_launcher_http``.
It authenticates and validates the launcher's closed request contract, reserves
global capacity in SQLite before external work, and delegates one exact isolated
review to a deployment backend.  It owns no Guard signer or publication right.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = ROOT / "integrations" / "website_localization_subagent_launcher_http.py"
PATH = "/v1/subagent-executions"
MAX_BODY_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 4_500_000
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BEARER = re.compile(r"^[A-Za-z0-9._~+/=-]{32,2048}$")
ACTIVE = {"dispatching", "running", "unknown", "cancel_pending"}
BACKEND_STATUSES = {"completed", "not_started", "running", "unknown", "cancel_pending"}
NATIVE_ALLOWED = {
    "candidate", "target", "content_type", "quality_profile", "response_schema",
    "audience", "tone_profile", "target_terms", "commercial_quality_profile",
}
FIDELITY_ALLOWED = {
    "source", "candidate", "target", "content_type", "content_guidance",
    "glossary", "protected_terms", "quality_profile", "response_schema",
    "audience", "tone_profile", "commercial_quality_profile",
    "commercial_profile", "commercial_review_evidence_contract",
}
NATIVE_FORBIDDEN_KEYS = {
    "source", "source_text", "source_locale", "source_language", "messages",
    "history", "creator", "creator_id", "creator_session", "tools", "tool",
    "credentials", "credential", "secret", "signature", "signing_key",
    "publication", "publish", "execution_key", "route_id", "provider_id",
    "previous_receipt", "previous_receipt_sha256", "inherited_context",
}


def _load(name: str, path: Path):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LAUNCHER = _load("blun_website_localization_subagent_launcher_http", LAUNCHER_PATH)


class SubagentExecutorBlocked(RuntimeError):
    """Content-free failure at the executor boundary."""

    def __init__(self, code: str, status: int, *, retryable: bool = False):
        self.code, self.status, self.retryable = code, status, retryable
        super().__init__(code)


def _blocked(code: str, status: int, *, retryable: bool = False):
    return SubagentExecutorBlocked("subagent_executor." + code, status,
                                   retryable=retryable)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _raw(value: Any, *, maximum: int = MAX_RESPONSE_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _blocked("payload_invalid", 400) from None
    if not encoded or len(encoded) > maximum:
        raise _blocked("payload_invalid", 400)
    return encoded


def _copy(value: Any) -> Any:
    return json.loads(_raw(value))


def _sha(value: Any) -> str:
    return hashlib.sha256(_raw(value)).hexdigest()


def _identifier(value: Any, code: str = "identity_invalid") -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise _blocked(code, 400)
    return value


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _blocked("model_input_invalid", 422)
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def task_policy_sha256(model_input: Mapping[str, Any]) -> str:
    """Bind every permitted instruction/profile field, excluding reviewed prose."""
    task = _copy(model_input)
    phase, inputs = task.get("phase"), task.get("input")
    if not isinstance(inputs, dict):
        raise _blocked("model_input_invalid", 422)
    if phase == "target_native":
        if "candidate" not in inputs:
            raise _blocked("model_input_invalid", 422)
        task["input"] = dict(inputs)
        task["input"]["candidate"] = "<candidate>"
        projection = task
    elif phase == "source_fidelity":
        required = {"target", "content_type", "quality_profile", "response_schema"}
        if not required.issubset(inputs):
            raise _blocked("model_input_invalid", 422)
        pinned = set(required)
        for optional in (
            "commercial_profile", "commercial_quality_profile",
            "commercial_review_evidence_contract",
        ):
            if optional in inputs:
                pinned.add(optional)
        projection = {
            "schema": task.get("schema"), "phase": phase,
            "system_instruction": task.get("system_instruction"),
            "input": {key: inputs[key] for key in sorted(pinned)},
        }
    else:
        raise _blocked("model_input_invalid", 422)
    return _sha(projection)


@dataclass(frozen=True)
class PinnedExecutorRoute:
    route_id: str
    phase: str
    reviewer_role: str
    reviewer_agent_id: str
    model_id: str
    model_version: str
    host_policy_version: str
    target_locale: str
    content_type: str
    task_policy_sha256: str

    def __post_init__(self):
        for value in (
            self.route_id, self.phase, self.reviewer_role,
            self.reviewer_agent_id, self.model_id, self.model_version,
            self.host_policy_version, self.target_locale, self.content_type,
        ):
            if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
                raise ValueError("executor route contains an invalid identifier")
        if self.phase not in {"target_native", "source_fidelity"}:
            raise ValueError("executor route phase is unsupported")
        expected = ("target-native-reviewer" if self.phase == "target_native"
                    else "source-fidelity-reviewer")
        if self.reviewer_role != expected:
            raise ValueError("executor route role does not match phase")
        if (not isinstance(self.task_policy_sha256, str)
                or SHA256.fullmatch(self.task_policy_sha256) is None):
            raise ValueError("executor route task policy digest is invalid")


class PinnedExecutorPolicy:
    def __init__(self, routes: list[PinnedExecutorRoute]):
        if not isinstance(routes, list) or not routes:
            raise ValueError("at least one executor route is required")
        indexed = {}
        for route in routes:
            if not isinstance(route, PinnedExecutorRoute):
                raise TypeError("executor routes must be pinned routes")
            if route.route_id in indexed:
                raise ValueError("duplicate executor route")
            indexed[route.route_id] = route
        self._routes = indexed

    def validate(self, assignment: Mapping[str, Any], model_input: Any | None,
                 *, operation: str) -> PinnedExecutorRoute:
        route = self._routes.get(assignment.get("route_id"))
        if route is None:
            raise _blocked("route_unavailable", 403)
        expected = {
            "phase": route.phase, "reviewer_role": route.reviewer_role,
            "reviewer_agent_id": route.reviewer_agent_id,
            "model_id": route.model_id, "model_version": route.model_version,
            "host_policy_version": route.host_policy_version,
        }
        if any(assignment.get(name) != value for name, value in expected.items()):
            raise _blocked("route_binding", 403)
        if operation == "reconcile":
            if model_input is not None:
                raise _blocked("request_invalid", 400)
            return route
        self._validate_model_input(route, model_input)
        if task_policy_sha256(model_input) != route.task_policy_sha256:
            raise _blocked("task_policy_binding", 403)
        return route

    @staticmethod
    def _validate_model_input(route: PinnedExecutorRoute, value: Any) -> None:
        value = _copy(value)
        if (not isinstance(value, dict)
                or set(value) != {"schema", "phase", "system_instruction", "input"}
                or not isinstance(value.get("schema"), str)
                or TOKEN.fullmatch(value["schema"]) is None
                or value.get("phase") != route.phase
                or not isinstance(value.get("system_instruction"), str)
                or not value["system_instruction"].strip()
                or not isinstance(value.get("input"), dict)):
            raise _blocked("model_input_invalid", 422)
        inputs = value["input"]
        required = {
            "candidate", "target", "content_type", "quality_profile",
            "response_schema",
        }
        allowed = NATIVE_ALLOWED if route.phase == "target_native" else FIDELITY_ALLOWED
        if (not required.issubset(inputs) or not set(inputs).issubset(allowed)
                or route.phase == "source_fidelity" and "source" not in inputs
                or not isinstance(inputs.get("candidate"), str)
                or not inputs["candidate"]
                or inputs.get("content_type") != route.content_type
                or not isinstance(inputs.get("target"), dict)
                or inputs["target"].get("locale") != route.target_locale):
            raise _blocked("model_input_invalid", 422)
        if route.phase == "target_native":
            for key in _walk_keys(inputs):
                folded = key.casefold().replace("-", "_")
                if folded in NATIVE_FORBIDDEN_KEYS:
                    raise _blocked("native_source_isolation", 422)


class ExecutionBackend(Protocol):
    backend_id: str
    backend_version: str

    def execute_idempotent(self, assignment: Mapping[str, Any],
                           model_input: Mapping[str, Any], *,
                           execute_request_sha256: str,
                           budgets: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def reconcile(self, assignment: Mapping[str, Any], *,
                  execute_request_sha256: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _Reservation:
    execution_key: str
    execute_request_sha256: str
    assignment_sha256: str
    generation: int
    status: str
    owner: bool
    execution: dict | None = None
    usage: dict | None = None


class SQLiteExecutionLedger:
    """Atomic idempotency and capacity state; dispatch is never lease-restarted."""

    COLUMNS = (
        "execution_key", "principal_sha256", "execute_request_sha256",
        "assignment_sha256", "launcher_id", "launcher_version", "executor_id",
        "status", "generation", "input_bytes", "max_output_tokens",
        "cost_unit", "max_cost_units", "execution_json", "usage_json",
        "created_at", "updated_at",
    )

    def __init__(self, path: Path, *, max_concurrent_executions: int,
                 clock: Callable[[], float] = time.time,
                 initialize_schema: bool = True):
        if (not isinstance(path, Path) or type(max_concurrent_executions) is not int
                or not 1 <= max_concurrent_executions <= 256
                or not callable(clock)):
            raise ValueError("executor ledger configuration is invalid")
        self.path, self.max_concurrent_executions = path, max_concurrent_executions
        self.clock = clock
        self._schema_lock = threading.Lock()
        self._ensure_schema() if initialize_schema else self._verify_schema()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _ensure_schema(self):
        with self._schema_lock, self._connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS subagent_executor_jobs (
                    execution_key TEXT PRIMARY KEY,
                    principal_sha256 TEXT NOT NULL,
                    execute_request_sha256 TEXT NOT NULL,
                    assignment_sha256 TEXT NOT NULL,
                    launcher_id TEXT NOT NULL,
                    launcher_version TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    input_bytes INTEGER NOT NULL,
                    max_output_tokens INTEGER NOT NULL,
                    cost_unit TEXT NOT NULL,
                    max_cost_units INTEGER NOT NULL,
                    execution_json BLOB,
                    usage_json BLOB,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS subagent_executor_active
                    ON subagent_executor_jobs(status);
            """)

    def _verify_schema(self):
        try:
            with self._schema_lock, self._connect() as connection:
                actual = tuple(row[1] for row in connection.execute(
                    "PRAGMA table_info(subagent_executor_jobs)"
                ))
                index = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='subagent_executor_active'"
                ).fetchone()
        except sqlite3.Error as error:
            raise ValueError("executor ledger schema is unavailable") from error
        if actual != self.COLUMNS or index is None:
            raise ValueError("executor ledger schema is unavailable")

    @staticmethod
    def _reservation(row: sqlite3.Row, *, owner: bool = False):
        execution = json.loads(bytes(row["execution_json"])) \
            if row["execution_json"] is not None else None
        usage = json.loads(bytes(row["usage_json"])) \
            if row["usage_json"] is not None else None
        return _Reservation(
            row["execution_key"], row["execute_request_sha256"],
            row["assignment_sha256"], row["generation"], row["status"], owner,
            execution, usage,
        )

    def reserve_execute(self, *, principal_sha256: str, request: Mapping[str, Any],
                        assignment_sha256: str, input_bytes: int) -> _Reservation:
        now, assignment = float(self.clock()), request["assignment"]
        key, digest = assignment["execution_key"], request["request_sha256"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM subagent_executor_jobs WHERE execution_key=?", (key,),
            ).fetchone()
            if row is not None:
                binding = (
                    principal_sha256, digest, assignment_sha256,
                    request["launcher_id"], request["launcher_version"],
                    request["executor_id"], input_bytes,
                    request["budgets"]["max_output_tokens"],
                    request["budgets"]["cost_unit"],
                    request["budgets"]["max_cost_units"],
                )
                stored = tuple(row[name] for name in (
                    "principal_sha256", "execute_request_sha256", "assignment_sha256",
                    "launcher_id", "launcher_version", "executor_id", "input_bytes",
                    "max_output_tokens", "cost_unit", "max_cost_units",
                ))
                if stored != binding:
                    connection.rollback()
                    raise _blocked("idempotency_conflict", 409)
                if row["status"] == "not_started":
                    active = connection.execute(
                        "SELECT COUNT(*) FROM subagent_executor_jobs "
                        "WHERE status IN ('dispatching','running','unknown','cancel_pending')"
                    ).fetchone()[0]
                    if active >= self.max_concurrent_executions:
                        connection.rollback()
                        raise _blocked("capacity", 429, retryable=True)
                    generation = row["generation"] + 1
                    connection.execute(
                        "UPDATE subagent_executor_jobs SET status='dispatching', "
                        "generation=?, updated_at=? WHERE execution_key=?",
                        (generation, now, key),
                    )
                    connection.commit()
                    changed = dict(row)
                    changed.update(status="dispatching", generation=generation,
                                   execution_json=None, usage_json=None)
                    return self._reservation(changed, owner=True)
                connection.commit()
                return self._reservation(row)
            active = connection.execute(
                "SELECT COUNT(*) FROM subagent_executor_jobs "
                "WHERE status IN ('dispatching','running','unknown','cancel_pending')"
            ).fetchone()[0]
            if active >= self.max_concurrent_executions:
                connection.rollback()
                raise _blocked("capacity", 429, retryable=True)
            connection.execute("""
                INSERT INTO subagent_executor_jobs VALUES (
                    ?,?,?,?,?,?,?,'dispatching',1,?,?,?,?,NULL,NULL,?,?
                )
            """, (
                key, principal_sha256, digest, assignment_sha256,
                request["launcher_id"], request["launcher_version"],
                request["executor_id"], input_bytes,
                request["budgets"]["max_output_tokens"],
                request["budgets"]["cost_unit"],
                request["budgets"]["max_cost_units"], now, now,
            ))
            connection.commit()
            return _Reservation(key, digest, assignment_sha256, 1,
                                "dispatching", True)

    def lookup(self, *, execution_key: str, principal_sha256: str,
               assignment_sha256: str) -> _Reservation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subagent_executor_jobs WHERE execution_key=?",
                (execution_key,),
            ).fetchone()
        if row is None:
            return None
        if (row["principal_sha256"] != principal_sha256
                or row["assignment_sha256"] != assignment_sha256):
            raise _blocked("idempotency_conflict", 409)
        return self._reservation(row)

    def transition(self, reservation: _Reservation, status: str, *,
                   execution: Mapping[str, Any] | None = None,
                   usage: Mapping[str, Any] | None = None) -> _Reservation:
        if status not in BACKEND_STATUSES:
            raise _blocked("backend_status", 503, retryable=True)
        completed = status == "completed"
        if completed != isinstance(execution, Mapping) \
                or completed != isinstance(usage, Mapping):
            raise _blocked("backend_result", 422)
        now = float(self.clock())
        encoded_execution = _raw(execution) if completed else None
        encoded_usage = _raw(usage) if completed else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("""
                UPDATE subagent_executor_jobs
                   SET status=?, execution_json=?, usage_json=?, updated_at=?
                 WHERE execution_key=? AND generation=?
                   AND status IN ('dispatching','running','unknown','cancel_pending')
            """, (status, encoded_execution, encoded_usage, now,
                  reservation.execution_key, reservation.generation)).rowcount
            if changed != 1:
                connection.rollback()
                raise _blocked("generation_lost", 503, retryable=True)
            connection.commit()
        return _Reservation(
            reservation.execution_key, reservation.execute_request_sha256,
            reservation.assignment_sha256, reservation.generation, status, False,
            _copy(execution) if completed else None,
            _copy(usage) if completed else None,
        )

    def quarantine(self, reservation: _Reservation) -> None:
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                UPDATE subagent_executor_jobs SET status='unknown', updated_at=?
                 WHERE execution_key=? AND generation=? AND status!='completed'
            """, (now, reservation.execution_key, reservation.generation))
            connection.commit()

    def count_active(self) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM subagent_executor_jobs "
                "WHERE status IN ('dispatching','running','unknown','cancel_pending')"
            ).fetchone()[0])


class SubagentExecutorApplication:
    """Auth-first WSGI application over one durable execution backend."""

    def __init__(self, *, executor_id: str, launcher_id: str,
                 launcher_version: str, bearer_token: str,
                 policy: PinnedExecutorPolicy, ledger: SQLiteExecutionLedger,
                 backend: ExecutionBackend, allow_loopback_http: bool = False):
        self.executor_id = _identifier(executor_id)
        self.launcher_id = _identifier(launcher_id)
        self.launcher_version = _identifier(launcher_version)
        if not isinstance(bearer_token, str) or BEARER.fullmatch(bearer_token) is None:
            raise ValueError("executor bearer token is invalid")
        if not isinstance(policy, PinnedExecutorPolicy):
            raise TypeError("executor policy is invalid")
        if not isinstance(ledger, SQLiteExecutionLedger):
            raise TypeError("executor ledger is invalid")
        if any(not callable(getattr(backend, name, None))
               for name in ("execute_idempotent", "reconcile")):
            raise TypeError("executor backend is invalid")
        if type(allow_loopback_http) is not bool:
            raise TypeError("allow_loopback_http must be boolean")
        self._bearer = bearer_token
        self.policy, self.ledger, self.backend = policy, ledger, backend
        self.allow_loopback_http = allow_loopback_http

    def _authenticate(self, environ: Mapping[str, Any]) -> str:
        supplied, expected = environ.get("HTTP_AUTHORIZATION"), "Bearer " + self._bearer
        if (not isinstance(supplied, str) or "," in supplied
                or not hmac.compare_digest(supplied, expected)):
            raise _blocked("authentication_rejected", 401)
        return hashlib.sha256(expected.encode("ascii")).hexdigest()

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> bytes:
        transfer, length = environ.get("HTTP_TRANSFER_ENCODING"), environ.get("CONTENT_LENGTH")
        if transfer not in {None, ""} or not isinstance(length, str) \
                or not length.isascii() or not length.isdecimal():
            raise _blocked("framing_invalid", 400)
        size = int(length)
        if not 1 <= size <= MAX_BODY_BYTES:
            raise _blocked("body_size", 413)
        stream = environ.get("wsgi.input")
        if not callable(getattr(stream, "read", None)):
            raise _blocked("body_invalid", 400)
        body = stream.read(size)
        if not isinstance(body, bytes) or len(body) != size:
            raise _blocked("body_invalid", 400)
        return body

    def _request(self, environ: Mapping[str, Any], body: bytes) -> dict:
        content_type = environ.get("CONTENT_TYPE")
        if not isinstance(content_type, str) or content_type.lower().replace(" ", "") \
                != "application/json;charset=utf-8":
            raise _blocked("content_type", 415)
        try:
            text = body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError
            request = json.loads(text, object_pairs_hook=_pairs,
                                 parse_constant=_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise _blocked("json_invalid", 400) from None
        operation = request.get("operation") if isinstance(request, dict) else None
        expected = {
            "schema", "operation", "launcher_id", "launcher_version",
            "executor_id", "assignment", "request_sha256",
        }
        if operation == "execute":
            expected |= {"model_input", "budgets"}
        if (not isinstance(request, dict) or set(request) != expected
                or request.get("schema") != LAUNCHER.REQUEST_SCHEMA
                or operation not in {"execute", "reconcile"}
                or request.get("launcher_id") != self.launcher_id
                or request.get("launcher_version") != self.launcher_version
                or request.get("executor_id") != self.executor_id):
            raise _blocked("request_invalid", 400)
        unsigned = {key: request[key] for key in request if key != "request_sha256"}
        if request.get("request_sha256") != _sha(unsigned):
            raise _blocked("request_digest", 400)
        assignment = self._assignment(request.get("assignment"))
        key = assignment["execution_key"]
        headers = {
            "HTTP_IDEMPOTENCY_KEY": key,
            "HTTP_X_SUBAGENT_EXECUTION_KEY": key,
            "HTTP_X_SUBAGENT_OPERATION": operation,
            "HTTP_X_SUBAGENT_REQUEST_SHA256": request["request_sha256"],
        }
        if any(environ.get(name) != value for name, value in headers.items()):
            raise _blocked("header_binding", 400)
        if operation == "execute":
            budgets = request.get("budgets")
            if (not isinstance(budgets, dict) or set(budgets) != {
                    "deadline_seconds", "max_output_tokens", "max_input_bytes",
                    "cost_unit", "max_cost_units", "max_concurrent_executions",
            } or any(budgets.get(name) != assignment[name] for name in (
                    "deadline_seconds", "max_output_tokens", "max_input_bytes",
                    "cost_unit", "max_cost_units"))
                    or budgets.get("max_concurrent_executions")
                    != self.ledger.max_concurrent_executions
                    or len(_raw(request["model_input"])) > assignment["max_input_bytes"]):
                raise _blocked("budget_binding", 403)
        self.policy.validate(
            assignment, request.get("model_input"), operation=operation,
        )
        request["assignment"] = assignment
        return request

    @staticmethod
    def _assignment(value: Any) -> dict:
        fields = {
            "execution_key", "route_id", "phase", "reviewer_role",
            "reviewer_agent_id", "reviewer_session_id", "model_id",
            "model_version", "host_policy_version", "deadline_seconds",
            "max_output_tokens", "max_input_bytes", "cost_unit", "max_cost_units",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise _blocked("assignment_invalid", 400)
        result = _copy(value)
        for name in (
            "route_id", "phase", "reviewer_role", "reviewer_agent_id",
            "reviewer_session_id", "model_id", "model_version",
            "host_policy_version", "cost_unit",
        ):
            _identifier(result[name], "assignment_invalid")
        if (not isinstance(result["execution_key"], str)
                or SHA256.fullmatch(result["execution_key"]) is None
                or result["phase"] not in {"target_native", "source_fidelity"}
                or type(result["deadline_seconds"]) is not int
                or not 1 <= result["deadline_seconds"] <= 300
                or type(result["max_output_tokens"]) is not int
                or not 128 <= result["max_output_tokens"] <= 32768
                or type(result["max_input_bytes"]) is not int
                or not 1024 <= result["max_input_bytes"] <= MAX_BODY_BYTES
                or type(result["max_cost_units"]) is not int
                or not 1 <= result["max_cost_units"] <= 1_000_000_000):
            raise _blocked("assignment_invalid", 400)
        return result

    @staticmethod
    def _validate_backend(result: Any, request: Mapping[str, Any], *,
                          execute_request_sha256: str, input_bytes: int) -> dict:
        result = _copy(result)
        if (not isinstance(result, dict)
                or set(result) != {"status", "execution", "usage"}
                or result.get("status") not in BACKEND_STATUSES):
            raise _blocked("backend_result", 422)
        completed = result["status"] == "completed"
        if completed != isinstance(result.get("execution"), dict) \
                or completed != isinstance(result.get("usage"), dict):
            raise _blocked("backend_result", 422)
        if not completed:
            return result
        assignment, execution, usage = (
            request["assignment"], result["execution"], result["usage"],
        )
        if (set(execution) != {
                "response", "execution_key", "phase", "reviewer_role", "agent_id",
                "session_id", "model_id", "model_version", "inherit_context",
                "tools", "max_delegation_depth",
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
            raise _blocked("execution_invalid", 422)
        if (set(usage) != {
                "execute_request_sha256", "cost_unit", "cost_units",
                "input_bytes", "output_tokens",
        } or usage.get("execute_request_sha256") != execute_request_sha256
                or usage.get("cost_unit") != assignment["cost_unit"]
                or type(usage.get("cost_units")) is not int
                or not 0 <= usage["cost_units"] <= assignment["max_cost_units"]
                or type(usage.get("input_bytes")) is not int
                or usage["input_bytes"] != input_bytes
                or type(usage.get("output_tokens")) is not int
                or not 0 <= usage["output_tokens"] <= assignment["max_output_tokens"]):
            raise _blocked("usage_invalid", 422)
        return result

    def _backend(self, request: Mapping[str, Any], reservation: _Reservation,
                 *, start: bool,
                 preserve_dispatch: bool = False) -> _Reservation:
        assignment = _copy(request["assignment"])
        try:
            if start:
                result = self.backend.execute_idempotent(
                    assignment, _copy(request["model_input"]),
                    execute_request_sha256=reservation.execute_request_sha256,
                    budgets=_copy(request["budgets"]),
                )
            else:
                result = self.backend.reconcile(
                    assignment,
                    execute_request_sha256=reservation.execute_request_sha256,
                )
            input_bytes = (len(_raw(request["model_input"]))
                           if "model_input" in request else self._stored_input_bytes(
                               reservation.execution_key))
            validated = self._validate_backend(
                result, request, execute_request_sha256=reservation.execute_request_sha256,
                input_bytes=input_bytes,
            )
            if preserve_dispatch and validated["status"] != "completed":
                # A dispatch owner may still be paused immediately before the
                # physical backend start.  No nonterminal observation can
                # prove that capacity or the dispatch barrier is safe to
                # release in that window.
                return _Reservation(
                    reservation.execution_key,
                    reservation.execute_request_sha256,
                    reservation.assignment_sha256,
                    reservation.generation,
                    "unknown",
                    False,
                )
            return self.ledger.transition(
                reservation, validated["status"],
                execution=validated["execution"], usage=validated["usage"],
            )
        except SubagentExecutorBlocked:
            if not preserve_dispatch:
                self.ledger.quarantine(reservation)
            raise
        except Exception:
            if not preserve_dispatch:
                self.ledger.quarantine(reservation)
            raise _blocked("backend_unavailable", 503, retryable=True) from None

    def _stored_input_bytes(self, execution_key: str) -> int:
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT input_bytes FROM subagent_executor_jobs WHERE execution_key=?",
                (execution_key,),
            ).fetchone()
        if row is None:
            raise _blocked("state_missing", 503, retryable=True)
        return int(row[0])

    def _run(self, request: dict, principal_sha256: str) -> _Reservation:
        assignment, operation = request["assignment"], request["operation"]
        assignment_sha256 = _sha(assignment)
        if operation == "execute":
            reservation = self.ledger.reserve_execute(
                principal_sha256=principal_sha256, request=request,
                assignment_sha256=assignment_sha256,
                input_bytes=len(_raw(request["model_input"])),
            )
            if reservation.status == "completed":
                return reservation
            if reservation.status == "dispatching" and not reservation.owner:
                # The original owner may still cross the physical-start boundary.
                # Never ask the backend for not_started or free capacity while
                # that owner remains capable of starting the exact request.
                raise _blocked("dispatch_in_progress", 425, retryable=True)
            return self._backend(request, reservation, start=reservation.owner)
        reservation = self.ledger.lookup(
            execution_key=assignment["execution_key"],
            principal_sha256=principal_sha256,
            assignment_sha256=assignment_sha256,
        )
        if reservation is None:
            return _Reservation(
                assignment["execution_key"], "0" * 64, assignment_sha256,
                0, "not_started", False,
            )
        if reservation.status in {"completed", "not_started"}:
            return reservation
        if reservation.status == "dispatching":
            # Read-only reconciliation can recover a backend-confirmed result
            # after a crash.  It must not release capacity for not_started,
            # because the original owner may still cross the start boundary.
            return self._backend(
                request, reservation, start=False, preserve_dispatch=True,
            )
        return self._backend(request, reservation, start=False)

    def _reply(self, request: Mapping[str, Any], result: _Reservation) -> bytes:
        response = {
            "schema": LAUNCHER.RESPONSE_SCHEMA,
            "operation": request["operation"],
            "launcher_id": self.launcher_id,
            "launcher_version": self.launcher_version,
            "executor_id": self.executor_id,
            "execution_key": request["assignment"]["execution_key"],
            "request_sha256": request["request_sha256"],
            "status": result.status,
            "execution": _copy(result.execution) if result.status == "completed" else None,
            "usage": _copy(result.usage) if result.status == "completed" else None,
        }
        return _raw(response)

    @staticmethod
    def _send(start_response, status: int, payload: bytes):
        phrases = {
            200: "OK", 202: "Accepted", 400: "Bad Request",
            401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 409: "Conflict", 413: "Content Too Large",
            415: "Unsupported Media Type", 422: "Unprocessable Content",
            425: "Too Early", 429: "Too Many Requests", 503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))), ("Cache-Control", "no-store"),
        ])
        return [payload]

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            if not isinstance(environ, Mapping) or environ.get("PATH_INFO") != PATH:
                raise _blocked("route_not_found", 404)
            if environ.get("REQUEST_METHOD") != "POST":
                raise _blocked("method_not_allowed", 405)
            scheme, server = environ.get("wsgi.url_scheme"), environ.get("SERVER_NAME")
            if scheme != "https" and not (
                    self.allow_loopback_http and scheme == "http"
                    and server in {"localhost", "127.0.0.1", "::1"}):
                raise _blocked("https_required", 400)
            principal = self._authenticate(environ)
            body = self._body(environ)
            request = self._request(environ, body)
            result = self._run(request, principal)
            payload = self._reply(request, result)
            return self._send(start_response, 202 if result.status == "running" else 200,
                              payload)
        except SubagentExecutorBlocked as error:
            payload = _raw({"error": {"code": error.code,
                                      "retryable": error.retryable}})
            return self._send(start_response, error.status, payload)
        except Exception:
            payload = _raw({"error": {"code": "subagent_executor.internal",
                                      "retryable": True}})
            return self._send(start_response, 503, payload)
