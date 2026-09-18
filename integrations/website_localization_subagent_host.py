#!/usr/bin/env python3
"""Durable provider-neutral host endpoint for isolated review subagents.

The application terminates the authenticated HTTPS review contract, assigns
reviewer identity from trusted host policy, launches one isolated reviewer and
stores the complete attested reply before returning it.  A model never receives
the HTTP envelope, host control, credentials, ledger, signing material or any
publication capability.
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
HTTP_PATH = ROOT / "integrations" / "website_localization_subagent_http.py"
COMMERCIAL_PATH = ROOT / "integrations" / "commercial_localization_profile.py"
PATH = "/v1/subagent-reviews"
MAX_BODY_BYTES = 4_500_000
MAX_RESPONSE_BYTES = 4_500_000
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BEARER = re.compile(r"^[A-Za-z0-9._~+/=-]{32,2048}$")
RESPONSE_REVIEW_SCHEMA = "translate-native.response-subagent-review.v1"
WEBSITE_REVIEW_SCHEMA = "translate-native.host-subagent-review.v1"
RESPONSE_NATIVE_SCHEMA = "translate-native.response-native-review.v1"
WEBSITE_RESPONSE_SCHEMA = "blun.website-localization-review.v2"
NATIVE_REWRITE_RESPONSE_SCHEMA = "translate-native.native-rewrite-review.v2"
NATIVE_PHASE = "target_native"
FIDELITY_PHASE = "source_fidelity"


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


HTTP = _load("blun_website_localization_subagent_http", HTTP_PATH)
COMMERCIAL = _load("blun_website_localization_host_commercial", COMMERCIAL_PATH)


class ReviewHostBlocked(RuntimeError):
    """Content-free HTTP failure at the trusted host boundary."""

    def __init__(self, code: str, status: int, *, retryable: bool = False):
        self.code, self.status, self.retryable = code, status, retryable
        super().__init__(code)


def _blocked(code: str, status: int, *, retryable: bool = False):
    return ReviewHostBlocked("review_host." + code, status, retryable=retryable)


def _launcher_blocked(error: Exception, fallback: str):
    """Preserve only an explicitly typed launcher's content-free failure."""
    code = getattr(error, "code", None)
    retryable = getattr(error, "retryable", None)
    if (getattr(error, "host_subagent_failure", False) is True
            and isinstance(code, str)
            and re.fullmatch(r"launcher\.[a-z0-9_.-]{1,118}", code)
            and type(retryable) is bool):
        return _blocked(
            code.replace(".", "_"), 503 if retryable else 422,
            retryable=retryable,
        )
    return _blocked(fallback, 503, retryable=True)


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


def _candidate(task: Mapping[str, Any]) -> str:
    value = task.get("input", {}).get("candidate")
    if not isinstance(value, str) or not value:
        raise _blocked("candidate_invalid", 400)
    return value


def _locale(task: Mapping[str, Any]) -> str:
    value = task.get("input", {}).get("target", {}).get("locale")
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise _blocked("locale_invalid", 400)
    return value


def task_policy_sha256(task: Mapping[str, Any]) -> str:
    """Digest trusted prompt/profile material while excluding reviewed prose.

    For a target-only review every field other than the candidate is pinned.
    Fidelity content is intentionally dynamic, but its system instruction,
    target profile, response schema and content type remain pinned.
    """
    task = _copy(task)
    phase, inputs = task.get("phase"), task.get("input")
    if not isinstance(inputs, dict):
        raise _blocked("task_invalid", 400)
    if phase == NATIVE_PHASE:
        projection = task
        projection["input"] = dict(inputs)
        projection["input"]["candidate"] = "<candidate>"
    elif phase == FIDELITY_PHASE:
        required = {"target", "content_type", "quality_profile", "response_schema"}
        if not required.issubset(inputs):
            raise _blocked("task_invalid", 400)
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
        raise _blocked("phase_invalid", 400)
    return _sha(projection)


def review_sequence_sha256(task: Mapping[str, Any], control: Mapping[str, Any]) -> str:
    """Bind both review phases to one exact candidate and target policy."""
    inputs = task.get("input")
    if not isinstance(inputs, Mapping):
        raise _blocked("task_invalid", 400)
    required = {
        "candidate", "target", "content_type", "quality_profile",
        "response_schema",
    }
    if not required.issubset(inputs):
        raise _blocked("task_invalid", 400)
    response_schema = inputs["response_schema"]
    if isinstance(response_schema, str):
        response_contract = response_schema
    elif (isinstance(response_schema, Mapping)
          and isinstance(response_schema.get("schema"), str)):
        response_contract = response_schema["schema"]
    else:
        raise _blocked("task_invalid", 400)
    if TOKEN.fullmatch(response_contract) is None:
        raise _blocked("task_invalid", 400)
    return _sha({
        "schema": task.get("schema"),
        "candidate_sha256": hashlib.sha256(
            _candidate(task).encode("utf-8")
        ).hexdigest(),
        "target": inputs["target"],
        "content_type": inputs["content_type"],
        "quality_profile": inputs["quality_profile"],
        "response_schema": response_contract,
        "commercial_quality_profile": inputs.get("commercial_quality_profile"),
        "provider_id": control.get("provider_id"),
        "host_policy_version": control.get("host_policy_version"),
        "model_id": control.get("model_id"),
        "model_version": control.get("model_version"),
    })


@dataclass(frozen=True)
class PinnedReviewRoute:
    """Trusted route selected without accepting model-chosen identity."""

    route_id: str
    schema: str
    phase: str
    target_locale: str
    content_type: str
    task_policy_sha256: str
    model_id: str
    model_version: str
    host_policy_version: str
    reviewer_agent_id: str
    reviewer_role: str
    max_timeout_seconds: int = 60
    max_output_tokens: int = 4096
    max_input_bytes: int = 2_000_000
    cost_unit: str = "deployment-cost-unit"
    max_cost_units: int = 100_000

    def __post_init__(self):
        for value in (
            self.route_id, self.schema, self.phase, self.target_locale,
            self.content_type, self.model_id, self.model_version,
            self.host_policy_version, self.reviewer_agent_id,
            self.reviewer_role, self.cost_unit,
        ):
            if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
                raise ValueError("review route contains an invalid identifier")
        if self.schema not in {RESPONSE_REVIEW_SCHEMA, WEBSITE_REVIEW_SCHEMA}:
            raise ValueError("review route schema is unsupported")
        if self.phase not in {NATIVE_PHASE, FIDELITY_PHASE}:
            raise ValueError("review route phase is unsupported")
        if self.schema == RESPONSE_REVIEW_SCHEMA and self.phase != NATIVE_PHASE:
            raise ValueError("ordinary responses only support native review")
        expected_role = (
            "target-native-reviewer" if self.phase == NATIVE_PHASE
            else "source-fidelity-reviewer"
        )
        if self.reviewer_role != expected_role:
            raise ValueError("review route role does not match its phase")
        if not isinstance(self.task_policy_sha256, str) or SHA256.fullmatch(
                self.task_policy_sha256) is None:
            raise ValueError("review route policy digest is invalid")
        if (type(self.max_timeout_seconds) is not int
                or not 1 <= self.max_timeout_seconds <= 60
                or type(self.max_output_tokens) is not int
                or not 128 <= self.max_output_tokens <= 32768
                or type(self.max_input_bytes) is not int
                or not 1024 <= self.max_input_bytes <= MAX_BODY_BYTES
                or type(self.max_cost_units) is not int
                or not 1 <= self.max_cost_units <= 1_000_000_000):
            raise ValueError("review route budgets are invalid")


class PinnedReviewPolicy:
    """Resolve only exact, pre-approved locale/profile/phase combinations."""

    def __init__(self, routes: list[PinnedReviewRoute]):
        if not isinstance(routes, list) or not routes:
            raise ValueError("at least one review route is required")
        indexed = {}
        for route in routes:
            if not isinstance(route, PinnedReviewRoute):
                raise TypeError("review routes must be pinned routes")
            key = (
                route.schema, route.phase, route.target_locale,
                route.content_type, route.task_policy_sha256,
            )
            if key in indexed:
                raise ValueError("duplicate review route")
            indexed[key] = route
        self._routes = indexed

    def resolve(self, task: Mapping[str, Any], control: Mapping[str, Any]):
        digest = task_policy_sha256(task)
        key = (
            task.get("schema"), task.get("phase"), _locale(task),
            task.get("input", {}).get("content_type"), digest,
        )
        route = self._routes.get(key)
        if route is None:
            raise _blocked("route_unavailable", 403)
        if any(control.get(name) != getattr(route, name) for name in (
                "model_id", "model_version", "host_policy_version")):
            raise _blocked("route_binding", 403)
        if (type(control.get("timeout_seconds")) is not int
                or control["timeout_seconds"] > route.max_timeout_seconds
                or type(control.get("max_output_tokens")) is not int
                or control["max_output_tokens"] > route.max_output_tokens):
            raise _blocked("budget_exceeded", 403)
        if control.get("inherit_context") is not False or control.get("tools") != [] \
                or control.get("max_delegation_depth") != 0:
            raise _blocked("isolation_required", 403)
        if route.schema == RESPONSE_REVIEW_SCHEMA and (
                control.get("reviewer_role") != route.reviewer_role):
            raise _blocked("role_binding", 403)
        return route


@dataclass(frozen=True)
class HostAssignment:
    execution_key: str
    route_id: str
    phase: str
    reviewer_role: str
    reviewer_agent_id: str
    reviewer_session_id: str
    model_id: str
    model_version: str
    host_policy_version: str
    deadline_seconds: int
    max_output_tokens: int
    max_input_bytes: int
    cost_unit: str
    max_cost_units: int


class ReviewLauncher(Protocol):
    def execute_idempotent(self, assignment: HostAssignment,
                           model_input: Mapping[str, Any], *,
                           deadline_seconds: int,
                           max_output_tokens: int) -> Mapping[str, Any]: ...

    def reconcile(self, assignment: HostAssignment,
                  model_input: Mapping[str, Any], *, deadline_seconds: int,
                  max_output_tokens: int) -> Mapping[str, Any]: ...


class HMACAttestationSigner:
    """Host-only attestation signer. Never pass this object to a launcher."""

    algorithm = "hmac-sha256"

    def __init__(self, secret: str, key_id: str):
        if (not isinstance(secret, str) or len(secret) < 43
                or not secret.isascii()):
            raise ValueError("attestation secret is invalid")
        self._secret = secret.encode("ascii")
        self.key_id = _identifier(key_id)

    def sign(self, payload: bytes) -> dict[str, str]:
        if not isinstance(payload, bytes):
            raise TypeError("attestation payload must be bytes")
        return {
            "schema": HTTP.ATTESTATION_SCHEMA,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "signature": hmac.new(self._secret, payload, hashlib.sha256).hexdigest(),
        }

    def __repr__(self):
        return f"{type(self).__name__}(key_id={self.key_id!r})"


@dataclass(frozen=True)
class _Lease:
    execution_key: str
    generation: int
    attempts: int
    replay: bytes | None


class SQLiteReviewLedger:
    """Small durable journal; no transaction remains open during model work."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 lease_seconds: int = 65, initialize_schema: bool = True):
        self.path = str(path)
        if not self.path or not callable(clock) or type(lease_seconds) is not int \
                or not 5 <= lease_seconds <= 300 \
                or type(initialize_schema) is not bool:
            raise ValueError("review ledger configuration is invalid")
        self.clock, self.lease_seconds = clock, lease_seconds
        self._schema_lock = threading.Lock()
        if initialize_schema:
            self._ensure_schema()
        else:
            self._verify_schema()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _ensure_schema(self):
        with self._schema_lock, self._connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS review_host_executions (
                    execution_key TEXT PRIMARY KEY,
                    principal_sha256 TEXT NOT NULL,
                    host_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    lease_expires REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    response_body BLOB,
                    receipt_sha256 TEXT,
                    candidate_sha256 TEXT NOT NULL,
                    sequence_sha256 TEXT NOT NULL,
                    target_locale TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    host_policy_version TEXT NOT NULL,
                    reviewer_agent_id TEXT,
                    reviewer_session_id TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS review_host_receipts
                    ON review_host_executions(receipt_sha256);
            """)

    def _verify_schema(self):
        expected = (
            "execution_key", "principal_sha256", "host_id", "request_sha256",
            "schema_name", "phase", "status", "generation", "lease_expires",
            "attempts", "response_body", "receipt_sha256", "candidate_sha256",
            "sequence_sha256", "target_locale", "provider_id",
            "host_policy_version", "reviewer_agent_id", "reviewer_session_id",
            "updated_at",
        )
        try:
            with self._schema_lock, self._connect() as connection:
                actual = tuple(
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(review_host_executions)"
                    )
                )
                index = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='review_host_receipts'"
                ).fetchone()
        except sqlite3.Error as error:
            raise ValueError("review ledger schema is unavailable") from error
        if actual != expected or index is None:
            raise ValueError("review ledger schema is unavailable")

    def reserve(self, *, execution_key: str, principal_sha256: str,
                host_id: str, request_sha256: str, schema: str, phase: str,
                candidate_sha256: str, target_locale: str, provider_id: str,
                host_policy_version: str, sequence_sha256: str) -> _Lease:
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_host_executions WHERE execution_key = ?",
                (execution_key,),
            ).fetchone()
            binding = (principal_sha256, host_id, request_sha256)
            if row is not None:
                if tuple(row[name] for name in (
                        "principal_sha256", "host_id", "request_sha256")) != binding:
                    connection.rollback()
                    raise _blocked("idempotency_conflict", 409)
                if row["status"] == "completed":
                    body = bytes(row["response_body"])
                    connection.commit()
                    return _Lease(execution_key, row["generation"], row["attempts"], body)
                if row["lease_expires"] > now:
                    connection.rollback()
                    raise _blocked("execution_running", 425, retryable=True)
                generation, attempts = row["generation"] + 1, row["attempts"]
                connection.execute(
                    "UPDATE review_host_executions SET generation=?, status='reserved', "
                    "lease_expires=?, updated_at=? WHERE execution_key=?",
                    (generation, now + self.lease_seconds, now, execution_key),
                )
            else:
                generation, attempts = 1, 0
                connection.execute("""
                    INSERT INTO review_host_executions(
                        execution_key, principal_sha256, host_id, request_sha256,
                        schema_name, phase, status, generation, lease_expires,
                        attempts, candidate_sha256, sequence_sha256,
                        target_locale, provider_id,
                        host_policy_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """, (
                    execution_key, principal_sha256, host_id, request_sha256,
                    schema, phase, generation, now + self.lease_seconds,
                    candidate_sha256, sequence_sha256, target_locale, provider_id,
                    host_policy_version, now,
                ))
            connection.commit()
            return _Lease(execution_key, generation, attempts, None)

    def mark_started(self, lease: _Lease):
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("""
                UPDATE review_host_executions
                   SET status='running', attempts=attempts+1,
                       lease_expires=?, updated_at=?
                 WHERE execution_key=? AND generation=? AND status='reserved'
            """, (now + self.lease_seconds, now, lease.execution_key,
                  lease.generation)).rowcount
            if changed != 1:
                connection.rollback()
                raise _blocked("lease_lost", 503, retryable=True)
            connection.commit()

    def mark_unknown(self, lease: _Lease):
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                UPDATE review_host_executions
                   SET status='unknown', lease_expires=0, updated_at=?
                 WHERE execution_key=? AND generation=? AND status='running'
            """, (now, lease.execution_key, lease.generation))
            connection.commit()

    def complete(self, lease: _Lease, *, body: bytes, receipt_sha256: str,
                 reviewer_agent_id: str, reviewer_session_id: str):
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("""
                UPDATE review_host_executions
                   SET status='completed', response_body=?, receipt_sha256=?,
                       reviewer_agent_id=?, reviewer_session_id=?,
                       lease_expires=0, updated_at=?
                 WHERE execution_key=? AND generation=?
                   AND status IN ('reserved','running')
            """, (
                body, receipt_sha256, reviewer_agent_id, reviewer_session_id,
                now, lease.execution_key, lease.generation,
            )).rowcount
            if changed != 1:
                connection.rollback()
                raise _blocked("lease_lost", 503, retryable=True)
            connection.commit()

    def native_predecessor(self, receipt_sha256: str):
        with self._connect() as connection:
            row = connection.execute("""
                SELECT * FROM review_host_executions
                 WHERE receipt_sha256=? AND status='completed'
            """, (receipt_sha256,)).fetchone()
            return dict(row) if row is not None else None

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM review_host_executions"
            ).fetchone()[0])


class ReviewHostApplication:
    """Strict synchronous WSGI endpoint over one durable review launcher."""

    def __init__(self, *, host_id: str, bearer_token: str,
                 signer: HMACAttestationSigner, policy: PinnedReviewPolicy,
                 ledger: SQLiteReviewLedger, launcher: ReviewLauncher,
                 allow_loopback_http: bool = False):
        self.host_id = _identifier(host_id)
        if not isinstance(bearer_token, str) or BEARER.fullmatch(bearer_token) is None:
            raise ValueError("bearer token is invalid")
        if not isinstance(signer, HMACAttestationSigner):
            raise TypeError("signer is invalid")
        if not isinstance(policy, PinnedReviewPolicy):
            raise TypeError("policy is invalid")
        if not isinstance(ledger, SQLiteReviewLedger):
            raise TypeError("ledger is invalid")
        if any(not callable(getattr(launcher, name, None))
               for name in ("execute_idempotent", "reconcile")):
            raise TypeError(
                "launcher must implement atomic execute_idempotent and reconcile"
            )
        if type(allow_loopback_http) is not bool:
            raise TypeError("allow_loopback_http must be boolean")
        self._bearer = bearer_token
        self.signer, self.policy, self.ledger, self.launcher = (
            signer, policy, ledger, launcher,
        )
        self.allow_loopback_http = allow_loopback_http

    def _authenticate(self, environ: Mapping[str, Any]) -> str:
        supplied = environ.get("HTTP_AUTHORIZATION")
        expected = "Bearer " + self._bearer
        if (not isinstance(supplied, str) or "," in supplied
                or not hmac.compare_digest(supplied, expected)):
            raise _blocked("authentication_rejected", 401)
        return hashlib.sha256(expected.encode("ascii")).hexdigest()

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> bytes:
        transfer = environ.get("HTTP_TRANSFER_ENCODING")
        length = environ.get("CONTENT_LENGTH")
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

    def _request(self, environ: Mapping[str, Any], body: bytes) -> tuple[dict, dict, dict]:
        content_type = environ.get("CONTENT_TYPE")
        if not isinstance(content_type, str) or content_type.lower().replace(" ", "") \
                != "application/json;charset=utf-8":
            raise _blocked("content_type", 415)
        try:
            text = body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError
            envelope = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise _blocked("json_invalid", 400) from None
        if (not isinstance(envelope, dict) or set(envelope) != {
                "schema", "host_id", "execution_key", "task", "control",
                "request_sha256",
        } or envelope.get("schema") != HTTP.REQUEST_SCHEMA
                or envelope.get("host_id") != self.host_id):
            raise _blocked("request_invalid", 400)
        unsigned = {key: envelope[key] for key in (
            "schema", "host_id", "execution_key", "task", "control",
        )}
        request_sha256 = _sha(unsigned)
        if envelope.get("request_sha256") != request_sha256:
            raise _blocked("request_digest", 400)
        task, control = HTTP._validate_task(envelope["task"], envelope["control"])
        if envelope["execution_key"] != control["execution_key"]:
            raise _blocked("execution_binding", 400)
        headers = {
            "HTTP_IDEMPOTENCY_KEY": control["execution_key"],
            "HTTP_X_SUBAGENT_EXECUTION_KEY": control["execution_key"],
            "HTTP_X_SUBAGENT_PHASE": control["phase"],
            "HTTP_X_SUBAGENT_REQUEST_SHA256": request_sha256,
        }
        if any(environ.get(name) != expected for name, expected in headers.items()):
            raise _blocked("header_binding", 400)
        return envelope, task, control

    @staticmethod
    def _assignment(route: PinnedReviewRoute, control: Mapping[str, Any]):
        key = control["execution_key"]
        session = "review:" + hashlib.sha256(
            (route.route_id + ":" + key).encode("utf-8")
        ).hexdigest()
        return HostAssignment(
            execution_key=key, route_id=route.route_id, phase=route.phase,
            reviewer_role=route.reviewer_role,
            reviewer_agent_id=route.reviewer_agent_id,
            reviewer_session_id=session,
            model_id=route.model_id, model_version=route.model_version,
            host_policy_version=route.host_policy_version,
            deadline_seconds=min(
                control["timeout_seconds"], route.max_timeout_seconds,
            ),
            max_output_tokens=min(
                control["max_output_tokens"], route.max_output_tokens,
            ),
            max_input_bytes=route.max_input_bytes,
            cost_unit=route.cost_unit, max_cost_units=route.max_cost_units,
        )

    @staticmethod
    def _model_task(task: Mapping[str, Any]) -> dict:
        """Construct the only object allowed to enter the reviewer context."""
        task = _copy(task)
        if task["phase"] == NATIVE_PHASE:
            return task
        allowed = {
            "source", "candidate", "target", "content_type", "content_guidance",
            "glossary", "protected_terms", "quality_profile", "response_schema",
            "audience", "tone_profile", "commercial_quality_profile",
            "commercial_profile", "commercial_review_evidence_contract",
        }
        inputs = task["input"]
        if not isinstance(inputs, dict) or not {
                "source", "candidate", "target", "content_type", "quality_profile",
                "response_schema",
        }.issubset(inputs):
            raise _blocked("task_invalid", 400)
        return {
            "schema": task["schema"], "phase": task["phase"],
            "system_instruction": task["system_instruction"],
            "input": {key: _copy(value) for key, value in inputs.items()
                      if key in allowed},
        }

    @staticmethod
    def _validate_execution(execution: Any, assignment: HostAssignment,
                            model_input: Mapping[str, Any]) -> dict:
        execution = _copy(execution)
        expected = {
            "response", "execution_key", "phase", "reviewer_role", "agent_id",
            "session_id", "model_id", "model_version", "inherit_context",
            "tools", "max_delegation_depth",
            "usage",
        }
        if (not isinstance(execution, dict) or set(execution) != expected
                or execution.get("execution_key") != assignment.execution_key
                or execution.get("phase") != assignment.phase
                or execution.get("reviewer_role") != assignment.reviewer_role
                or execution.get("agent_id") != assignment.reviewer_agent_id
                or execution.get("session_id") != assignment.reviewer_session_id
                or execution.get("model_id") != assignment.model_id
                or execution.get("model_version") != assignment.model_version
                or execution.get("inherit_context") is not False
                or execution.get("tools") != []
                or type(execution.get("max_delegation_depth")) is not int
                or execution.get("max_delegation_depth") != 0
                or not isinstance(execution.get("response"), dict)
                or not isinstance(execution.get("usage"), dict)):
            raise _blocked("execution_invalid", 422)
        usage = execution["usage"]
        if (set(usage) != {
                "execute_request_sha256", "cost_unit", "cost_units",
                "input_bytes", "output_tokens",
            }
                or not isinstance(usage.get("execute_request_sha256"), str)
                or SHA256.fullmatch(usage["execute_request_sha256"]) is None
                or usage.get("cost_unit") != assignment.cost_unit
                or type(usage.get("cost_units")) is not int
                or not 0 <= usage["cost_units"] <= assignment.max_cost_units
                or type(usage.get("input_bytes")) is not int
                or usage["input_bytes"] != len(_raw(model_input))
                or usage["input_bytes"] > assignment.max_input_bytes
                or type(usage.get("output_tokens")) is not int
                or not 0 <= usage["output_tokens"] <= assignment.max_output_tokens):
            raise _blocked("execution_usage_invalid", 422)
        return execution

    @staticmethod
    def _validate_review_response(response: Any, route: PinnedReviewRoute,
                                  task: Mapping[str, Any]) -> dict:
        """Validate the complete phase-specific model contract before attesting it."""
        response = _copy(response)
        if route.schema == RESPONSE_REVIEW_SCHEMA:
            expected = {
                "schema", "phase", "locale", "status", "confidence",
                "findings", "uncertainties",
            }
            if (not isinstance(response, dict) or set(response) != expected
                    or response.get("schema") != RESPONSE_NATIVE_SCHEMA
                    or response.get("phase") != route.phase
                    or response.get("locale") != route.target_locale
                    or response.get("status") not in {"PASS", "FAIL"}
                    or response.get("confidence") not in {"high", "low"}
                    or not isinstance(response.get("findings"), list)
                    or not isinstance(response.get("uncertainties"), list)
                    or any(not isinstance(item, str) or not item.strip()
                           for item in response.get("uncertainties", []))):
                raise _blocked("review_invalid", 422)
            for finding in response["findings"]:
                if (not isinstance(finding, dict)
                        or set(finding) != {
                            "code", "severity", "reason", "uncertainty",
                        }
                        or not isinstance(finding["code"], str)
                        or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,117}",
                                            finding["code"])
                        or finding["severity"] not in {
                            "minor", "major", "blocking",
                        }
                        or not isinstance(finding["reason"], str)
                        or not finding["reason"].strip()
                        or not isinstance(finding["uncertainty"], str)):
                    raise _blocked("review_invalid", 422)
            return response

        expected = {
            "schema", "phase", "locale", "status", "confidence",
            "blocking_defects", "major_defects",
        }
        inputs = task.get("input")
        response_schema = (
            inputs.get("response_schema", {}).get("schema")
            if isinstance(inputs, Mapping) else None
        )
        if response_schema == NATIVE_REWRITE_RESPONSE_SCHEMA:
            expected.add("uncertainties")
            if route.phase == NATIVE_PHASE:
                expected.add("holistic_assessment")
            if (not isinstance(response, dict) or set(response) != expected
                    or response.get("schema") != response_schema
                    or response.get("phase") != route.phase
                    or response.get("locale") != route.target_locale
                    or response.get("status") not in {"PASS", "FAIL"}
                    or response.get("confidence") not in {"high", "low"}
                    or not isinstance(response.get("uncertainties"), list)
                    or (route.phase == NATIVE_PHASE
                        and not isinstance(response.get("holistic_assessment"), dict))):
                raise _blocked("review_invalid", 422)
            defect_fields = {"severity", "class", "excerpt", "reason", "impact",
                             "revision_direction"}
            has_findings = False
            for field, severity in (("blocking_defects", "blocking"),
                                    ("major_defects", "major")):
                findings = response.get(field)
                if not isinstance(findings, list):
                    raise _blocked("review_invalid", 422)
                has_findings = has_findings or bool(findings)
                for finding in findings:
                    if (not isinstance(finding, dict) or set(finding) != defect_fields
                            or finding.get("severity") != severity
                            or any(not isinstance(finding.get(name), str)
                                   or not finding[name].strip()
                                   for name in defect_fields - {"severity"})
                            or (finding["excerpt"] not in _candidate(task)
                                and (route.phase != FIDELITY_PHASE
                                     or not isinstance(inputs.get("source"), Mapping)
                                     or not isinstance(inputs["source"].get("text"), str)
                                     or finding["excerpt"] not in inputs["source"]["text"]))):
                        raise _blocked("review_invalid", 422)
            uncertainty_fields = {"class", "reason", "evidence_needed"}
            for uncertainty in response["uncertainties"]:
                if (not isinstance(uncertainty, dict)
                        or set(uncertainty) != uncertainty_fields
                        or any(not isinstance(uncertainty.get(name), str)
                               or not uncertainty[name].strip()
                               for name in uncertainty_fields)):
                    raise _blocked("review_invalid", 422)
            holistic_pass = True
            if route.phase == NATIVE_PHASE:
                holistic = response["holistic_assessment"]
                if (set(holistic) != {
                        "reads_as_native_original", "reason", "repair_scope"}
                        or type(holistic.get("reads_as_native_original")) is not bool
                        or not isinstance(holistic.get("reason"), str)
                        or not holistic["reason"].strip()
                        or holistic.get("repair_scope") not in {
                            "none", "local", "passage", "whole_text"}
                        or (not holistic["reads_as_native_original"]
                            and (not has_findings or holistic["repair_scope"] not in {
                                "passage", "whole_text"}))
                        or (holistic["reads_as_native_original"]
                            and holistic["repair_scope"] in {"passage", "whole_text"})):
                    raise _blocked("review_invalid", 422)
                holistic_pass = (holistic["reads_as_native_original"]
                                 and holistic["repair_scope"] == "none")
            passing = (not has_findings and not response["uncertainties"]
                       and response["confidence"] == "high" and holistic_pass)
            if ((response["status"] == "PASS") != passing
                    or response["confidence"] == "low" and not response["uncertainties"]):
                raise _blocked("review_invalid", 422)
            return response
        commercial = (
            route.phase == FIDELITY_PHASE
            and isinstance(inputs, Mapping)
            and inputs.get("content_type") == "commercial"
        )
        if commercial:
            expected.add("commercial_review")
        if (not isinstance(response, dict) or set(response) != expected
                or response.get("schema") != WEBSITE_RESPONSE_SCHEMA
                or response.get("phase") != route.phase
                or response.get("locale") != route.target_locale
                or response.get("status") not in {"PASS", "FAIL"}
                or response.get("confidence") not in {"high", "low"}):
            raise _blocked("review_invalid", 422)
        has_findings = False
        for field in ("blocking_defects", "major_defects"):
            findings = response.get(field)
            if not isinstance(findings, list):
                raise _blocked("review_invalid", 422)
            has_findings = has_findings or bool(findings)
            for finding in findings:
                if (not isinstance(finding, dict)
                        or set(finding) != {"class", "excerpt", "reason"}
                        or any(not isinstance(finding[name], str)
                               or not finding[name].strip()
                               for name in ("class", "excerpt", "reason"))):
                    raise _blocked("review_invalid", 422)
        if (response["status"] == "PASS") != (not has_findings):
            raise _blocked("review_invalid", 422)
        if commercial:
            source = inputs.get("source")
            quality = inputs.get("commercial_quality_profile")
            if (not isinstance(source, Mapping)
                    or not isinstance(source.get("text"), str)
                    or not isinstance(quality, Mapping)
                    or not isinstance(inputs.get("commercial_profile"), str)
                    or not isinstance(
                        inputs.get("commercial_review_evidence_contract"), Mapping,
                    )):
                raise _blocked("review_invalid", 422)
            try:
                COMMERCIAL.validate_review(
                    response["commercial_review"], source["text"],
                    _candidate(task), inputs["commercial_profile"],
                    target_locale=route.target_locale,
                    commercial_quality_profile_version=quality.get("version"),
                    commercial_quality_profile_sha256=quality.get("sha256"),
                    allow_uncertain=True,
                )
            except COMMERCIAL.CommercialReviewBlocked:
                raise _blocked("review_invalid", 422) from None
        return response

    def _validate_predecessor(self, task: dict, control: dict,
                              assignment: HostAssignment,
                              principal_sha256: str, sequence_sha256: str):
        if task["phase"] != FIDELITY_PHASE:
            return
        digest = control.get("previous_receipt_sha256")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise _blocked("native_predecessor_missing", 409)
        previous = self.ledger.native_predecessor(digest)
        if (previous is None or previous["schema_name"] != WEBSITE_REVIEW_SCHEMA
                or previous["phase"] != NATIVE_PHASE
                or previous["principal_sha256"] != principal_sha256
                or previous["host_id"] != self.host_id
                or previous["candidate_sha256"] != hashlib.sha256(
                    _candidate(task).encode("utf-8")
                ).hexdigest()
                or previous["sequence_sha256"] != sequence_sha256
                or previous["target_locale"] != _locale(task)
                or previous["provider_id"] != control["provider_id"]
                or previous["host_policy_version"] != control["host_policy_version"]
                or previous["reviewer_agent_id"] == assignment.reviewer_agent_id
                or previous["reviewer_session_id"] == assignment.reviewer_session_id):
            raise _blocked("native_predecessor_invalid", 409)
        envelope = json.loads(bytes(previous["response_body"]).decode("utf-8"))
        response = envelope.get("result", {}).get("response", {})
        response_schema = task["input"].get("response_schema")
        expected_schema = (response_schema.get("schema")
                           if isinstance(response_schema, dict) else None)
        if (not isinstance(expected_schema, str)
                or response.get("schema") != expected_schema
                or response.get("status") != "PASS"
                or response.get("blocking_defects") != []
                or response.get("major_defects") != []
                or (expected_schema == NATIVE_REWRITE_RESPONSE_SCHEMA
                    and response.get("uncertainties") != [])
                or (expected_schema == NATIVE_REWRITE_RESPONSE_SCHEMA
                    and response.get("holistic_assessment", {}).get(
                        "reads_as_native_original") is not True)
                or (expected_schema == NATIVE_REWRITE_RESPONSE_SCHEMA
                    and response.get("holistic_assessment", {}).get(
                        "repair_scope") != "none")):
            raise _blocked("native_predecessor_invalid", 409)

    @staticmethod
    def _receipt(response: dict, control: dict, execution: dict) -> dict:
        fields = {
            "schema", "execution_key", "request_sha256", "task_sha256",
            "phase", "previous_receipt_sha256", "model_id", "model_version",
            "inherit_context", "tools", "max_delegation_depth",
        }
        if control["schema"] == RESPONSE_REVIEW_SCHEMA:
            fields |= {"reviewer_role", "assignment_id"}
        return {
            **{name: control[name] for name in fields},
            "response_sha256": _sha(response),
            "agent_id": execution["agent_id"],
            "session_id": execution["session_id"],
            "usage": execution["usage"],
        }

    def _result(self, envelope: dict, control: dict, execution: dict,
                route: PinnedReviewRoute, task: Mapping[str, Any]) -> tuple[bytes, str]:
        response = self._validate_review_response(
            execution["response"], route, task,
        )
        receipt = self._receipt(response, control, execution)
        result = {"response": response, "receipt": receipt}
        signed = {
            "schema": HTTP.ATTESTATION_PAYLOAD_SCHEMA,
            "host_id": self.host_id,
            "execution_key": control["execution_key"],
            "request_sha256": envelope["request_sha256"],
            "result_sha256": _sha(result),
            "completed": True,
        }
        reply = {
            "schema": HTTP.RESPONSE_SCHEMA,
            "host_id": self.host_id,
            "execution_key": control["execution_key"],
            "request_sha256": envelope["request_sha256"],
            "result": result,
            "attestation": self.signer.sign(_raw(signed)),
        }
        return _raw(reply), _sha(receipt)

    def _run(self, envelope: dict, task: dict, control: dict,
             principal_sha256: str) -> bytes:
        route = self.policy.resolve(task, control)
        assignment = self._assignment(route, control)
        model_task = self._model_task(task)
        creator_agent = control.get("creator_id")
        creator_session = control.get("creator_session_id")
        if control["schema"] == RESPONSE_REVIEW_SCHEMA:
            if (hashlib.sha256(assignment.reviewer_agent_id.encode()).hexdigest()
                    == control["creator_id_sha256"]
                    or hashlib.sha256(assignment.reviewer_session_id.encode()).hexdigest()
                    == control["creator_session_id_sha256"]):
                raise _blocked("self_review", 403)
        elif (assignment.reviewer_agent_id == creator_agent
              or assignment.reviewer_session_id == creator_session):
            raise _blocked("self_review", 403)
        sequence_sha256 = review_sequence_sha256(task, control)
        self._validate_predecessor(
            task, control, assignment, principal_sha256, sequence_sha256,
        )
        lease = self.ledger.reserve(
            execution_key=control["execution_key"],
            principal_sha256=principal_sha256, host_id=self.host_id,
            request_sha256=envelope["request_sha256"], schema=control["schema"],
            phase=control["phase"],
            candidate_sha256=hashlib.sha256(_candidate(task).encode("utf-8")).hexdigest(),
            target_locale=_locale(task), provider_id=control["provider_id"],
            host_policy_version=control["host_policy_version"],
            sequence_sha256=sequence_sha256,
        )
        if lease.replay is not None:
            return lease.replay
        if lease.attempts:
            try:
                reconciled = _copy(self.launcher.reconcile(
                    assignment, model_task,
                    deadline_seconds=assignment.deadline_seconds,
                    max_output_tokens=assignment.max_output_tokens,
                ))
            except Exception as error:
                raise _launcher_blocked(error, "reconcile_unavailable") from None
            if not isinstance(reconciled, dict) or set(reconciled) != {"status", "execution"}:
                raise _blocked("reconcile_invalid", 422)
            status = reconciled["status"]
            if status == "completed":
                execution = self._validate_execution(
                    reconciled["execution"], assignment, model_task,
                )
            elif status == "not_started" and reconciled["execution"] is None:
                execution = None
            elif status in {"running", "unknown", "cancel_pending"} \
                    and reconciled["execution"] is None:
                raise _blocked(
                    "reconcile_" + status,
                    503 if status == "running" else 422,
                    retryable=status == "running",
                )
            else:
                raise _blocked("reconcile_invalid", 422)
        else:
            execution = None
        if execution is None:
            self.ledger.mark_started(lease)
            try:
                # This call is deliberately named as a capability requirement:
                # the launcher must atomically deduplicate physical model starts
                # by assignment.execution_key.  A database generation fences an
                # old commit, but cannot by itself stop a paused old process from
                # crossing the external start boundary after its lease expires.
                launched = self.launcher.execute_idempotent(
                    assignment, model_task,
                    deadline_seconds=assignment.deadline_seconds,
                    max_output_tokens=assignment.max_output_tokens,
                )
                execution = self._validate_execution(
                    launched, assignment, model_task,
                )
            except ReviewHostBlocked:
                self.ledger.mark_unknown(lease)
                raise
            except Exception as error:
                self.ledger.mark_unknown(lease)
                raise _launcher_blocked(error, "launcher_unknown") from None
        try:
            body, receipt_sha256 = self._result(
                envelope, control, execution, route, task,
            )
        except ReviewHostBlocked:
            self.ledger.mark_unknown(lease)
            raise
        self.ledger.complete(
            lease, body=body, receipt_sha256=receipt_sha256,
            reviewer_agent_id=execution["agent_id"],
            reviewer_session_id=execution["session_id"],
        )
        return body

    @staticmethod
    def _send(start_response, status: int, payload: bytes):
        phrases = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 413: "Content Too Large", 415: "Unsupported Media Type",
            422: "Unprocessable Content",
            425: "Too Early", 503: "Service Unavailable",
        }
        start_response(f"{status} {phrases[status]}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
        ])
        return [payload]

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            if not isinstance(environ, Mapping) or environ.get("PATH_INFO") != PATH:
                raise _blocked("route_not_found", 404)
            if environ.get("REQUEST_METHOD") != "POST":
                raise _blocked("method_not_allowed", 405)
            scheme = environ.get("wsgi.url_scheme")
            server = environ.get("SERVER_NAME")
            if scheme != "https" and not (
                    self.allow_loopback_http and scheme == "http"
                    and server in {"localhost", "127.0.0.1", "::1"}):
                raise _blocked("https_required", 400)
            principal = self._authenticate(environ)
            body = self._body(environ)
            envelope, task, control = self._request(environ, body)
            response = self._run(envelope, task, control, principal)
            return self._send(start_response, 200, response)
        except ReviewHostBlocked as error:
            payload = _raw({
                "error": {"code": error.code, "retryable": error.retryable},
            })
            return self._send(start_response, error.status, payload)
        except Exception:
            payload = _raw({
                "error": {"code": "review_host.internal", "retryable": True},
            })
            return self._send(start_response, 503, payload)
