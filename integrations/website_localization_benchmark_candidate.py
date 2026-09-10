#!/usr/bin/env python3
"""Durable, attested acquisition of one benchmark candidate result.

The trusted host owns the SQLite connection, model adapter, evidence authority,
and operation guard. The first valid worker result is bound to one non-secret
route, benchmark policy, and canonical suite job. Every read reverifies the
stored digest, complete worker-result binding, and host attestation.
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


STORE_SCHEMA = "blun.website-localization-benchmark-candidate-store.v1"
ARTIFACT_SCHEMA = "blun.website-localization-benchmark-candidate.v1"
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
STORE_COLUMNS = (
    "acquisition_id", "route_id", "policy_sha256", "job_sha256",
    "artifact_json", "artifact_sha256", "created_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load candidate dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK = _load_module(
    "blun_website_localization_candidate_benchmark",
    _ROOT / "integrations" / "website_localization_benchmark.py",
)
_WORKER = _BENCHMARK._WORKER
_SUITE = _BENCHMARK._SUITE


class CandidateAcquisitionFailed(RuntimeError):
    """Content-free candidate failure understood by campaign orchestration."""

    benchmark_campaign_dependency_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("candidate error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("candidate retryability must be boolean")
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
        raise CandidateAcquisitionFailed(code, retryable=False) from None
    if not encoded or len(encoded) > MAX_ARTIFACT_BYTES:
        raise CandidateAcquisitionFailed(code, retryable=False)
    return encoded


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        raise CandidateAcquisitionFailed(
            "candidate.store.state_invalid", retryable=False,
        )
    try:
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError("stored JSON is too large")
        return json.loads(
            value, object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (UnicodeEncodeError, ValueError, RecursionError):
        raise CandidateAcquisitionFailed(
            "candidate.store.state_invalid", retryable=False,
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
        raise CandidateAcquisitionFailed(
            "candidate.store.time_invalid", retryable=False,
        )
    return float(value)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or _BENCHMARK.IDENTIFIER.fullmatch(value) is None:
        raise CandidateAcquisitionFailed(code, retryable=False)
    return value


def _binding(
    job_payload: Any,
    policy: Any,
    route_id: Any,
) -> tuple[tuple[str, str, str, str], dict[str, Any], Any]:
    route = _identifier(route_id, "candidate.store.route_invalid")
    try:
        validated_policy = _BENCHMARK._validate_policy(policy)
        job = _WORKER._validated_job(job_payload)
        _BENCHMARK._validate_candidate_job_binding(job, validated_policy)
        if job["target"]["locale"] not in validated_policy.required_locales:
            raise ValueError("locale is not required")
        _SUITE.case_for_job(job)
    except Exception:
        raise CandidateAcquisitionFailed(
            "candidate.store.binding_invalid", retryable=False,
        ) from None
    policy_sha256 = _hash_bytes(_json_bytes(
        asdict(validated_policy), "candidate.store.binding_invalid",
    ))
    job_sha256 = _hash_bytes(_json_bytes(
        job, "candidate.store.binding_invalid",
    ))
    acquisition_id = "benchmark-candidate:" + _hash_bytes(_json_bytes(
        {
            "schema": STORE_SCHEMA,
            "route_id": route,
            "policy_sha256": policy_sha256,
            "job_sha256": job_sha256,
        },
        "candidate.store.binding_invalid",
    ))
    return (
        (acquisition_id, route, policy_sha256, job_sha256),
        job,
        validated_policy,
    )


def _worker_result(
    result: Any,
    job: dict[str, Any],
) -> dict[str, Any]:
    try:
        return _BENCHMARK._validate_worker_result(job, result)
    except Exception:
        raise CandidateAcquisitionFailed(
            "candidate.store.result_invalid", retryable=False,
        ) from None


def _validate_artifact(
    artifact: Any,
    identity: tuple[str, str, str, str],
    job: dict[str, Any],
    policy: Any,
    evidence_authority: Any,
) -> dict[str, Any]:
    if not isinstance(artifact, dict) or set(artifact) != {
        "schema", "acquisition_id", "route_id", "policy_sha256",
        "job_sha256", "candidate_result", "attestation",
    }:
        raise CandidateAcquisitionFailed(
            "candidate.store.artifact_invalid", retryable=False,
        )
    unsigned = {key: value for key, value in artifact.items() if key != "attestation"}
    if (
        unsigned["schema"] != ARTIFACT_SCHEMA
        or tuple(unsigned[name] for name in STORE_COLUMNS[:4]) != identity
    ):
        raise CandidateAcquisitionFailed(
            "candidate.store.artifact_invalid", retryable=False,
        )
    unsigned["candidate_result"] = _worker_result(
        unsigned["candidate_result"], job,
    )
    try:
        _BENCHMARK._verify_attestation(
            unsigned, artifact["attestation"], policy, evidence_authority,
        )
    except _BENCHMARK.BenchmarkBlocked as error:
        retryable = error.code == "benchmark.attestation.verify_failed"
        code = (
            "candidate.store.attestation_unavailable"
            if retryable else "candidate.store.artifact_invalid"
        )
        raise CandidateAcquisitionFailed(code, retryable=retryable) from None
    except Exception:
        raise CandidateAcquisitionFailed(
            "candidate.store.artifact_invalid", retryable=False,
        ) from None
    verified = dict(unsigned)
    verified["attestation"] = artifact["attestation"]
    return json.loads(_json_bytes(
        verified, "candidate.store.artifact_invalid",
    ).decode("utf-8"))


def _create_artifact(
    identity: tuple[str, str, str, str],
    result: Any,
    job: dict[str, Any],
    policy: Any,
    evidence_authority: Any,
) -> dict[str, Any]:
    unsigned = {
        "schema": ARTIFACT_SCHEMA,
        "acquisition_id": identity[0],
        "route_id": identity[1],
        "policy_sha256": identity[2],
        "job_sha256": identity[3],
        "candidate_result": _worker_result(result, job),
    }
    try:
        artifact = _BENCHMARK._attest(unsigned, policy, evidence_authority)
    except _BENCHMARK.BenchmarkBlocked as error:
        retryable = error.code in {
            "benchmark.attestation.sign_failed",
            "benchmark.attestation.verify_failed",
        }
        code = (
            "candidate.store.attestation_unavailable"
            if retryable else "candidate.store.artifact_invalid"
        )
        raise CandidateAcquisitionFailed(code, retryable=retryable) from None
    return _validate_artifact(
        artifact, identity, job, policy, evidence_authority,
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise CandidateAcquisitionFailed(
            "candidate.store.external_transaction", retryable=False,
        )
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class CandidateAcquisitionStore:
    """Persist the first exact host-attested candidate for one bound route."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise CandidateAcquisitionFailed(
                "candidate.store.connection_invalid", retryable=False,
            )
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_candidate_acquisitions (
                    acquisition_id TEXT PRIMARY KEY,
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
                "PRAGMA table_info(benchmark_candidate_acquisitions)"
            ).fetchall()
        )
        if columns != STORE_COLUMNS:
            raise CandidateAcquisitionFailed(
                "candidate.store.schema_unsupported", retryable=False,
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
        evidence_authority: Any,
    ) -> dict[str, Any] | None:
        identity, job, validated_policy = _binding(job_payload, policy, route_id)
        row = self.connection.execute("""
            SELECT * FROM benchmark_candidate_acquisitions
            WHERE acquisition_id = ?
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
            raise CandidateAcquisitionFailed(
                "candidate.store.state_invalid", retryable=False,
            )
        artifact = _parse_json(row["artifact_json"])
        verified = _validate_artifact(
            artifact, identity, job, validated_policy, evidence_authority,
        )
        return verified["candidate_result"]

    def save(
        self,
        job_payload: Any,
        policy: Any,
        route_id: Any,
        result: Any,
        *,
        evidence_authority: Any,
        now: Any = None,
    ) -> dict[str, Any]:
        identity, job, validated_policy = _binding(job_payload, policy, route_id)
        artifact = _create_artifact(
            identity, result, job, validated_policy, evidence_authority,
        )
        artifact_json = _json_bytes(
            artifact, "candidate.store.artifact_invalid",
        ).decode("utf-8")
        values = identity + (
            artifact_json,
            _hash_text(artifact_json),
            _timestamp(now),
        )
        with _transaction(self.connection):
            self.connection.execute("""
                INSERT OR IGNORE INTO benchmark_candidate_acquisitions
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, values)
        stored = self.load(
            job_payload,
            policy,
            route_id,
            evidence_authority=evidence_authority,
        )
        if stored != artifact["candidate_result"]:
            raise CandidateAcquisitionFailed(
                "candidate.store.conflict", retryable=False,
            )
        return stored


class _GuardedAuthority:
    def __init__(self, authority: Any, guard: Callable[[], None]):
        self.authority = authority
        self.guard = guard

    def sign(self, payload: bytes):
        self.guard()
        return self.authority.sign(payload)

    def verify(self, payload: bytes, signature: Any):
        self.guard()
        return self.authority.verify(payload, signature)


class _GuardedProvider:
    def __init__(self, provider: Any, guard: Callable[[], None]):
        self.provider = provider
        self.guard = guard

    def invoke(self, request: Any):
        try:
            self.guard()
        except CandidateAcquisitionFailed as error:
            raise _WORKER.ProviderCallFailed(
                error.code, retryable=error.retryable,
            ) from None
        return self.provider.invoke(request)


def resolve_candidate_acquisition(
    store: CandidateAcquisitionStore,
    job_payload: Any,
    policy: Any,
    route_id: Any,
    assets: Any,
    provider: Any,
    *,
    evidence_authority: Any,
    operation_guard: Callable[[], Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Reuse an attested candidate or run and persist exactly one worker output."""
    if any(not callable(getattr(store, name, None)) for name in ("load", "save")):
        raise TypeError("store must provide load and save")
    if operation_guard is not None and not callable(operation_guard):
        raise TypeError("operation_guard must be callable")
    if any(
        not callable(getattr(evidence_authority, name, None))
        for name in ("sign", "verify")
    ):
        raise CandidateAcquisitionFailed(
            "candidate.authority.invalid", retryable=False,
        )

    def guard() -> None:
        if operation_guard is None:
            return
        try:
            operation_guard()
        except CandidateAcquisitionFailed:
            raise
        except Exception:
            raise CandidateAcquisitionFailed(
                "candidate.operation_guard_failed", retryable=True,
            ) from None

    guarded_authority = _GuardedAuthority(evidence_authority, guard)
    cached = store.load(
        job_payload,
        policy,
        route_id,
        evidence_authority=guarded_authority,
    )
    if cached is not None:
        return cached
    if not callable(getattr(provider, "invoke", None)):
        raise CandidateAcquisitionFailed(
            "candidate.provider.invalid", retryable=False,
        )
    try:
        result = _WORKER.run_localization_job(
            job_payload, assets, _GuardedProvider(provider, guard),
        )
    except _WORKER.LocalizationWorkerBlocked as error:
        code = "candidate.worker." + error.code
        if len(code) > 128:
            code = "candidate.worker.failed"
        raise CandidateAcquisitionFailed(
            code, retryable=error.retryable,
        ) from None
    except Exception:
        raise CandidateAcquisitionFailed(
            "candidate.worker.unexpected", retryable=True,
        ) from None
    return store.save(
        job_payload,
        policy,
        route_id,
        result,
        evidence_authority=guarded_authority,
        now=now,
    )
