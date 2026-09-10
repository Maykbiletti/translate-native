#!/usr/bin/env python3
"""Durable, fail-closed storage for qualified-native benchmark references.

The trusted host owns the SQLite connection, receipt verifier, evidence
authority, and external artifact loader. The first fully verified reference is
bound to one route, benchmark policy, and suite job. Every read reverifies both
the artifact attestation and the qualified-human receipt before returning text.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator


STORE_SCHEMA = "blun.website-localization-native-reference-store.v1"
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
STORE_COLUMNS = (
    "reference_id", "route_id", "policy_sha256", "job_sha256",
    "artifact_json", "artifact_sha256", "created_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native-reference dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK = _load_module(
    "blun_website_localization_native_reference_benchmark",
    _ROOT / "integrations" / "website_localization_benchmark.py",
)
_WORKER = _BENCHMARK._WORKER
_SUITE = _BENCHMARK._SUITE


class NativeReferenceStoreFailed(RuntimeError):
    """Content-free failure understood by durable benchmark orchestration."""

    benchmark_campaign_dependency_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("native-reference error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("native-reference retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _json_bytes(value: Any, code: str) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise NativeReferenceStoreFailed(code, retryable=False) from None
    if not encoded or len(encoded) > MAX_ARTIFACT_BYTES:
        raise NativeReferenceStoreFailed(code, retryable=False)
    return encoded


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        raise NativeReferenceStoreFailed(
            "native_reference.store.state_invalid", retryable=False,
        )
    try:
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError("stored JSON is too large")
        return json.loads(
            value, object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (UnicodeEncodeError, ValueError, RecursionError):
        raise NativeReferenceStoreFailed(
            "native_reference.store.state_invalid", retryable=False,
        ) from None


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_text(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


def _timestamp(value: Any = None) -> float:
    value = time.time() if value is None else value
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise NativeReferenceStoreFailed(
            "native_reference.store.time_invalid", retryable=False,
        )
    return float(value)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or _BENCHMARK.IDENTIFIER.fullmatch(value) is None:
        raise NativeReferenceStoreFailed(code, retryable=False)
    return value


def _binding(
    job_payload: Any,
    policy: Any,
    route_id: Any,
) -> tuple[tuple[str, str, str, str], dict[str, Any], Any]:
    route = _identifier(
        route_id, "native_reference.store.route_invalid",
    )
    try:
        validated_policy = _BENCHMARK._validate_policy(policy)
        job = _WORKER._validated_job(job_payload)
        _BENCHMARK._validate_candidate_job_binding(job, validated_policy)
        if job["target"]["locale"] not in validated_policy.required_locales:
            raise ValueError("locale is not required")
        _SUITE.case_for_job(job)
    except Exception:
        raise NativeReferenceStoreFailed(
            "native_reference.store.binding_invalid", retryable=False,
        ) from None
    policy_sha256 = _hash_bytes(_json_bytes(
        asdict(validated_policy), "native_reference.store.binding_invalid",
    ))
    job_sha256 = _hash_bytes(_json_bytes(
        job, "native_reference.store.binding_invalid",
    ))
    reference_id = "native-reference:" + _hash_bytes(_json_bytes(
        {
            "schema": STORE_SCHEMA,
            "route_id": route,
            "policy_sha256": policy_sha256,
            "job_sha256": job_sha256,
        },
        "native_reference.store.binding_invalid",
    ))
    return (
        (reference_id, route, policy_sha256, job_sha256),
        job,
        validated_policy,
    )


def _validated_artifact(
    artifact: Any,
    job: dict[str, Any],
    policy: Any,
    native_reference_verifier: Any,
    evidence_authority: Any,
) -> dict[str, Any]:
    try:
        return _BENCHMARK._validate_native_reference(
            job,
            artifact,
            policy,
            native_reference_verifier,
            evidence_authority,
        )
    except _BENCHMARK.BenchmarkBlocked as error:
        retryable = error.code in {
            "benchmark.attestation.verify_failed",
            "benchmark.native_reference.verify_failed",
        }
        code = (
            "native_reference.store.verification_unavailable"
            if retryable else "native_reference.store.artifact_invalid"
        )
        raise NativeReferenceStoreFailed(code, retryable=retryable) from None
    except Exception:
        raise NativeReferenceStoreFailed(
            "native_reference.store.artifact_invalid", retryable=False,
        ) from None


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise NativeReferenceStoreFailed(
            "native_reference.store.external_transaction", retryable=False,
        )
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class NativeReferenceArtifactStore:
    """Persist the first exact verified native reference for one bound route."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise NativeReferenceStoreFailed(
                "native_reference.store.connection_invalid", retryable=False,
            )
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_native_references (
                    reference_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    job_sha256 TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(route_id, policy_sha256, job_sha256)
                )
            """)
        columns = tuple(
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(benchmark_native_references)"
            ).fetchall()
        )
        if columns != STORE_COLUMNS:
            raise NativeReferenceStoreFailed(
                "native_reference.store.schema_unsupported", retryable=False,
            )

    @staticmethod
    def identity(
        job_payload: Any, policy: Any, route_id: Any,
    ) -> tuple[str, str, str, str]:
        return _binding(job_payload, policy, route_id)[0]

    def load(
        self,
        job_payload: Any,
        policy: Any,
        route_id: Any,
        *,
        native_reference_verifier: Any,
        evidence_authority: Any,
    ) -> dict[str, Any] | None:
        identity, job, validated_policy = _binding(
            job_payload, policy, route_id,
        )
        row = self.connection.execute("""
            SELECT * FROM benchmark_native_references
            WHERE reference_id = ?
        """, (identity[0],)).fetchone()
        if row is None:
            return None
        if (
            tuple(row.keys()) != STORE_COLUMNS
            or tuple(row[name] for name in STORE_COLUMNS[:4]) != identity
            or isinstance(row["created_at"], bool)
            or not isinstance(row["created_at"], (int, float))
            or not math.isfinite(float(row["created_at"]))
            or float(row["created_at"]) < 0
            or not isinstance(row["artifact_json"], str)
            or row["artifact_sha256"] != _hash_text(row["artifact_json"])
        ):
            raise NativeReferenceStoreFailed(
                "native_reference.store.state_invalid", retryable=False,
            )
        artifact = _parse_json(row["artifact_json"])
        return _validated_artifact(
            artifact,
            job,
            validated_policy,
            native_reference_verifier,
            evidence_authority,
        )

    def save(
        self,
        job_payload: Any,
        policy: Any,
        route_id: Any,
        artifact: Any,
        *,
        native_reference_verifier: Any,
        evidence_authority: Any,
        now: Any = None,
    ) -> dict[str, Any]:
        identity, job, validated_policy = _binding(
            job_payload, policy, route_id,
        )
        verified = _validated_artifact(
            artifact,
            job,
            validated_policy,
            native_reference_verifier,
            evidence_authority,
        )
        artifact_json = _json_bytes(
            verified, "native_reference.store.artifact_invalid",
        ).decode("utf-8")
        values = identity + (
            artifact_json,
            _hash_text(artifact_json),
            _timestamp(now),
        )
        with _transaction(self.connection):
            self.connection.execute("""
                INSERT OR IGNORE INTO benchmark_native_references
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, values)
        stored = self.load(
            job_payload,
            policy,
            route_id,
            native_reference_verifier=native_reference_verifier,
            evidence_authority=evidence_authority,
        )
        if stored != verified:
            raise NativeReferenceStoreFailed(
                "native_reference.store.conflict", retryable=False,
            )
        return stored


def resolve_native_reference_artifact(
    store: NativeReferenceArtifactStore,
    job_payload: Any,
    policy: Any,
    route_id: Any,
    load_artifact: Callable[[], Any],
    *,
    native_reference_verifier: Any,
    evidence_authority: Any,
    operation_guard: Callable[[], Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Reuse verified state or load and persist one exact external artifact."""
    if any(not callable(getattr(store, name, None)) for name in ("load", "save")):
        raise TypeError("store must provide load and save")
    if not callable(load_artifact):
        raise TypeError("load_artifact must be callable")
    if operation_guard is not None and not callable(operation_guard):
        raise TypeError("operation_guard must be callable")

    def guard_operation() -> None:
        if operation_guard is None:
            return
        try:
            operation_guard()
        except Exception:
            raise NativeReferenceStoreFailed(
                "native_reference.operation_guard_failed", retryable=True,
            ) from None

    # A cache hit still invokes the configured receipt verifier. Guard it just
    # like an external vault lookup rather than assuming verification is local.
    guard_operation()
    cached = store.load(
        job_payload,
        policy,
        route_id,
        native_reference_verifier=native_reference_verifier,
        evidence_authority=evidence_authority,
    )
    if cached is not None:
        return cached
    guard_operation()
    try:
        artifact = load_artifact()
    except NativeReferenceStoreFailed:
        raise
    except Exception:
        raise NativeReferenceStoreFailed(
            "native_reference.loader_failed", retryable=True,
        ) from None
    guard_operation()
    return store.save(
        job_payload,
        policy,
        route_id,
        artifact,
        native_reference_verifier=native_reference_verifier,
        evidence_authority=evidence_authority,
        now=now,
    )
