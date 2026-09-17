#!/usr/bin/env python3
"""Durable execution boundary for isolated language-review subagents.

The facility is the server half of ``website_localization_subagent_backend_http``.
It authenticates before body access, atomically reserves one physical reviewer
execution, and accepts trusted execution metadata only from a pinned host
driver.  It owns neither Guard signing keys nor publication authority.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = ROOT / "integrations" / "website_localization_subagent_backend_http.py"
EXECUTOR_PATH = ROOT / "integrations" / "website_localization_subagent_executor.py"
PATH = "/v1/isolated-review-executions"
MAX_BODY_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 4_500_000
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BEARER = re.compile(r"^[A-Za-z0-9._~+/=-]{32,2048}$")
ACTIVE = {"dispatching", "running", "unknown", "cancel_pending"}
RESULT_STATUSES = {"completed", "not_started", "running", "unknown", "cancel_pending"}
ISOLATION = {"inherit_context": False, "tools": [], "max_delegation_depth": 0}
DRIVER_RESULT_FIELDS = {
    "status", "provider_execution_key", "provider_request_sha256",
    "actual_execution", "usage",
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


BACKEND = _load("blun_website_localization_subagent_backend_http", BACKEND_PATH)
EXECUTOR = _load("blun_website_localization_subagent_executor", EXECUTOR_PATH)


class SubagentFacilityBlocked(RuntimeError):
    """Content-free failure at the host-facility boundary."""

    def __init__(self, code: str, status: int, *, retryable: bool = False):
        self.code, self.status, self.retryable = code, status, retryable
        super().__init__(code)


def _blocked(code: str, status: int, *, retryable: bool = False):
    return SubagentFacilityBlocked(
        "subagent_facility." + code, status, retryable=retryable,
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


class HostSubagentDriver(Protocol):
    driver_id: str
    driver_version: str
    supports_atomic_idempotency: bool
    supports_reconcile: bool
    supports_hard_deadline: bool
    supports_isolated_context: bool

    def execute_idempotent(
        self, assignment: Mapping[str, Any], model_input: Mapping[str, Any], *,
        provider_execution_key: str, provider_request_sha256: str,
        budgets: Mapping[str, Any], isolation: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def reconcile(
        self, assignment: Mapping[str, Any], *,
        provider_execution_key: str, provider_request_sha256: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _Reservation:
    execution_key: str
    upstream_execute_sha256: str
    facility_execute_sha256: str
    assignment_sha256: str
    provider_execution_key: str
    provider_request_sha256: str
    generation: int
    owner_boot_id: str
    status: str
    owner: bool
    input_bytes: int
    execution: dict | None = None
    usage: dict | None = None


class SQLiteFacilityLedger:
    """Atomic facility idempotency, capacity and crash-fencing state."""

    COLUMNS = (
        "execution_key", "principal_sha256", "backend_id", "backend_version",
        "facility_id", "facility_version", "upstream_execute_sha256",
        "facility_execute_sha256", "assignment_sha256", "model_input_sha256",
        "provider_execution_key", "provider_request_sha256", "driver_id",
        "driver_version", "status", "generation", "owner_boot_id",
        "input_bytes", "max_output_tokens", "cost_unit", "max_cost_units",
        "execution_json", "usage_json", "created_at", "updated_at",
    )

    def __init__(self, path: Path, *, max_concurrent_executions: int,
                 boot_id: str, clock: Callable[[], float] = time.time,
                 initialize_schema: bool = True):
        if (not isinstance(path, Path)
                or type(max_concurrent_executions) is not int
                or not 1 <= max_concurrent_executions <= 256
                or not isinstance(boot_id, str) or SHA256.fullmatch(boot_id) is None
                or not callable(clock)):
            raise ValueError("facility ledger configuration is invalid")
        self.path = path
        self.max_concurrent_executions = max_concurrent_executions
        self.boot_id, self.clock = boot_id, clock
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
                CREATE TABLE IF NOT EXISTS subagent_facility_jobs (
                    execution_key TEXT PRIMARY KEY,
                    principal_sha256 TEXT NOT NULL,
                    backend_id TEXT NOT NULL,
                    backend_version TEXT NOT NULL,
                    facility_id TEXT NOT NULL,
                    facility_version TEXT NOT NULL,
                    upstream_execute_sha256 TEXT NOT NULL,
                    facility_execute_sha256 TEXT NOT NULL,
                    assignment_sha256 TEXT NOT NULL,
                    model_input_sha256 TEXT NOT NULL,
                    provider_execution_key TEXT NOT NULL,
                    provider_request_sha256 TEXT NOT NULL,
                    driver_id TEXT NOT NULL,
                    driver_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    owner_boot_id TEXT NOT NULL,
                    input_bytes INTEGER NOT NULL,
                    max_output_tokens INTEGER NOT NULL,
                    cost_unit TEXT NOT NULL,
                    max_cost_units INTEGER NOT NULL,
                    execution_json BLOB,
                    usage_json BLOB,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS subagent_facility_active
                    ON subagent_facility_jobs(status);
            """)

    def _verify_schema(self):
        try:
            with self._schema_lock, self._connect() as connection:
                actual = tuple(row[1] for row in connection.execute(
                    "PRAGMA table_info(subagent_facility_jobs)"
                ))
                index = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='subagent_facility_active'"
                ).fetchone()
        except sqlite3.Error as error:
            raise ValueError("facility ledger schema is unavailable") from error
        if actual != self.COLUMNS or index is None:
            raise ValueError("facility ledger schema is unavailable")

    @staticmethod
    def _reservation(row: sqlite3.Row | Mapping[str, Any], *, owner: bool = False):
        execution = json.loads(bytes(row["execution_json"])) \
            if row["execution_json"] is not None else None
        usage = json.loads(bytes(row["usage_json"])) \
            if row["usage_json"] is not None else None
        return _Reservation(
            row["execution_key"], row["upstream_execute_sha256"],
            row["facility_execute_sha256"], row["assignment_sha256"],
            row["provider_execution_key"], row["provider_request_sha256"],
            row["generation"], row["owner_boot_id"], row["status"], owner,
            row["input_bytes"], execution, usage,
        )

    @staticmethod
    def _binding(request: Mapping[str, Any], *, principal_sha256: str,
                 assignment_sha256: str, model_input_sha256: str,
                 provider_execution_key: str, provider_request_sha256: str,
                 driver_id: str, driver_version: str, input_bytes: int) -> tuple:
        budgets = request["budgets"]
        return (
            principal_sha256, request["backend_id"], request["backend_version"],
            request["facility_id"], request["facility_version"],
            request["execute_request_sha256"], request["request_sha256"],
            assignment_sha256, model_input_sha256, provider_execution_key,
            provider_request_sha256, driver_id, driver_version, input_bytes,
            budgets["max_output_tokens"], budgets["cost_unit"],
            budgets["max_cost_units"],
        )

    def reserve_execute(
        self, *, request: Mapping[str, Any], principal_sha256: str,
        assignment_sha256: str, model_input_sha256: str,
        provider_execution_key: str, provider_request_sha256: str,
        driver_id: str, driver_version: str, input_bytes: int,
    ) -> _Reservation:
        now, key = float(self.clock()), request["assignment"]["execution_key"]
        binding = self._binding(
            request, principal_sha256=principal_sha256,
            assignment_sha256=assignment_sha256,
            model_input_sha256=model_input_sha256,
            provider_execution_key=provider_execution_key,
            provider_request_sha256=provider_request_sha256,
            driver_id=driver_id, driver_version=driver_version,
            input_bytes=input_bytes,
        )
        columns = (
            "principal_sha256", "backend_id", "backend_version", "facility_id",
            "facility_version", "upstream_execute_sha256",
            "facility_execute_sha256", "assignment_sha256",
            "model_input_sha256", "provider_execution_key",
            "provider_request_sha256", "driver_id", "driver_version",
            "input_bytes", "max_output_tokens", "cost_unit", "max_cost_units",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM subagent_facility_jobs WHERE execution_key=?",
                (key,),
            ).fetchone()
            if row is not None:
                if tuple(row[name] for name in columns) != binding:
                    connection.rollback()
                    raise _blocked("idempotency_conflict", 409)
                if row["status"] != "not_started":
                    connection.commit()
                    return self._reservation(row)
                if self._active_count(connection) >= self.max_concurrent_executions:
                    connection.rollback()
                    raise _blocked("capacity", 429, retryable=True)
                generation = row["generation"] + 1
                connection.execute(
                    "UPDATE subagent_facility_jobs SET status='dispatching', "
                    "generation=?,owner_boot_id=?,execution_json=NULL,usage_json=NULL,"
                    "updated_at=? WHERE execution_key=?",
                    (generation, self.boot_id, now, key),
                )
                connection.commit()
                changed = dict(row)
                changed.update(
                    status="dispatching", generation=generation,
                    owner_boot_id=self.boot_id, execution_json=None,
                    usage_json=None,
                )
                return self._reservation(changed, owner=True)
            if self._active_count(connection) >= self.max_concurrent_executions:
                connection.rollback()
                raise _blocked("capacity", 429, retryable=True)
            connection.execute("""
                INSERT INTO subagent_facility_jobs VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,'dispatching',1,?,?,?,?,?,
                    NULL,NULL,?,?
                )
            """, (
                key, *binding[:13], self.boot_id, *binding[13:], now, now,
            ))
            connection.commit()
            return _Reservation(
                key, request["execute_request_sha256"], request["request_sha256"],
                assignment_sha256, provider_execution_key,
                provider_request_sha256, 1, self.boot_id, "dispatching", True,
                input_bytes,
            )

    @staticmethod
    def _active_count(connection) -> int:
        return int(connection.execute(
            "SELECT COUNT(*) FROM subagent_facility_jobs "
            "WHERE status IN ('dispatching','running','unknown','cancel_pending')"
        ).fetchone()[0])

    def lookup(
        self, *, request: Mapping[str, Any], principal_sha256: str,
        assignment_sha256: str,
    ) -> _Reservation | None:
        key = request["assignment"]["execution_key"]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subagent_facility_jobs WHERE execution_key=?", (key,),
            ).fetchone()
        if row is None:
            return None
        expected = (
            principal_sha256, request["backend_id"], request["backend_version"],
            request["facility_id"], request["facility_version"],
            request["execute_request_sha256"], assignment_sha256,
        )
        actual = tuple(row[name] for name in (
            "principal_sha256", "backend_id", "backend_version", "facility_id",
            "facility_version", "upstream_execute_sha256", "assignment_sha256",
        ))
        if actual != expected:
            raise _blocked("idempotency_conflict", 409)
        return self._reservation(row)

    def transition(
        self, reservation: _Reservation, status: str, *,
        execution: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> _Reservation:
        if status not in RESULT_STATUSES:
            raise _blocked("driver_result", 422)
        completed = status == "completed"
        if completed != isinstance(execution, Mapping) \
                or completed != isinstance(usage, Mapping):
            raise _blocked("driver_result", 422)
        now = float(self.clock())
        encoded_execution = _raw(execution) if completed else None
        encoded_usage = _raw(usage) if completed else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("""
                UPDATE subagent_facility_jobs
                   SET status=?,execution_json=?,usage_json=?,updated_at=?
                 WHERE execution_key=? AND generation=?
                   AND status IN ('dispatching','running','unknown','cancel_pending')
            """, (
                status, encoded_execution, encoded_usage, now,
                reservation.execution_key, reservation.generation,
            )).rowcount
            if changed != 1:
                connection.rollback()
                raise _blocked("generation_lost", 503, retryable=True)
            connection.commit()
        return _Reservation(
            reservation.execution_key, reservation.upstream_execute_sha256,
            reservation.facility_execute_sha256, reservation.assignment_sha256,
            reservation.provider_execution_key,
            reservation.provider_request_sha256, reservation.generation,
            reservation.owner_boot_id, status, False, reservation.input_bytes,
            _copy(execution) if completed else None,
            _copy(usage) if completed else None,
        )

    def quarantine(self, reservation: _Reservation):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE subagent_facility_jobs SET status='unknown',updated_at=? "
                "WHERE execution_key=? AND generation=? AND status!='completed'",
                (float(self.clock()), reservation.execution_key,
                 reservation.generation),
            )
            connection.commit()

    def recover_foreign_dispatches(self):
        """Fence dispatch owners from an earlier runtime boot."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE subagent_facility_jobs SET status='unknown',updated_at=? "
                "WHERE status='dispatching' AND owner_boot_id!=?",
                (float(self.clock()), self.boot_id),
            ).rowcount
            connection.commit()
        return int(changed)

    def count_active(self) -> int:
        with self._connect() as connection:
            return self._active_count(connection)


class SubagentFacilityApplication:
    """Auth-first V6.191 facility over one trusted host driver."""

    def __init__(
        self, *, backend_id: str, backend_version: str, facility_id: str,
        facility_version: str, bearer_token: str,
        policy: Any, ledger: SQLiteFacilityLedger,
        driver: HostSubagentDriver, allow_loopback_http: bool = False,
    ):
        self.backend_id = _identifier(backend_id)
        self.backend_version = _identifier(backend_version)
        self.facility_id = _identifier(facility_id)
        self.facility_version = _identifier(facility_version)
        if not isinstance(bearer_token, str) or BEARER.fullmatch(bearer_token) is None:
            raise ValueError("facility bearer token is invalid")
        if not isinstance(policy, EXECUTOR.PinnedExecutorPolicy):
            raise TypeError("facility route policy is invalid")
        if not isinstance(ledger, SQLiteFacilityLedger):
            raise TypeError("facility ledger is invalid")
        if (not isinstance(getattr(driver, "driver_id", None), str)
                or TOKEN.fullmatch(driver.driver_id) is None
                or not isinstance(getattr(driver, "driver_version", None), str)
                or TOKEN.fullmatch(driver.driver_version) is None
                or getattr(driver, "supports_atomic_idempotency", None) is not True
                or getattr(driver, "supports_reconcile", None) is not True
                or getattr(driver, "supports_hard_deadline", None) is not True
                or getattr(driver, "supports_isolated_context", None) is not True
                or any(not callable(getattr(driver, name, None))
                       for name in ("execute_idempotent", "reconcile"))):
            raise TypeError("facility driver lacks mandatory capabilities")
        if type(allow_loopback_http) is not bool:
            raise TypeError("allow_loopback_http must be boolean")
        self._bearer = bearer_token
        self.policy, self.ledger, self.driver = policy, ledger, driver
        self.allow_loopback_http = allow_loopback_http

    def _authenticate(self, environ: Mapping[str, Any]) -> str:
        supplied, expected = environ.get("HTTP_AUTHORIZATION"), "Bearer " + self._bearer
        if (not isinstance(supplied, str) or "," in supplied
                or not hmac.compare_digest(supplied, expected)):
            raise _blocked("authentication_rejected", 401)
        return hashlib.sha256(expected.encode("ascii")).hexdigest()

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> bytes:
        transfer, length = (
            environ.get("HTTP_TRANSFER_ENCODING"), environ.get("CONTENT_LENGTH"),
        )
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
            request = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise _blocked("json_invalid", 400) from None
        operation = request.get("operation") if isinstance(request, dict) else None
        expected = {
            "schema", "operation", "backend_id", "backend_version",
            "facility_id", "facility_version", "assignment",
            "execute_request_sha256", "isolation", "request_sha256",
        }
        if operation == "execute":
            expected |= {"model_input", "budgets"}
        if (not isinstance(request, dict) or set(request) != expected
                or request.get("schema") != BACKEND.REQUEST_SCHEMA
                or operation not in {"execute", "reconcile"}
                or request.get("backend_id") != self.backend_id
                or request.get("backend_version") != self.backend_version
                or request.get("facility_id") != self.facility_id
                or request.get("facility_version") != self.facility_version
                or request.get("isolation") != ISOLATION
                or not isinstance(request.get("execute_request_sha256"), str)
                or SHA256.fullmatch(request["execute_request_sha256"]) is None):
            raise _blocked("request_invalid", 400)
        unsigned = {key: request[key] for key in request if key != "request_sha256"}
        if request.get("request_sha256") != _sha(unsigned):
            raise _blocked("request_digest", 400)
        try:
            assignment = BACKEND._assignment(request.get("assignment"))
        except Exception:
            raise _blocked("assignment_invalid", 400) from None
        key = assignment["execution_key"]
        headers = {
            "HTTP_IDEMPOTENCY_KEY": key,
            "HTTP_X_SUBAGENT_FACILITY_OPERATION": operation,
            "HTTP_X_SUBAGENT_EXECUTION_KEY": key,
            "HTTP_X_SUBAGENT_REQUEST_SHA256": request["request_sha256"],
            "HTTP_X_UPSTREAM_EXECUTE_REQUEST_SHA256": (
                request["execute_request_sha256"]
            ),
        }
        if any(environ.get(name) != value for name, value in headers.items()):
            raise _blocked("header_binding", 400)
        if operation == "execute":
            try:
                request["model_input"] = BACKEND._model_input(
                    request.get("model_input"), assignment,
                )
            except Exception as error:
                code = ("native_source_isolation"
                        if getattr(error, "code", "").endswith(
                            ".native_source_isolation")
                        else "model_input_invalid")
                raise _blocked(code, 422) from None
            budgets = request.get("budgets")
            if (not isinstance(budgets, dict)
                    or set(budgets) != BACKEND.BUDGET_FIELDS
                    or any(budgets.get(name) != assignment[name] for name in (
                        "deadline_seconds", "max_output_tokens", "max_input_bytes",
                        "cost_unit", "max_cost_units",
                    ))
                    or type(budgets.get("max_concurrent_executions")) is not int
                    or budgets["max_concurrent_executions"]
                    != self.ledger.max_concurrent_executions
                    or len(_raw(request["model_input"]))
                    > assignment["max_input_bytes"]):
                raise _blocked("budget_binding", 403)
        try:
            self.policy.validate(
                assignment, request.get("model_input"), operation=operation,
            )
        except EXECUTOR.SubagentExecutorBlocked as error:
            raise _blocked(error.code.rsplit(".", 1)[-1], error.status,
                           retryable=error.retryable) from None
        request["assignment"] = assignment
        return request

    def _provider_identity(self, request: Mapping[str, Any]) -> tuple[str, str]:
        assignment = request["assignment"]
        execution_key = _sha({
            "schema": "translate-native.subagent-review-provider-execution.v1",
            "facility_id": self.facility_id,
            "facility_version": self.facility_version,
            "driver_id": self.driver.driver_id,
            "driver_version": self.driver.driver_version,
            "execution_key": assignment["execution_key"],
            "execute_request_sha256": request["execute_request_sha256"],
        })
        provider_request_sha256 = _sha({
            "schema": "translate-native.subagent-review-provider-request.v1",
            "provider_execution_key": execution_key,
            "assignment": assignment,
            "model_input": request["model_input"],
            "budgets": request["budgets"],
            "isolation": ISOLATION,
        })
        return execution_key, provider_request_sha256

    @staticmethod
    def _validate_driver(
        result: Any, request: Mapping[str, Any], reservation: _Reservation,
        *, start: bool,
    ) -> dict:
        result = _copy(result)
        if (not isinstance(result, dict) or set(result) != DRIVER_RESULT_FIELDS
                or result.get("status") not in RESULT_STATUSES
                or result.get("provider_execution_key")
                != reservation.provider_execution_key
                or result.get("provider_request_sha256")
                != reservation.provider_request_sha256
                or start and result.get("status") == "not_started"):
            raise _blocked("driver_result", 422)
        completed = result["status"] == "completed"
        if (completed != isinstance(result.get("actual_execution"), dict)
                or completed != isinstance(result.get("usage"), dict)):
            raise _blocked("driver_result", 422)
        if not completed:
            return {"status": result["status"], "execution": None, "usage": None}
        assignment = request["assignment"]
        actual, usage = result["actual_execution"], result["usage"]
        if (set(actual) != {
                "response", "phase", "reviewer_role", "agent_id", "session_id",
                "model_id", "model_version", "inherit_context", "tools",
                "max_delegation_depth",
            } or not isinstance(actual.get("response"), dict)
                or actual.get("phase") != assignment["phase"]
                or actual.get("reviewer_role") != assignment["reviewer_role"]
                or actual.get("agent_id") != assignment["reviewer_agent_id"]
                or actual.get("session_id") != assignment["reviewer_session_id"]
                or actual.get("model_id") != assignment["model_id"]
                or actual.get("model_version") != assignment["model_version"]
                or actual.get("inherit_context") is not False
                or actual.get("tools") != []
                or actual.get("max_delegation_depth") != 0):
            raise _blocked("actual_execution_invalid", 422)
        if (set(usage) != {
                "provider_request_sha256", "cost_unit", "cost_units",
                "input_bytes", "output_tokens",
            } or usage.get("provider_request_sha256")
                != reservation.provider_request_sha256
                or usage.get("cost_unit") != assignment["cost_unit"]
                or type(usage.get("cost_units")) is not int
                or not 0 <= usage["cost_units"] <= assignment["max_cost_units"]
                or type(usage.get("input_bytes")) is not int
                or usage["input_bytes"] != reservation.input_bytes
                or type(usage.get("output_tokens")) is not int
                or not 0 <= usage["output_tokens"]
                <= assignment["max_output_tokens"]):
            raise _blocked("usage_invalid", 422)
        execution = {
            "response": _copy(actual["response"]),
            "execution_key": assignment["execution_key"],
            "phase": actual["phase"],
            "reviewer_role": actual["reviewer_role"],
            "agent_id": actual["agent_id"],
            "session_id": actual["session_id"],
            "model_id": actual["model_id"],
            "model_version": actual["model_version"],
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
        }
        outer_usage = {
            "execute_request_sha256": request["execute_request_sha256"],
            "cost_unit": usage["cost_unit"],
            "cost_units": usage["cost_units"],
            "input_bytes": usage["input_bytes"],
            "output_tokens": usage["output_tokens"],
        }
        return {"status": "completed", "execution": execution,
                "usage": outer_usage}

    def _driver_call(
        self, request: Mapping[str, Any], reservation: _Reservation, *,
        start: bool, preserve_dispatch: bool = False,
    ) -> _Reservation:
        try:
            if start:
                result = self.driver.execute_idempotent(
                    _copy(request["assignment"]), _copy(request["model_input"]),
                    provider_execution_key=reservation.provider_execution_key,
                    provider_request_sha256=reservation.provider_request_sha256,
                    budgets=_copy(request["budgets"]), isolation=_copy(ISOLATION),
                )
            else:
                result = self.driver.reconcile(
                    _copy(request["assignment"]),
                    provider_execution_key=reservation.provider_execution_key,
                    provider_request_sha256=reservation.provider_request_sha256,
                )
            validated = self._validate_driver(
                result, request, reservation, start=start,
            )
            if preserve_dispatch and validated["status"] != "completed":
                return _Reservation(
                    reservation.execution_key,
                    reservation.upstream_execute_sha256,
                    reservation.facility_execute_sha256,
                    reservation.assignment_sha256,
                    reservation.provider_execution_key,
                    reservation.provider_request_sha256,
                    reservation.generation, reservation.owner_boot_id,
                    "unknown", False, reservation.input_bytes,
                )
            return self.ledger.transition(
                reservation, validated["status"],
                execution=validated["execution"], usage=validated["usage"],
            )
        except SubagentFacilityBlocked:
            if not preserve_dispatch:
                self.ledger.quarantine(reservation)
            raise
        except Exception as error:
            if not preserve_dispatch:
                self.ledger.quarantine(reservation)
            retryable = getattr(error, "retryable", True)
            if type(retryable) is not bool:
                retryable = True
            raise _blocked(
                "driver_unavailable" if retryable else "driver_rejected",
                503 if retryable else 422, retryable=retryable,
            ) from None

    def _run(self, request: dict, principal_sha256: str) -> _Reservation:
        assignment = request["assignment"]
        assignment_sha256 = _sha(assignment)
        if request["operation"] == "execute":
            provider_key, provider_digest = self._provider_identity(request)
            input_bytes = len(_raw(request["model_input"]))
            reservation = self.ledger.reserve_execute(
                request=request, principal_sha256=principal_sha256,
                assignment_sha256=assignment_sha256,
                model_input_sha256=_sha(request["model_input"]),
                provider_execution_key=provider_key,
                provider_request_sha256=provider_digest,
                driver_id=self.driver.driver_id,
                driver_version=self.driver.driver_version,
                input_bytes=input_bytes,
            )
            if reservation.status == "completed":
                return reservation
            if reservation.status == "dispatching" and not reservation.owner:
                raise _blocked("dispatch_in_progress", 425, retryable=True)
            if not reservation.owner:
                return reservation
            return self._driver_call(request, reservation, start=True)
        reservation = self.ledger.lookup(
            request=request, principal_sha256=principal_sha256,
            assignment_sha256=assignment_sha256,
        )
        if reservation is None:
            return _Reservation(
                assignment["execution_key"], request["execute_request_sha256"],
                "0" * 64, assignment_sha256, "0" * 64, "0" * 64, 0,
                self.ledger.boot_id, "not_started", False, 0,
            )
        if reservation.status in {"completed", "not_started"}:
            return reservation
        if reservation.status == "dispatching":
            return self._driver_call(
                request, reservation, start=False, preserve_dispatch=True,
            )
        return self._driver_call(request, reservation, start=False)

    def _reply(self, request: Mapping[str, Any], result: _Reservation) -> bytes:
        response = {
            "schema": BACKEND.RESPONSE_SCHEMA,
            "operation": request["operation"],
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "facility_id": self.facility_id,
            "facility_version": self.facility_version,
            "execution_key": request["assignment"]["execution_key"],
            "execute_request_sha256": request["execute_request_sha256"],
            "request_sha256": request["request_sha256"],
            "status": result.status,
            "execution": (_copy(result.execution)
                          if result.status == "completed" else None),
            "usage": (_copy(result.usage)
                      if result.status == "completed" else None),
        }
        return _raw(response)

    @staticmethod
    def _send(start_response, status: int, payload: bytes):
        phrases = {
            200: "OK", 202: "Accepted", 400: "Bad Request",
            401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 409: "Conflict",
            413: "Content Too Large", 415: "Unsupported Media Type",
            422: "Unprocessable Content", 425: "Too Early",
            429: "Too Many Requests", 503: "Service Unavailable",
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
            request = self._request(environ, self._body(environ))
            result = self._run(request, principal)
            payload = self._reply(request, result)
            status = 202 if result.status in {"running", "unknown", "cancel_pending"} else 200
            return self._send(start_response, status, payload)
        except SubagentFacilityBlocked as error:
            payload = _raw({"error": {
                "code": error.code, "retryable": error.retryable,
            }})
            return self._send(start_response, error.status, payload)
        except Exception:
            payload = _raw({"error": {
                "code": "subagent_facility.internal", "retryable": True,
            }})
            return self._send(start_response, 503, payload)

    def close(self):
        closer = getattr(self.driver, "close", None)
        if callable(closer):
            closer()
