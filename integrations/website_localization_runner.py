#!/usr/bin/env python3
"""Lease-safe bridge between the localization queue and one worker attempt.

The runner processes at most one locale. It never publishes content or creates
a release. Provider and asset lookup stay host-owned and provider-neutral.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
MAX_DELAY_SECONDS = 86_400.0


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required runner dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_QUEUE = _load_module(
    "blun_website_localization_runner_queue",
    _ROOT / "integrations" / "website_localization_queue.py",
)
_WORKER = _load_module(
    "blun_website_localization_runner_worker",
    _ROOT / "integrations" / "website_localization_worker.py",
)


class ProviderResolver(Protocol):
    def __call__(self, job_payload: dict[str, Any]) -> Any: ...


class AssetsResolver(Protocol):
    def __call__(self, job_payload: dict[str, Any]) -> Any: ...


class RunnerDependencyFailed(RuntimeError):
    """Host-declared, content-free provider or asset lookup failure."""

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("runner dependency failure code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("runner dependency retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class RunOutcome:
    """Content-free result of one claim and transition."""

    job_id: str
    target_locale: str
    status: str
    attempt: int
    max_attempts: int
    next_attempt_at: float
    error_code: str | None
    error_detail_hash: str | None
    result_sha256: str | None


def _duration(name: str, value: Any, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if value != value or value in {float("inf"), float("-inf")}:
        raise ValueError(f"{name} must be a finite number")
    if (value < 0 if allow_zero else value <= 0) or value > MAX_DELAY_SECONDS:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _now(clock: Callable[[], float]) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("clock must return a finite timestamp")
    value = float(value)
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise ValueError("clock must return a finite timestamp")
    return value


def _outcome(claim: Any, status: Any) -> RunOutcome:
    return RunOutcome(
        job_id=status.job_id,
        target_locale=status.target_locale,
        status=status.status,
        attempt=claim.attempt,
        max_attempts=claim.max_attempts,
        next_attempt_at=status.next_attempt_at,
        error_code=status.last_error_code,
        error_detail_hash=status.last_error_detail_hash,
        result_sha256=status.result_sha256,
    )


def _retry_delay(attempt: int, base: float, maximum: float) -> float:
    return min(maximum, base * (2 ** min(attempt - 1, 30)))


def run_next_localization_job(
    queue: Any,
    worker_id: str,
    provider_resolver: ProviderResolver,
    assets_resolver: AssetsResolver,
    *,
    clock: Callable[[], float] = time.time,
    lease_seconds: float | int = 300,
    retry_base_seconds: float | int = 5,
    retry_max_seconds: float | int = 3600,
) -> RunOutcome | None:
    """Claim and execute at most one locale, then transition it atomically.

    A successful transition records only an unsigned worker result. It does not
    make the website version publishable. Failures retain stable codes and
    optional hashes only; provider prose and candidate text never enter status.
    """
    if not isinstance(queue, _QUEUE.LocalizationQueue):
        raise TypeError("queue must be LocalizationQueue")
    if not callable(provider_resolver) or not callable(assets_resolver):
        raise TypeError("provider and asset resolvers must be callable")
    if not callable(clock):
        raise TypeError("clock must be callable")
    lease_seconds = _duration("lease_seconds", lease_seconds)
    retry_base_seconds = _duration("retry_base_seconds", retry_base_seconds, allow_zero=True)
    retry_max_seconds = _duration("retry_max_seconds", retry_max_seconds, allow_zero=True)
    if retry_base_seconds > retry_max_seconds:
        raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")

    claim = queue.claim(
        worker_id,
        now=_now(clock),
        lease_seconds=lease_seconds,
    )
    if claim is None:
        return None

    active_claim = claim

    def renew(_: str) -> None:
        nonlocal active_claim
        active_claim = queue.renew(
            active_claim,
            now=_now(clock),
            lease_seconds=lease_seconds,
        )

    try:
        assets = assets_resolver(claim.payload)
        provider = provider_resolver(claim.payload)
        renew("dependencies")
        result = _WORKER.run_localization_job(
            claim.payload,
            assets,
            provider,
            progress_callback=renew,
        )
    except RunnerDependencyFailed as error:
        failure_code = "runner.dependency." + error.code
        if len(failure_code) > 128:
            failure_code = "runner.dependency.failure"
        retryable = error.retryable
        finding_detail = None
    except _WORKER.LocalizationWorkerBlocked as error:
        failure_code = error.code
        retryable = error.retryable
        finding_detail = ":".join(error.finding_hashes) or None
    except _QUEUE.LocalizationQueueBlocked:
        raise
    except Exception:
        failure_code = "runner.dependency.unexpected"
        retryable = True
        finding_detail = None
    else:
        status = queue.complete(active_claim, result, now=_now(clock))
        return _outcome(claim, status)

    transition_now = _now(clock)
    if retryable:
        status = queue.retry(
            active_claim,
            failure_code,
            error_detail=finding_detail,
            delay_seconds=_retry_delay(
                claim.attempt,
                retry_base_seconds,
                retry_max_seconds,
            ),
            now=transition_now,
        )
    else:
        status = queue.fail(
            active_claim,
            failure_code,
            error_detail=finding_detail,
            now=transition_now,
        )
    return _outcome(claim, status)
