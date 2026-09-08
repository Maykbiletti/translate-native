#!/usr/bin/env python3
"""Durable, provider-neutral execution for a complete benchmark campaign.

The trusted host owns the SQLite connection and every external adapter. One
runner invocation claims at most one exact suite-case/locale pair. Stored
results contain no source, candidate, baseline, reference, or reviewer prose.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol


SCHEMA_VERSION = 1
HEALTH_SCHEMA = "blun.website-localization-benchmark-campaign-health.v1"
MAX_ATTEMPTS = 20
MAX_LEASE_SECONDS = 86_400.0
MAX_STALE_SECONDS = 31_536_000.0
MAX_RESULT_BYTES = 2_000_000
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
HEALTH_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
STATUSES = ("pending", "leased", "retry_wait", "succeeded", "failed")
CAMPAIGN_COLUMNS = (
    "campaign_id", "policy_sha256", "suite_sha256", "work_count",
    "created_at", "updated_at",
)
WORK_COLUMNS = (
    "work_id", "campaign_id", "target_locale", "suite_case_key", "status",
    "attempts", "max_attempts", "next_attempt_at", "lease_owner",
    "lease_token", "lease_expires_at", "last_error_code",
    "last_error_detail_hash", "result_json", "result_sha256", "created_at",
    "updated_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark campaign dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK = _load_module(
    "blun_website_localization_campaign_benchmark",
    _ROOT / "integrations" / "website_localization_benchmark.py",
)
_PLANNER = _BENCHMARK._PLANNER
_SUITE = _BENCHMARK._SUITE


class BenchmarkCampaignBlocked(RuntimeError):
    """Content-free campaign failure safe for operational status."""

    def __init__(self, code: str):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("campaign error code is invalid")
        super().__init__(code)
        self.code = code


class BenchmarkCampaignDependencyFailed(RuntimeError):
    """Host-declared dependency failure with explicit retry ownership."""

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("campaign dependency error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("campaign dependency retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ClaimedBenchmarkCase:
    work_id: str
    campaign_id: str
    target_locale: str
    suite_case_key: str
    job_payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class BenchmarkCaseInputs:
    candidate_result: Mapping[str, Any]
    baseline_artifact: Mapping[str, Any]
    assets: Any
    native_reference_artifact: Mapping[str, Any]


@dataclass(frozen=True)
class BenchmarkCampaignOutcome:
    work_id: str
    target_locale: str
    suite_case_key: str
    status: str
    attempt: int
    max_attempts: int
    next_attempt_at: float
    error_code: str | None
    error_detail_hash: str | None
    result_sha256: str | None


@dataclass(frozen=True)
class BenchmarkCampaignHealth:
    campaign_id: str
    status: str
    reasons: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]
    work_count: int
    report_ready: bool
    last_progress_at: float | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": HEALTH_SCHEMA,
            "campaign_id": self.campaign_id,
            "status": self.status,
            "reasons": list(self.reasons),
            "counts": dict(self.counts),
            "work_count": self.work_count,
            "report_ready": self.report_ready,
            "last_progress_at": self.last_progress_at,
        }


class BenchmarkInputResolver(Protocol):
    def __call__(self, job_payload: dict[str, Any]) -> BenchmarkCaseInputs: ...


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise BenchmarkCampaignBlocked("benchmark.campaign.input_invalid") from error


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: Any = None) -> float:
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkCampaignBlocked("benchmark.campaign.time_invalid")
    value = float(value)
    if value < 0 or not math.isfinite(value):
        raise BenchmarkCampaignBlocked("benchmark.campaign.time_invalid")
    return value


def _duration(value: Any, *, allow_zero: bool = False) -> float:
    value = _timestamp(value)
    if (value < 0 if allow_zero else value <= 0) or value > MAX_LEASE_SECONDS:
        raise BenchmarkCampaignBlocked("benchmark.campaign.duration_invalid")
    return value


def _identifier(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 256 or "\x00" in value
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise BenchmarkCampaignBlocked("benchmark.campaign.identity_invalid")
    return value


def _policy_binding(policy: Any) -> dict[str, Any]:
    validated = _BENCHMARK._validate_policy(policy)
    return json.loads(_canonical_json(asdict(validated)))


def _campaign_identity(policy: Any) -> tuple[str, str]:
    policy_sha256 = _hash_json(_policy_binding(policy))
    return "benchmark-campaign-" + policy_sha256, policy_sha256


def _expected_work(policy: Any) -> tuple[tuple[str, str, str], ...]:
    campaign_id, policy_sha256 = _campaign_identity(policy)
    return tuple(
        (
            "benchmark-work-" + _hash_json({
                "campaign_id": campaign_id,
                "policy_sha256": policy_sha256,
                "suite_sha256": policy.suite_sha256,
                "target_locale": locale,
                "suite_case_key": case["key"],
            }),
            locale,
            case["key"],
        )
        for locale in policy.required_locales
        for case in _SUITE.manifest()["cases"]
    )


def _job_payload(policy: Any, locale: str, case_key: str) -> dict[str, Any]:
    cases = {item["key"]: item for item in _SUITE.manifest()["cases"]}
    case = cases.get(case_key)
    if case is None:
        raise BenchmarkCampaignBlocked("benchmark.campaign.state_invalid")
    try:
        return _PLANNER.plan_website_localization(
            source_id=case["source_id"],
            source_revision=case["source_revision"],
            source_text=case["source_text"],
            source_locale=case["source_locale"],
            content_type=case["content_type"],
            glossary_version=policy.candidate_glossary_version,
            policy_version=policy.candidate_policy_version,
            provider_id=policy.candidate_provider_id,
            model_id=policy.candidate_model_id,
            model_version=policy.candidate_model_version,
            software_version=policy.candidate_software_version,
            target_locales=[locale],
        ).jobs[0].as_payload()
    except Exception:
        raise BenchmarkCampaignBlocked("benchmark.campaign.state_invalid") from None


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise BenchmarkCampaignBlocked("benchmark.campaign.external_transaction")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class BenchmarkCampaignStore:
    """Dedicated SQLite campaign queue with exact policy and suite binding."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise BenchmarkCampaignBlocked("benchmark.campaign.connection_invalid")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise BenchmarkCampaignBlocked("benchmark.campaign.schema_unsupported")
        if version == 0:
            self._create_schema()
        self._verify_schema()

    def _create_schema(self) -> None:
        with _transaction(self.connection):
            self.connection.execute("""
                CREATE TABLE benchmark_campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    policy_sha256 TEXT NOT NULL,
                    suite_sha256 TEXT NOT NULL,
                    work_count INTEGER NOT NULL CHECK (work_count > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.connection.execute(f"""
                CREATE TABLE benchmark_campaign_work (
                    work_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    target_locale TEXT NOT NULL,
                    suite_case_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN {STATUSES}),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND {MAX_ATTEMPTS}),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_error_detail_hash TEXT,
                    result_json TEXT,
                    result_sha256 TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (campaign_id, target_locale, suite_case_key),
                    FOREIGN KEY (campaign_id) REFERENCES benchmark_campaigns (campaign_id)
                )
            """)
            self.connection.execute("""
                CREATE INDEX benchmark_campaign_ready
                ON benchmark_campaign_work
                (campaign_id, status, next_attempt_at, target_locale, suite_case_key)
            """)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _verify_schema(self) -> None:
        campaign = tuple(
            row["name"] for row in
            self.connection.execute("PRAGMA table_info(benchmark_campaigns)")
        )
        work = tuple(
            row["name"] for row in
            self.connection.execute("PRAGMA table_info(benchmark_campaign_work)")
        )
        if campaign != CAMPAIGN_COLUMNS or work != WORK_COLUMNS:
            raise BenchmarkCampaignBlocked("benchmark.campaign.schema_invalid")

    def create(self, policy: Any, *, max_attempts: int = 3, now: Any = None) -> str:
        policy = _BENCHMARK._validate_policy(policy)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise BenchmarkCampaignBlocked("benchmark.campaign.attempts_invalid")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise BenchmarkCampaignBlocked("benchmark.campaign.attempts_invalid")
        now = _timestamp(now)
        campaign_id, policy_sha256 = _campaign_identity(policy)
        expected = _expected_work(policy)
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM benchmark_campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if row is None:
                self.connection.execute("""
                    INSERT INTO benchmark_campaigns (
                        campaign_id, policy_sha256, suite_sha256, work_count,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    campaign_id, policy_sha256, policy.suite_sha256,
                    len(expected), now, now,
                ))
                self.connection.executemany("""
                    INSERT INTO benchmark_campaign_work (
                        work_id, campaign_id, target_locale, suite_case_key,
                        status, attempts, max_attempts, next_attempt_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """, (
                    (work_id, campaign_id, locale, case_key, max_attempts, now, now, now)
                    for work_id, locale, case_key in expected
                ))
            self._verify_binding_locked(policy, campaign_id)
            configured = self.connection.execute("""
                SELECT MIN(max_attempts) AS minimum, MAX(max_attempts) AS maximum
                FROM benchmark_campaign_work WHERE campaign_id = ?
            """, (campaign_id,)).fetchone()
            if (
                configured["minimum"] != max_attempts
                or configured["maximum"] != max_attempts
            ):
                raise BenchmarkCampaignBlocked(
                    "benchmark.campaign.attempts_mismatch",
                )
        return campaign_id

    def _verify_binding_locked(self, policy: Any, campaign_id: str) -> None:
        expected_id, policy_sha256 = _campaign_identity(policy)
        if campaign_id != expected_id:
            raise BenchmarkCampaignBlocked("benchmark.campaign.policy_mismatch")
        campaign = self.connection.execute(
            "SELECT * FROM benchmark_campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if campaign is None or (
            campaign["policy_sha256"] != policy_sha256
            or campaign["suite_sha256"] != policy.suite_sha256
            or campaign["work_count"] != len(_expected_work(policy))
        ):
            raise BenchmarkCampaignBlocked("benchmark.campaign.state_invalid")
        observed = {
            (row["work_id"], row["target_locale"], row["suite_case_key"])
            for row in self.connection.execute("""
                SELECT work_id, target_locale, suite_case_key
                FROM benchmark_campaign_work WHERE campaign_id = ?
            """, (campaign_id,))
        }
        if observed != set(_expected_work(policy)):
            raise BenchmarkCampaignBlocked("benchmark.campaign.state_invalid")

    def claim(
        self, policy: Any, campaign_id: str, worker_id: Any, *,
        now: Any = None, lease_seconds: Any = 300,
    ) -> ClaimedBenchmarkCase | None:
        policy = _BENCHMARK._validate_policy(policy)
        worker_id = _identifier(worker_id)
        now = _timestamp(now)
        lease_seconds = _duration(lease_seconds)
        with _transaction(self.connection):
            self._verify_binding_locked(policy, campaign_id)
            expired = self.connection.execute("""
                SELECT work_id, attempts, max_attempts
                FROM benchmark_campaign_work
                WHERE campaign_id = ? AND status = 'leased'
                  AND lease_expires_at <= ?
            """, (campaign_id, now)).fetchall()
            for row in expired:
                terminal = row["attempts"] >= row["max_attempts"]
                self.connection.execute("""
                    UPDATE benchmark_campaign_work
                    SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_code = 'benchmark.campaign.lease_expired',
                        updated_at = ? WHERE work_id = ?
                """, (
                    "failed" if terminal else "retry_wait", now, now,
                    row["work_id"],
                ))
            row = self.connection.execute("""
                SELECT * FROM benchmark_campaign_work
                WHERE campaign_id = ?
                  AND status IN ('pending', 'retry_wait')
                  AND next_attempt_at <= ? AND attempts < max_attempts
                ORDER BY target_locale, suite_case_key LIMIT 1
            """, (campaign_id, now)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            attempt = row["attempts"] + 1
            expires = now + lease_seconds
            changed = self.connection.execute("""
                UPDATE benchmark_campaign_work
                SET status = 'leased', attempts = ?, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?, updated_at = ?
                WHERE work_id = ? AND status IN ('pending', 'retry_wait')
            """, (
                attempt, worker_id, token, expires, now, row["work_id"],
            )).rowcount
            if changed != 1:
                raise BenchmarkCampaignBlocked("benchmark.campaign.claim_lost")
        return ClaimedBenchmarkCase(
            work_id=row["work_id"], campaign_id=campaign_id,
            target_locale=row["target_locale"],
            suite_case_key=row["suite_case_key"],
            job_payload=_job_payload(policy, row["target_locale"], row["suite_case_key"]),
            attempt=attempt, max_attempts=row["max_attempts"],
            lease_owner=worker_id, lease_token=token, lease_expires_at=expires,
        )

    def renew(self, claim: ClaimedBenchmarkCase, *, now: Any, lease_seconds: Any) -> ClaimedBenchmarkCase:
        now = _timestamp(now)
        lease_seconds = _duration(lease_seconds)
        expires = now + lease_seconds
        with _transaction(self.connection):
            self._assert_live_locked(claim, now)
            self.connection.execute("""
                UPDATE benchmark_campaign_work SET lease_expires_at = ?, updated_at = ?
                WHERE work_id = ?
            """, (expires, now, claim.work_id))
        return ClaimedBenchmarkCase(**{**asdict(claim), "lease_expires_at": expires})

    def _assert_live_locked(self, claim: Any, now: float) -> sqlite3.Row:
        if not isinstance(claim, ClaimedBenchmarkCase):
            raise BenchmarkCampaignBlocked("benchmark.campaign.claim_invalid")
        row = self.connection.execute(
            "SELECT * FROM benchmark_campaign_work WHERE work_id = ?",
            (claim.work_id,),
        ).fetchone()
        if row is None or (
            row["status"] != "leased"
            or row["campaign_id"] != claim.campaign_id
            or row["target_locale"] != claim.target_locale
            or row["suite_case_key"] != claim.suite_case_key
            or row["attempts"] != claim.attempt
            or row["lease_owner"] != claim.lease_owner
            or row["lease_token"] != claim.lease_token
            or row["lease_expires_at"] != claim.lease_expires_at
            or row["lease_expires_at"] <= now
        ):
            raise BenchmarkCampaignBlocked("benchmark.campaign.lease_lost")
        return row

    def complete(
        self, policy: Any, claim: ClaimedBenchmarkCase, result: Any, authority: Any,
        *, now: Any,
    ) -> BenchmarkCampaignOutcome:
        policy = _BENCHMARK._validate_policy(policy)
        if not isinstance(result, Mapping):
            raise BenchmarkCampaignBlocked("benchmark.campaign.result_invalid")
        signed = json.loads(_canonical_json(dict(result)))
        try:
            validated = _BENCHMARK._validated_case_result(signed, policy, authority)
        except _BENCHMARK.BenchmarkBlocked as error:
            raise BenchmarkCampaignBlocked("benchmark.campaign.result_invalid") from error
        if (
            validated["target_locale"] != claim.target_locale
            or validated["suite"]["case_key"] != claim.suite_case_key
        ):
            raise BenchmarkCampaignBlocked("benchmark.campaign.result_mismatch")
        encoded = _canonical_json(signed)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise BenchmarkCampaignBlocked("benchmark.campaign.result_invalid")
        now = _timestamp(now)
        with _transaction(self.connection):
            self._verify_binding_locked(policy, claim.campaign_id)
            self._assert_live_locked(claim, now)
            digest = _hash_text(encoded)
            self.connection.execute("""
                UPDATE benchmark_campaign_work
                SET status = 'succeeded', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    last_error_detail_hash = NULL, result_json = ?,
                    result_sha256 = ?, updated_at = ? WHERE work_id = ?
            """, (encoded, digest, now, claim.work_id))
        return self._outcome(claim)

    def transition_failure(
        self, policy: Any, claim: ClaimedBenchmarkCase, code: Any, *,
        retryable: bool, delay_seconds: Any = 0, detail: str | None = None,
        now: Any,
    ) -> BenchmarkCampaignOutcome:
        policy = _BENCHMARK._validate_policy(policy)
        code = _identifier(code)
        if ERROR_CODE.fullmatch(code) is None or not isinstance(retryable, bool):
            raise BenchmarkCampaignBlocked("benchmark.campaign.failure_invalid")
        delay = _duration(delay_seconds, allow_zero=True)
        now = _timestamp(now)
        if detail is not None and (
            not isinstance(detail, str) or "\x00" in detail
            or not unicodedata.is_normalized("NFC", detail)
        ):
            raise BenchmarkCampaignBlocked("benchmark.campaign.failure_invalid")
        with _transaction(self.connection):
            self._verify_binding_locked(policy, claim.campaign_id)
            row = self._assert_live_locked(claim, now)
            will_retry = retryable and row["attempts"] < row["max_attempts"]
            self.connection.execute("""
                UPDATE benchmark_campaign_work
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = ?, last_error_detail_hash = ?, updated_at = ?
                WHERE work_id = ?
            """, (
                "retry_wait" if will_retry else "failed",
                now + delay if will_retry else now,
                code, None if detail is None else _hash_text(detail), now,
                claim.work_id,
            ))
        return self._outcome(claim)

    def _outcome(self, claim: ClaimedBenchmarkCase) -> BenchmarkCampaignOutcome:
        row = self.connection.execute(
            "SELECT * FROM benchmark_campaign_work WHERE work_id = ?",
            (claim.work_id,),
        ).fetchone()
        return BenchmarkCampaignOutcome(
            work_id=row["work_id"], target_locale=row["target_locale"],
            suite_case_key=row["suite_case_key"], status=row["status"],
            attempt=row["attempts"], max_attempts=row["max_attempts"],
            next_attempt_at=row["next_attempt_at"],
            error_code=row["last_error_code"],
            error_detail_hash=row["last_error_detail_hash"],
            result_sha256=row["result_sha256"],
        )

    def status(self, policy: Any, campaign_id: str) -> dict[str, Any]:
        policy = _BENCHMARK._validate_policy(policy)
        with _transaction(self.connection):
            self._verify_binding_locked(policy, campaign_id)
            rows = self.connection.execute("""
                SELECT status, COUNT(*) AS count FROM benchmark_campaign_work
                WHERE campaign_id = ? GROUP BY status
            """, (campaign_id,)).fetchall()
            errors = self.connection.execute("""
                SELECT last_error_code, COUNT(*) AS count
                FROM benchmark_campaign_work
                WHERE campaign_id = ? AND last_error_code IS NOT NULL
                GROUP BY last_error_code ORDER BY last_error_code
            """, (campaign_id,)).fetchall()
        counts = {name: 0 for name in STATUSES}
        counts.update({row["status"]: row["count"] for row in rows})
        return {
            "campaign_id": campaign_id,
            "policy_sha256": _campaign_identity(policy)[1],
            "suite_sha256": policy.suite_sha256,
            "work_count": len(_expected_work(policy)),
            "counts": counts,
            "error_counts": {
                row["last_error_code"]: row["count"] for row in errors
            },
            "complete": counts["succeeded"] == len(_expected_work(policy)),
            "blocked": counts["failed"] > 0,
        }

    def health(
        self,
        policy: Any,
        campaign_id: str,
        authority: Any,
        *,
        now: Any,
        stale_after_seconds: Any = 3600,
    ) -> BenchmarkCampaignHealth:
        """Return a read-only, text-free health snapshot for one campaign."""
        now = _timestamp(now)
        stale_after_seconds = _timestamp(stale_after_seconds)
        if not 0 < stale_after_seconds <= MAX_STALE_SECONDS:
            raise BenchmarkCampaignBlocked("benchmark.campaign.stale_threshold_invalid")
        counts = {name: 0 for name in STATUSES}
        work_count = 0
        last_progress_at: float | None = None
        results: list[dict[str, Any]] = []
        reasons: set[str] = set()
        try:
            policy = _BENCHMARK._validate_policy(policy)
            if self.connection.in_transaction:
                raise BenchmarkCampaignBlocked(
                    "benchmark.campaign.external_transaction",
                )
            self.connection.execute("BEGIN")
            try:
                self._verify_binding_locked(policy, campaign_id)
                rows = self.connection.execute("""
                    SELECT * FROM benchmark_campaign_work
                    WHERE campaign_id = ?
                    ORDER BY target_locale, suite_case_key
                """, (campaign_id,)).fetchall()
                work_count = len(rows)
                due = False
                live_lease = False
                for row in rows:
                    status = row["status"]
                    if status not in counts:
                        raise ValueError
                    counts[status] += 1
                    attempts = row["attempts"]
                    maximum = row["max_attempts"]
                    if (
                        isinstance(attempts, bool)
                        or isinstance(maximum, bool)
                        or not isinstance(attempts, int)
                        or not isinstance(maximum, int)
                        or not 0 <= attempts <= maximum <= MAX_ATTEMPTS
                    ):
                        raise ValueError
                    created_at = _timestamp(row["created_at"])
                    updated_at = _timestamp(row["updated_at"])
                    next_attempt_at = _timestamp(row["next_attempt_at"])
                    if created_at > updated_at or updated_at > now:
                        raise ValueError
                    last_progress_at = max(
                        updated_at,
                        last_progress_at if last_progress_at is not None else updated_at,
                    )
                    lease_values = (
                        row["lease_owner"], row["lease_token"],
                        row["lease_expires_at"],
                    )
                    if status == "leased":
                        _identifier(row["lease_owner"])
                        _identifier(row["lease_token"])
                        expires_at = _timestamp(row["lease_expires_at"])
                        if attempts < 1:
                            raise ValueError
                        if expires_at <= now:
                            reasons.add("benchmark.campaign.lease_expired")
                            due = True
                        else:
                            live_lease = True
                    elif any(value is not None for value in lease_values):
                        raise ValueError
                    if status == "pending":
                        if attempts != 0 or row["last_error_code"] is not None:
                            raise ValueError
                        due = due or next_attempt_at <= now
                    elif status in {"retry_wait", "failed"}:
                        if attempts < 1:
                            raise ValueError
                        if status == "retry_wait":
                            if attempts >= maximum:
                                raise ValueError
                            due = due or next_attempt_at <= now
                    elif status == "succeeded" and row["last_error_code"] is not None:
                        raise ValueError
                    code = row["last_error_code"]
                    if code is not None:
                        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
                            raise ValueError
                        reasons.add("benchmark.campaign.error." + code)
                    elif status in {"retry_wait", "failed"}:
                        raise ValueError
                    detail_hash = row["last_error_detail_hash"]
                    if detail_hash is not None and re.fullmatch(
                        r"[0-9a-f]{64}", detail_hash,
                    ) is None:
                        raise ValueError
                    if status in {"pending", "succeeded"} and detail_hash is not None:
                        raise ValueError
                    result_json = row["result_json"]
                    result_sha256 = row["result_sha256"]
                    if status == "succeeded":
                        if (
                            not isinstance(result_json, str)
                            or result_sha256 != _hash_text(result_json)
                            or attempts < 1
                        ):
                            raise ValueError
                        result = json.loads(result_json)
                        if _canonical_json(result) != result_json:
                            raise ValueError
                        validated = _BENCHMARK._validated_case_result(
                            result, policy, authority,
                        )
                        if (
                            validated["target_locale"] != row["target_locale"]
                            or validated["suite"]["case_key"] != row["suite_case_key"]
                        ):
                            raise ValueError
                        results.append(result)
                    elif result_json is not None or result_sha256 is not None:
                        raise ValueError
                if work_count != len(_expected_work(policy)):
                    raise ValueError
            finally:
                self.connection.rollback()
        except Exception:
            return BenchmarkCampaignHealth(
                campaign_id=(
                    campaign_id
                    if isinstance(campaign_id, str) and re.fullmatch(
                        r"benchmark-campaign-[0-9a-f]{64}", campaign_id,
                    ) is not None
                    else "invalid"
                ),
                status="blocked",
                reasons=("benchmark.campaign.state_invalid",),
                counts=tuple(sorted(counts.items())),
                work_count=work_count,
                report_ready=False,
                last_progress_at=last_progress_at,
            )

        report_ready = counts["succeeded"] == work_count
        if counts["failed"]:
            reasons.add("benchmark.campaign.failed")
        if (
            not report_ready
            and not counts["failed"]
            and not live_lease
            and due
            and last_progress_at is not None
            and now - last_progress_at > stale_after_seconds
        ):
            reasons.add("benchmark.campaign.stalled")
        if report_ready:
            try:
                _BENCHMARK.summarize_benchmark(
                    policy, results, evidence_authority=authority,
                )
            except Exception:
                reasons.add("benchmark.campaign.report_invalid")
                report_ready = False
        blocking = counts["failed"] > 0 or "benchmark.campaign.report_invalid" in reasons
        status = "blocked" if blocking else ("degraded" if reasons else "healthy")
        return BenchmarkCampaignHealth(
            campaign_id=campaign_id,
            status=status,
            reasons=tuple(sorted(reasons)),
            counts=tuple(sorted(counts.items())),
            work_count=work_count,
            report_ready=report_ready,
            last_progress_at=last_progress_at,
        )

    def summarize(self, policy: Any, campaign_id: str, authority: Any) -> dict[str, Any]:
        policy = _BENCHMARK._validate_policy(policy)
        with _transaction(self.connection):
            self._verify_binding_locked(policy, campaign_id)
            rows = self.connection.execute("""
                SELECT status, result_json, result_sha256
                FROM benchmark_campaign_work WHERE campaign_id = ?
                ORDER BY target_locale, suite_case_key
            """, (campaign_id,)).fetchall()
            if any(row["status"] != "succeeded" for row in rows):
                raise BenchmarkCampaignBlocked("benchmark.campaign.incomplete")
            results = []
            for row in rows:
                if (
                    not isinstance(row["result_json"], str)
                    or row["result_sha256"] != _hash_text(row["result_json"])
                ):
                    raise BenchmarkCampaignBlocked("benchmark.campaign.state_invalid")
                try:
                    results.append(json.loads(row["result_json"]))
                except (TypeError, json.JSONDecodeError):
                    raise BenchmarkCampaignBlocked("benchmark.campaign.state_invalid") from None
        return _BENCHMARK.summarize_benchmark(
            policy, results, evidence_authority=authority,
        )


def _retry_delay(attempt: int, base: float, maximum: float) -> float:
    return min(maximum, base * (2 ** min(attempt - 1, 30)))


def run_next_benchmark_case(
    store: BenchmarkCampaignStore,
    policy: Any,
    campaign_id: str,
    worker_id: str,
    input_resolver: BenchmarkInputResolver,
    reviewer: Any,
    *,
    blinding_key: bytes,
    native_reference_verifier: Any,
    evidence_authority: Any,
    clock: Callable[[], float] = time.time,
    lease_seconds: Any = 300,
    retry_base_seconds: Any = 5,
    retry_max_seconds: Any = 3600,
    operation_guard: Callable[[float], Any] | None = None,
) -> BenchmarkCampaignOutcome | None:
    """Resolve and evaluate at most one durable benchmark case."""
    if not isinstance(store, BenchmarkCampaignStore):
        raise TypeError("store must be BenchmarkCampaignStore")
    if not callable(input_resolver) or not callable(clock):
        raise TypeError("input_resolver and clock must be callable")
    if operation_guard is not None and not callable(operation_guard):
        raise TypeError("operation_guard must be callable")
    lease_seconds = _duration(lease_seconds)
    retry_base_seconds = _duration(retry_base_seconds, allow_zero=True)
    retry_max_seconds = _duration(retry_max_seconds, allow_zero=True)
    if retry_base_seconds > retry_max_seconds:
        raise BenchmarkCampaignBlocked("benchmark.campaign.retry_invalid")
    claim = store.claim(
        policy, campaign_id, worker_id, now=_timestamp(clock()),
        lease_seconds=lease_seconds,
    )
    if claim is None:
        return None
    active = claim

    def renew(_: str) -> None:
        nonlocal active
        if operation_guard is not None:
            try:
                operation_guard(lease_seconds)
            except Exception:
                raise BenchmarkCampaignBlocked("benchmark.campaign.operation_guard_failed") from None
        active = store.renew(
            active, now=_timestamp(clock()), lease_seconds=lease_seconds,
        )

    try:
        renew("dependencies")
        inputs = input_resolver(claim.job_payload)
        if not isinstance(inputs, BenchmarkCaseInputs):
            raise BenchmarkCampaignDependencyFailed(
                "inputs_invalid", retryable=False,
            )
        renew("benchmark")
        result = _BENCHMARK.run_blind_benchmark_case(
            claim.job_payload,
            inputs.candidate_result,
            inputs.baseline_artifact,
            inputs.assets,
            policy,
            reviewer,
            blinding_key=blinding_key,
            native_reference_artifact=inputs.native_reference_artifact,
            native_reference_verifier=native_reference_verifier,
            evidence_authority=evidence_authority,
            progress_callback=renew,
        )
    except BenchmarkCampaignDependencyFailed as error:
        code = "benchmark.campaign.dependency." + error.code
        if len(code) > 128:
            code = "benchmark.campaign.dependency_failed"
        retryable = error.retryable
    except _BENCHMARK.BenchmarkBlocked as error:
        code = error.code
        retryable = code.startswith("reviewer.") or code in {
            "benchmark.attestation.sign_failed",
            "benchmark.attestation.verify_failed",
        }
    except BenchmarkCampaignBlocked:
        raise
    except Exception as error:
        if getattr(error, "benchmark_campaign_dependency_failure", None) is True:
            dependency_code = getattr(error, "code", None)
            dependency_retryable = getattr(error, "retryable", None)
            if (
                isinstance(dependency_code, str)
                and ERROR_CODE.fullmatch(dependency_code) is not None
                and isinstance(dependency_retryable, bool)
            ):
                code = "benchmark.campaign.dependency." + dependency_code
                if len(code) > 128:
                    code = "benchmark.campaign.dependency_failed"
                retryable = dependency_retryable
            else:
                code = "benchmark.campaign.unexpected"
                retryable = True
        else:
            code = "benchmark.campaign.unexpected"
            retryable = True
    else:
        return store.complete(
            policy, active, result, evidence_authority, now=_timestamp(clock()),
        )
    return store.transition_failure(
        policy, active, code, retryable=retryable,
        delay_seconds=_retry_delay(
            claim.attempt, retry_base_seconds, retry_max_seconds,
        ),
        now=_timestamp(clock()),
    )
