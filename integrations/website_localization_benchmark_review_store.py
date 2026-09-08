#!/usr/bin/env python3
"""Durable, attested storage for one anonymous benchmark review pass.

The campaign lease remains the concurrency boundary. This store makes each
successful reviewer response immutable before the campaign proceeds to the
next ordered phase, so a later retry reuses the exact response instead of
asking the reviewer to judge the same blind variants again.
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


STORE_SCHEMA = "blun.website-localization-benchmark-review-store.v1"
ARTIFACT_SCHEMA = "blun.website-localization-benchmark-review-evidence.v1"
HEALTH_SCHEMA = "blun.website-localization-benchmark-review-health.v1"
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
REVIEW_ID = re.compile(r"^benchmark-review-[0-9a-f]{64}$")
STORE_COLUMNS = (
    "acquisition_id", "review_id", "route_id", "policy_sha256",
    "request_sha256", "artifact_json", "artifact_sha256", "created_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark review dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK = _load_module(
    "blun_website_localization_review_store_benchmark",
    _ROOT / "integrations" / "website_localization_benchmark.py",
)


class BenchmarkReviewEvidenceFailed(RuntimeError):
    """Content-free failure understood by benchmark orchestration."""

    benchmark_reviewer_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("benchmark review evidence code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("benchmark review evidence retryability is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class BenchmarkReviewEvidenceHealth:
    """Content-free integrity summary for one active review route and policy."""

    route_id: str
    status: str
    reasons: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": HEALTH_SCHEMA,
            "route_id": self.route_id,
            "status": self.status,
            "reasons": list(self.reasons),
            "counts": dict(self.counts),
        }


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
        raise BenchmarkReviewEvidenceFailed(code, retryable=False) from None
    if not encoded or len(encoded) > MAX_ARTIFACT_BYTES:
        raise BenchmarkReviewEvidenceFailed(code, retryable=False)
    return encoded


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.state_invalid", retryable=False,
        )
    try:
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError("stored review JSON is too large")
        return json.loads(
            value, object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (UnicodeEncodeError, ValueError, RecursionError):
        raise BenchmarkReviewEvidenceFailed(
            "review.store.state_invalid", retryable=False,
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
        raise BenchmarkReviewEvidenceFailed(
            "review.store.time_invalid", retryable=False,
        )
    return float(value)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or _BENCHMARK.IDENTIFIER.fullmatch(value) is None:
        raise BenchmarkReviewEvidenceFailed(code, retryable=False)
    return value


def _request_payload(value: Any) -> dict[str, Any]:
    try:
        payload = value.as_payload()
    except Exception:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.request_invalid", retryable=False,
        ) from None
    expected = {
        "schema", "review_id", "phase", "target_locale",
        "system_instruction", "input",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.request_invalid", retryable=False,
        )
    phase = payload.get("phase")
    expected_system = {
        "target_native": _BENCHMARK._NATIVE_SYSTEM,
        "source_fidelity": _BENCHMARK._FIDELITY_SYSTEM,
    }.get(phase)
    input_value = payload.get("input")
    if (
        payload.get("schema") != _BENCHMARK.BENCHMARK_SCHEMA
        or REVIEW_ID.fullmatch(payload.get("review_id", "")) is None
        or expected_system is None
        or payload.get("system_instruction") != expected_system
        or not isinstance(payload.get("target_locale"), str)
        or not isinstance(input_value, dict)
        or input_value.get("blind_id") is None
    ):
        raise BenchmarkReviewEvidenceFailed(
            "review.store.request_invalid", retryable=False,
        )
    try:
        return json.loads(_json_bytes(
            payload, "review.store.request_invalid",
        ).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BenchmarkReviewEvidenceFailed(
            "review.store.request_invalid", retryable=False,
        ) from None


def _binding(
    request: Any,
    policy: Any,
    route_id: Any,
) -> tuple[tuple[str, str, str, str, str], dict[str, Any], Any]:
    route = _identifier(route_id, "review.store.route_invalid")
    try:
        validated_policy = _BENCHMARK._validate_policy(policy)
    except Exception:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.policy_invalid", retryable=False,
        ) from None
    payload = _request_payload(request)
    if payload["target_locale"] not in validated_policy.required_locales:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.binding_invalid", retryable=False,
        )
    policy_sha256 = _hash_bytes(_json_bytes(
        asdict(validated_policy), "review.store.policy_invalid",
    ))
    request_sha256 = _hash_bytes(_json_bytes(
        payload, "review.store.request_invalid",
    ))
    acquisition_id = "benchmark-review-evidence:" + _hash_bytes(_json_bytes(
        {
            "schema": STORE_SCHEMA,
            "review_id": payload["review_id"],
            "route_id": route,
            "policy_sha256": policy_sha256,
            "request_sha256": request_sha256,
        },
        "review.store.binding_invalid",
    ))
    return (
        (
            acquisition_id, payload["review_id"], route,
            policy_sha256, request_sha256,
        ),
        payload,
        validated_policy,
    )


def _response(value: Any, request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BenchmarkReviewEvidenceFailed(
            "review.store.response_invalid", retryable=False,
        )
    try:
        response = json.loads(_json_bytes(
            dict(value), "review.store.response_invalid",
        ).decode("utf-8"))
        _BENCHMARK._validate_review(
            response,
            phase=request["phase"],
            locale=request["target_locale"],
            blind_id=request["input"]["blind_id"],
        )
    except BenchmarkReviewEvidenceFailed:
        raise
    except Exception:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.response_invalid", retryable=False,
        ) from None
    return response


def _validate_artifact(
    artifact: Any,
    identity: tuple[str, str, str, str, str],
    request: dict[str, Any],
    policy: Any,
    evidence_authority: Any,
) -> dict[str, Any]:
    expected = {
        "schema", "acquisition_id", "review_id", "route_id",
        "policy_sha256", "request_sha256", "response_sha256", "reviewer",
        "response", "attestation",
    }
    if not isinstance(artifact, dict) or set(artifact) != expected:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.artifact_invalid", retryable=False,
        )
    unsigned = {key: value for key, value in artifact.items() if key != "attestation"}
    if (
        unsigned["schema"] != ARTIFACT_SCHEMA
        or tuple(unsigned[name] for name in STORE_COLUMNS[:5]) != identity
        or unsigned["reviewer"] != {
            "id": policy.reviewer_id,
            "version": policy.reviewer_version,
        }
    ):
        raise BenchmarkReviewEvidenceFailed(
            "review.store.artifact_invalid", retryable=False,
        )
    unsigned["response"] = _response(unsigned["response"], request)
    if unsigned["response_sha256"] != _hash_bytes(_json_bytes(
        unsigned["response"], "review.store.artifact_invalid",
    )):
        raise BenchmarkReviewEvidenceFailed(
            "review.store.artifact_invalid", retryable=False,
        )
    try:
        _BENCHMARK._verify_attestation(
            unsigned, artifact["attestation"], policy, evidence_authority,
        )
    except _BENCHMARK.BenchmarkBlocked as error:
        retryable = error.code == "benchmark.attestation.verify_failed"
        code = (
            "review.store.attestation_unavailable"
            if retryable else "review.store.artifact_invalid"
        )
        raise BenchmarkReviewEvidenceFailed(code, retryable=retryable) from None
    except Exception:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.artifact_invalid", retryable=False,
        ) from None
    verified = dict(unsigned)
    verified["attestation"] = artifact["attestation"]
    return json.loads(_json_bytes(
        verified, "review.store.artifact_invalid",
    ).decode("utf-8"))


def _create_artifact(
    identity: tuple[str, str, str, str, str],
    response: Any,
    request: dict[str, Any],
    policy: Any,
    evidence_authority: Any,
) -> dict[str, Any]:
    validated_response = _response(response, request)
    unsigned = {
        "schema": ARTIFACT_SCHEMA,
        "acquisition_id": identity[0],
        "review_id": identity[1],
        "route_id": identity[2],
        "policy_sha256": identity[3],
        "request_sha256": identity[4],
        "response_sha256": _hash_bytes(_json_bytes(
            validated_response, "review.store.response_invalid",
        )),
        "reviewer": {
            "id": policy.reviewer_id,
            "version": policy.reviewer_version,
        },
        "response": validated_response,
    }
    try:
        artifact = _BENCHMARK._attest(unsigned, policy, evidence_authority)
    except _BENCHMARK.BenchmarkBlocked as error:
        retryable = error.code in {
            "benchmark.attestation.sign_failed",
            "benchmark.attestation.verify_failed",
        }
        code = (
            "review.store.attestation_unavailable"
            if retryable else "review.store.artifact_invalid"
        )
        raise BenchmarkReviewEvidenceFailed(code, retryable=retryable) from None
    return _validate_artifact(
        artifact, identity, request, policy, evidence_authority,
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise BenchmarkReviewEvidenceFailed(
            "review.store.external_transaction", retryable=False,
        )
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class BenchmarkReviewEvidenceStore:
    """Persist the first exact attested response for one bound review pass."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise BenchmarkReviewEvidenceFailed(
                "review.store.connection_invalid", retryable=False,
            )
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_review_evidence (
                    acquisition_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(review_id, route_id, policy_sha256)
                )
            """)
        self._verify_schema()

    def _verify_schema(self) -> None:
        columns = tuple(
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(benchmark_review_evidence)"
            ).fetchall()
        )
        if columns != STORE_COLUMNS:
            raise BenchmarkReviewEvidenceFailed(
                "review.store.schema_unsupported", retryable=False,
            )

    def health(
        self,
        policy: Any,
        route_id: Any,
        *,
        evidence_authority: Any,
        expected_passes: Any = (),
        now: Any = None,
    ) -> BenchmarkReviewEvidenceHealth:
        """Verify stored evidence without returning review text or changing state."""
        counts = {
            "total": 0,
            "scoped": 0,
            "historical": 0,
            "target_native": 0,
            "source_fidelity": 0,
            "required": 0,
            "matched": 0,
        }
        reasons: set[str] = set()
        safe_route = route_id if isinstance(route_id, str) else "invalid"
        try:
            safe_route = _identifier(route_id, "review.store.route_invalid")
            validated_policy = _BENCHMARK._validate_policy(policy)
            policy_sha256 = _hash_bytes(_json_bytes(
                asdict(validated_policy), "review.store.policy_invalid",
            ))
            checked_at = _timestamp(now)
            if self.connection.in_transaction:
                raise BenchmarkReviewEvidenceFailed(
                    "review.store.external_transaction", retryable=False,
                )
            if not isinstance(expected_passes, (tuple, list)):
                raise ValueError
            required: dict[str, tuple[str, str]] = {}
            for item in expected_passes:
                if not isinstance(item, Mapping) or set(item) != {
                    "phase", "request_sha256", "response_sha256",
                }:
                    raise ValueError
                phase = item["phase"]
                request_sha256 = item["request_sha256"]
                response_sha256 = item["response_sha256"]
                if (
                    phase not in _BENCHMARK.PHASES
                    or re.fullmatch(r"[0-9a-f]{64}", request_sha256 or "") is None
                    or re.fullmatch(r"[0-9a-f]{64}", response_sha256 or "") is None
                    or request_sha256 in required
                ):
                    raise ValueError
                required[request_sha256] = (phase, response_sha256)
            counts["required"] = len(required)
            observed: dict[str, tuple[str, str]] = {}
            self._verify_schema()
            rows = self.connection.execute(
                "SELECT * FROM benchmark_review_evidence "
                "ORDER BY route_id, policy_sha256, review_id"
            ).fetchall()
            counts["total"] = len(rows)
            for row in rows:
                if tuple(row.keys()) != STORE_COLUMNS:
                    raise ValueError
                scoped = (
                    row["route_id"] == safe_route
                    and row["policy_sha256"] == policy_sha256
                )
                if not scoped:
                    counts["historical"] += 1
                    continue
                counts["scoped"] += 1
                created_at = _timestamp(row["created_at"])
                if created_at > checked_at:
                    raise ValueError
                identity = tuple(row[name] for name in STORE_COLUMNS[:5])
                if (
                    not isinstance(row["artifact_json"], str)
                    or row["artifact_sha256"] != _hash_text(row["artifact_json"])
                    or REVIEW_ID.fullmatch(row["review_id"] or "") is None
                    or re.fullmatch(r"[0-9a-f]{64}", row["request_sha256"] or "") is None
                ):
                    raise ValueError
                artifact = _parse_json(row["artifact_json"])
                if _json_bytes(
                    artifact, "review.store.artifact_invalid",
                ).decode("utf-8") != row["artifact_json"]:
                    raise ValueError
                response = artifact.get("response") if isinstance(artifact, dict) else None
                if not isinstance(response, dict):
                    raise ValueError
                phase = response.get("phase")
                locale = response.get("target_locale")
                blind_id = response.get("blind_id")
                if (
                    phase not in _BENCHMARK.PHASES
                    or locale not in validated_policy.required_locales
                    or re.fullmatch(r"blind-[0-9a-f]{64}", blind_id or "") is None
                ):
                    raise ValueError
                request_stub = {
                    "phase": phase,
                    "target_locale": locale,
                    "input": {"blind_id": blind_id},
                }
                verified = _validate_artifact(
                    artifact, identity, request_stub, validated_policy,
                    evidence_authority,
                )
                response_sha256 = verified["response_sha256"]
                if row["request_sha256"] in observed:
                    raise ValueError
                observed[row["request_sha256"]] = (phase, response_sha256)
                counts[phase] += 1
            for request_sha256, expected in required.items():
                actual = observed.get(request_sha256)
                if actual is None:
                    reasons.add("review.store.required_missing")
                elif actual != expected:
                    reasons.add("review.store.required_mismatch")
                else:
                    counts["matched"] += 1
        except BenchmarkReviewEvidenceFailed as error:
            reasons = {error.code}
        except Exception:
            reasons = {"review.store.state_invalid"}
        status = "blocked" if reasons else "healthy"
        return BenchmarkReviewEvidenceHealth(
            route_id=safe_route,
            status=status,
            reasons=tuple(sorted(reasons)),
            counts=tuple(sorted(counts.items())),
        )

    def _row_for_identity(
        self, identity: tuple[str, str, str, str, str],
    ) -> sqlite3.Row | None:
        row = self.connection.execute("""
            SELECT * FROM benchmark_review_evidence
            WHERE review_id = ? AND route_id = ? AND policy_sha256 = ?
        """, (identity[1], identity[2], identity[3])).fetchone()
        if row is not None and tuple(row[name] for name in STORE_COLUMNS[:5]) != identity:
            raise BenchmarkReviewEvidenceFailed(
                "review.store.conflict", retryable=False,
            )
        return row

    def load(
        self,
        request: Any,
        policy: Any,
        route_id: Any,
        *,
        evidence_authority: Any,
    ) -> dict[str, Any] | None:
        identity, request_payload, validated_policy = _binding(
            request, policy, route_id,
        )
        row = self._row_for_identity(identity)
        if row is None:
            return None
        if (
            tuple(row.keys()) != STORE_COLUMNS
            or isinstance(row["created_at"], bool)
            or not isinstance(row["created_at"], (int, float))
            or not math.isfinite(float(row["created_at"]))
            or float(row["created_at"]) < 0
            or not isinstance(row["artifact_json"], str)
            or row["artifact_sha256"] != _hash_text(row["artifact_json"])
        ):
            raise BenchmarkReviewEvidenceFailed(
                "review.store.state_invalid", retryable=False,
            )
        artifact = _parse_json(row["artifact_json"])
        verified = _validate_artifact(
            artifact, identity, request_payload, validated_policy,
            evidence_authority,
        )
        return verified["response"]

    def save(
        self,
        request: Any,
        policy: Any,
        route_id: Any,
        response: Any,
        *,
        evidence_authority: Any,
        now: Any = None,
    ) -> dict[str, Any]:
        identity, request_payload, validated_policy = _binding(
            request, policy, route_id,
        )
        artifact = _create_artifact(
            identity, response, request_payload, validated_policy,
            evidence_authority,
        )
        artifact_json = _json_bytes(
            artifact, "review.store.artifact_invalid",
        ).decode("utf-8")
        values = identity + (
            artifact_json,
            _hash_text(artifact_json),
            _timestamp(now),
        )
        with _transaction(self.connection):
            self.connection.execute("""
                INSERT OR IGNORE INTO benchmark_review_evidence
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
        stored = self.load(
            request, policy, route_id, evidence_authority=evidence_authority,
        )
        if stored != artifact["response"]:
            raise BenchmarkReviewEvidenceFailed(
                "review.store.conflict", retryable=False,
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


class DurableBenchmarkReviewer:
    """Reuse a valid review response or obtain and attest it exactly once."""

    def __init__(
        self,
        *,
        store: BenchmarkReviewEvidenceStore,
        policy: Any,
        route_id: Any,
        reviewer: Any,
        evidence_authority: Any,
        operation_guard: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if any(not callable(getattr(store, name, None)) for name in ("load", "save")):
            raise BenchmarkReviewEvidenceFailed(
                "review.store.invalid", retryable=False,
            )
        if not callable(getattr(reviewer, "review", None)):
            raise BenchmarkReviewEvidenceFailed(
                "review.adapter.invalid", retryable=False,
            )
        if any(
            not callable(getattr(evidence_authority, name, None))
            for name in ("sign", "verify")
        ):
            raise BenchmarkReviewEvidenceFailed(
                "review.authority.invalid", retryable=False,
            )
        if operation_guard is not None and not callable(operation_guard):
            raise BenchmarkReviewEvidenceFailed(
                "review.operation_guard_invalid", retryable=False,
            )
        if not callable(clock):
            raise BenchmarkReviewEvidenceFailed(
                "review.clock_invalid", retryable=False,
            )
        self.store = store
        self.policy = _BENCHMARK._validate_policy(policy)
        self.route_id = _identifier(route_id, "review.store.route_invalid")
        self.reviewer = reviewer
        self.evidence_authority = evidence_authority
        self.operation_guard = operation_guard
        self.clock = clock

    def _guard(self) -> None:
        if self.operation_guard is None:
            return
        try:
            self.operation_guard()
        except BenchmarkReviewEvidenceFailed:
            raise
        except Exception:
            raise BenchmarkReviewEvidenceFailed(
                "review.operation_guard_failed", retryable=True,
            ) from None

    def review(self, request: Any) -> Mapping[str, Any]:
        guarded_authority = _GuardedAuthority(
            self.evidence_authority, self._guard,
        )
        cached = self.store.load(
            request,
            self.policy,
            self.route_id,
            evidence_authority=guarded_authority,
        )
        if cached is not None:
            return cached
        before = _request_payload(request)
        self._guard()
        try:
            response = self.reviewer.review(request)
        except Exception as error:
            if getattr(error, "benchmark_reviewer_failure", None) is True:
                raise
            retryable = getattr(error, "retryable", True)
            if not isinstance(retryable, bool):
                retryable = True
            raise BenchmarkReviewEvidenceFailed(
                "review.adapter_unavailable", retryable=retryable,
            ) from None
        after = _request_payload(request)
        if after != before:
            raise BenchmarkReviewEvidenceFailed(
                "review.request_mutated", retryable=False,
            )
        self._guard()
        return self.store.save(
            request,
            self.policy,
            self.route_id,
            response,
            evidence_authority=guarded_authority,
            now=self.clock(),
        )
