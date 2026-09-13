#!/usr/bin/env python3
"""Safe composition root for the durable terminal-notification receiver."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sqlite3
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load terminal receiver dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_RECEIVER = _load_module(
    "blun_website_localization_terminal_receiver_runtime_dependency",
    _ROOT
    / "integrations"
    / "website_localization_cms_terminal_notification_receiver.py",
)


class DurableTerminalReceiverRuntimeBlocked(RuntimeError):
    """Private composition failure that must not cross the HTTP boundary."""


@dataclass(frozen=True)
class DurableTerminalReceiverRuntimeHealth:
    status: str
    state: str
    received: int
    processing_counts: dict[str, int]
    processing_due: int
    expired_leases: int
    failed: int


def _database_path(value: Any) -> str:
    try:
        result = os.fspath(value)
    except TypeError as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "database path is invalid"
        ) from error
    if isinstance(result, bytes):
        raise DurableTerminalReceiverRuntimeBlocked(
            "database path must be Unicode"
        )
    if (
        not isinstance(result, str)
        or not result
        or "\x00" in result
        or result.startswith("file:")
    ):
        raise DurableTerminalReceiverRuntimeBlocked("database path is invalid")
    if result == ":memory:":
        return result
    if (
        os.name != "posix"
        or not os.path.isabs(result)
        or os.path.normpath(result) != result
    ):
        raise DurableTerminalReceiverRuntimeBlocked(
            "database path must be a canonical absolute POSIX path"
        )
    return result


def _validate_database_parent(database_path: str) -> None:
    current = os.path.dirname(database_path)
    first = True
    while True:
        try:
            current_stat = os.lstat(current)
        except OSError as error:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database directory is unavailable"
            ) from error
        mode = stat.S_IMODE(current_stat.st_mode)
        sticky_root_directory = (
            current_stat.st_uid == 0
            and bool(mode & stat.S_ISVTX)
            and bool(mode & 0o002)
        )
        if (
            not stat.S_ISDIR(current_stat.st_mode)
            or current_stat.st_uid not in {0, os.geteuid()}
            or (first and current_stat.st_uid != os.geteuid())
            or (mode & 0o022 and not sticky_root_directory)
        ):
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database directory is not private"
            )
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        first = False


def _validate_database_file(
    database_path: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    try:
        database_stat = os.lstat(database_path)
    except OSError as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver database file is unavailable"
        ) from error
    identity = (database_stat.st_dev, database_stat.st_ino)
    if (
        not stat.S_ISREG(database_stat.st_mode)
        or database_stat.st_uid != os.geteuid()
        or database_stat.st_nlink != 1
        or stat.S_IMODE(database_stat.st_mode) != 0o600
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver database file is not private"
        )
    return identity


def _prepare_database_file(database_path: str) -> Callable[[], None]:
    if database_path == ":memory:":
        return lambda: None
    _validate_database_parent(database_path)
    try:
        identity = _validate_database_file(database_path)
    except DurableTerminalReceiverRuntimeBlocked as error:
        if error.__cause__ is None or not isinstance(
            error.__cause__, FileNotFoundError
        ):
            raise
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(database_path, flags, 0o600)
        except FileExistsError:
            identity = _validate_database_file(database_path)
        except OSError as create_error:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database file could not be created"
            ) from create_error
        else:
            try:
                os.fchmod(descriptor, 0o600)
                created_stat = os.fstat(descriptor)
                identity = (created_stat.st_dev, created_stat.st_ino)
                if (
                    not stat.S_ISREG(created_stat.st_mode)
                    or created_stat.st_uid != os.geteuid()
                    or created_stat.st_nlink != 1
                    or stat.S_IMODE(created_stat.st_mode) != 0o600
                ):
                    raise DurableTerminalReceiverRuntimeBlocked(
                        "terminal receiver database file was not created privately"
                    )
            finally:
                os.close(descriptor)

    def guard() -> None:
        _validate_database_parent(database_path)
        _validate_database_file(database_path, identity)

    guard()
    return guard


def _preflight_application(
    authenticate: Callable[[Mapping[str, Any], Mapping[str, str]], Any],
    *,
    origin: str,
    clock: Callable[[], float | int],
    path: str,
    require_https: bool,
) -> None:
    dummy_inbox = object.__new__(_RECEIVER.DurableCMSTerminalNotificationInbox)
    try:
        _RECEIVER.CMSTerminalNotificationReceiverApplication(
            dummy_inbox,
            authenticate,
            origin=origin,
            clock=clock,
            path=path,
            require_https=require_https,
        )
    except (TypeError, ValueError) as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver configuration is invalid"
        ) from error


def _preflight_processing(
    max_attempts: int,
    base_delay_seconds: float | int,
    max_delay_seconds: float | int,
) -> None:
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 20
    ):
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal processing attempts are invalid"
        )
    try:
        base_delay = _RECEIVER._duration(
            base_delay_seconds, "processing_delay_invalid"
        )
        max_delay = _RECEIVER._duration(
            max_delay_seconds, "processing_delay_invalid"
        )
    except Exception as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal processing delay is invalid"
        ) from error
    if base_delay > max_delay:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal processing delay range is invalid"
        )


class DurableTerminalNotificationReceiverRuntime:
    """Own one protected SQLite connection and its complete WSGI boundary."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        inbox: Any,
        application: Any,
        database_guard: Callable[[], None],
        clock: Callable[[], float | int],
    ):
        self._connection = connection
        self.inbox = inbox
        self.application = application
        self._database_guard = database_guard
        self._clock = clock
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._closed = False
        self._worker_state = "unmanaged"
        self._worker_error_code: str | None = None
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def _assert_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver runtime belongs to another process"
            )

    def _require_open(self) -> None:
        self._assert_owner()
        if self._closed:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver runtime is closed"
            )
        try:
            self._database_guard()
        except Exception as error:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver database is unsafe"
            ) from error

    @staticmethod
    def _unavailable(start_response: Callable[..., Any]):
        value = {
            "schema": _RECEIVER.ERROR_SCHEMA,
            "status": "BLOCK",
            "error_code": "notification_receiver.runtime_unavailable",
        }
        try:
            body = json.dumps(
                value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            start_response("503 Service Unavailable", (
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
            ))
            return [body]
        except Exception:
            return []

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        try:
            self._assert_owner()
            with self._lock:
                self._require_open()
                if isinstance(environ, Mapping) and environ.get("PATH_INFO") in {
                    _RECEIVER.STATUS_PATH, _RECEIVER.READINESS_PATH,
                    _RECEIVER.CAPABILITIES_PATH, _RECEIVER.HEALTH_PATH,
                }:
                    return self._control_request(environ, start_response)
                self.require_worker_ready()
                return self.application(environ, start_response)
        except Exception:
            return self._unavailable(start_response)

    def _control_request(
        self,
        environ: Mapping[str, Any],
        start_response: Callable[..., Any],
    ):
        """Serve authenticated, content-free receiver control routes."""

        try:
            path = environ.get("PATH_INFO")
            if not isinstance(environ, Mapping):
                _RECEIVER._blocked("environment_invalid", 400)
            if self.application.require_https and (
                environ.get("wsgi.url_scheme") != "https"
            ):
                _RECEIVER._blocked("https_required", 400)
            if environ.get("QUERY_STRING") not in {None, ""}:
                _RECEIVER._blocked("query_rejected", 400)
            if environ.get("HTTP_TRANSFER_ENCODING") not in {None, ""}:
                _RECEIVER._blocked("framing_invalid", 400)
            headers = self.application._headers(environ)
            if path == _RECEIVER.CAPABILITIES_PATH:
                return self._capabilities_request(
                    environ, headers, start_response,
                )
            if path == _RECEIVER.HEALTH_PATH:
                return self._health_request(environ, headers, start_response)
            if path == _RECEIVER.READINESS_PATH:
                return self._readiness_request(
                    environ, headers, start_response,
                )
            if path == _RECEIVER.STATUS_PATH:
                return self._status_request(environ, headers, start_response)
            _RECEIVER._blocked("path_not_found", 404)
        except _RECEIVER.TerminalNotificationReceiverBlocked as failure:
            return self.application._send(start_response, failure.status, {
                "schema": _RECEIVER.ERROR_SCHEMA,
                "status": "BLOCK",
                "error_code": failure.code,
            })
        except Exception:
            return self._unavailable(start_response)

    def _authenticate_control(
        self,
        request: dict[str, Any],
        headers: dict[str, str],
        site_id: str | None,
        scope: str,
    ) -> None:
        try:
            principal = self.application.authenticate(
                dict(request), dict(headers)
            )
        except Exception:
            _RECEIVER._blocked("authentication_unavailable", 503)
        _RECEIVER._principal(principal, site_id, scope)

    def _capabilities_request(
        self,
        environ: Mapping[str, Any],
        headers: dict[str, str],
        start_response: Callable[..., Any],
    ):
        if environ.get("REQUEST_METHOD") != "GET":
            _RECEIVER._blocked("method_not_allowed", 405)
        if headers.get("content-length") not in {None, "0"}:
            _RECEIVER._blocked("body_invalid", 400)
        if "content-type" in headers:
            _RECEIVER._blocked("content_type", 415)
        body_sha256 = hashlib.sha256(b"").hexdigest()
        request = {
            "schema": _RECEIVER.AUTH_SCHEMA,
            "method": "GET",
            "origin": self.application.origin,
            "path": _RECEIVER.CAPABILITIES_PATH,
            "body_sha256": body_sha256,
        }
        self._authenticate_control(
            request, headers, None, _RECEIVER.CAPABILITIES_SCOPE,
        )
        return self.application._send(start_response, 200, {
            "schema": _RECEIVER.CAPABILITIES_RESPONSE_SCHEMA,
            "capabilities": _RECEIVER.capabilities_payload(
                self.application.path
            ),
        })

    def _readiness_request(
        self,
        environ: Mapping[str, Any],
        headers: dict[str, str],
        start_response: Callable[..., Any],
    ):
        if environ.get("REQUEST_METHOD") != "GET":
            _RECEIVER._blocked("method_not_allowed", 405)
        if headers.get("content-length") not in {None, "0"}:
            _RECEIVER._blocked("body_invalid", 400)
        if "content-type" in headers:
            _RECEIVER._blocked("content_type", 415)
        body_sha256 = hashlib.sha256(b"").hexdigest()
        request = {
            "schema": _RECEIVER.AUTH_SCHEMA,
            "method": "GET",
            "origin": self.application.origin,
            "path": _RECEIVER.READINESS_PATH,
            "body_sha256": body_sha256,
        }
        self._authenticate_control(
            request, headers, None, _RECEIVER.READINESS_SCOPE,
        )
        report = {
            **self.worker_readiness(),
            "capabilities_sha256": _RECEIVER.capabilities_payload(
                self.application.path
            )["sha256"],
        }
        status = 200 if report["status"] == "ready" else 503
        return self.application._send(start_response, status, report)

    def _health_request(
        self,
        environ: Mapping[str, Any],
        headers: dict[str, str],
        start_response: Callable[..., Any],
    ):
        if environ.get("REQUEST_METHOD") != "GET":
            _RECEIVER._blocked("method_not_allowed", 405)
        if headers.get("content-length") not in {None, "0"}:
            _RECEIVER._blocked("body_invalid", 400)
        if "content-type" in headers:
            _RECEIVER._blocked("content_type", 415)
        body_sha256 = hashlib.sha256(b"").hexdigest()
        request = {
            "schema": _RECEIVER.AUTH_SCHEMA,
            "method": "GET",
            "origin": self.application.origin,
            "path": _RECEIVER.HEALTH_PATH,
            "body_sha256": body_sha256,
        }
        self._authenticate_control(
            request, headers, None, _RECEIVER.HEALTH_SCOPE,
        )
        report = self.worker_health()
        counts = report.get("processing_counts") if isinstance(
            report, Mapping
        ) else None
        metrics = tuple(
            report.get(name) if isinstance(report, Mapping) else None
            for name in (
                "received", "processing_due", "expired_leases", "failed",
            )
        )
        metrics_available = counts is not None
        worker_ok = isinstance(report, Mapping) and report.get(
            "worker_state"
        ) in {"unmanaged", "running"}
        healthy = (
            isinstance(report, Mapping)
            and report.get("status") == "ok"
            and report.get("inbox_status") == "ok"
            and report.get("error_code") is None
            and worker_ok
        )
        valid_counts = (
            isinstance(counts, dict)
            and set(counts) == set(_RECEIVER.PROCESSING_STATUSES)
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                and value >= 0
                for value in counts.values()
            )
        )
        if not (
            isinstance(report, dict)
            and set(report) == set(_RECEIVER.HEALTH_RESPONSE_FIELDS)
            and report.get("schema") == _RECEIVER.HEALTH_RESPONSE_SCHEMA
            and report.get("status") in {"ok", "blocked"}
            and report.get("runtime_state") == "open"
            and report.get("worker_state") in {
                "unmanaged", "starting", "running", "stopping", "stopped",
                "failed",
            }
            and report.get("inbox_status") in {"ok", "blocked"}
            and (report.get("error_code") is None or _RECEIVER._error(
                report.get("error_code")
            ))
            and healthy == (report.get("status") == "ok")
            and (report.get("status") == "ok") == (
                report.get("error_code") is None
            )
            and (
                (metrics_available and valid_counts and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    and value >= 0
                    for value in metrics
                ))
                or (
                    not metrics_available
                    and counts is None
                    and all(value is None for value in metrics)
                    and report.get("status") == "blocked"
                    and report.get("inbox_status") == "blocked"
                )
            )
            and (
                not metrics_available
                or (
                    sum(counts.values()) == report["received"]
                    and report["failed"] == counts["failed"]
                    and report["processing_due"] <= (
                        counts["pending"] + counts["retry_wait"]
                    )
                    and report["expired_leases"] <= counts["leased"]
                    and (report["inbox_status"] == "ok") == (
                        report["failed"] == 0
                        and report["expired_leases"] == 0
                    )
                )
            )
        ):
            _RECEIVER._blocked("health_invalid", 503)
        response = {
            **report,
            "capabilities_sha256": _RECEIVER.capabilities_payload(
                self.application.path
            )["sha256"],
        }
        status = 200 if report["status"] == "ok" else 503
        return self.application._send(start_response, status, response)

    def _status_request(
        self,
        environ: Mapping[str, Any],
        headers: dict[str, str],
        start_response: Callable[..., Any],
    ):
        if environ.get("REQUEST_METHOD") != "POST":
            _RECEIVER._blocked("method_not_allowed", 405)
        if headers.get("content-type", "").lower().replace(" ", "") != (
            "application/json;charset=utf-8"
        ):
            _RECEIVER._blocked("content_type", 415)
        length = headers.get("content-length")
        if (
            not isinstance(length, str)
            or not length.isascii()
            or not length.isdecimal()
        ):
            _RECEIVER._blocked("content_length_required", 411)
        size = int(length)
        if size <= 0:
            _RECEIVER._blocked("body_invalid", 400)
        if size > _RECEIVER.MAX_BODY_BYTES:
            _RECEIVER._blocked("body_too_large", 413)
        try:
            body = environ["wsgi.input"].read(size)
        except Exception:
            body = None
        if not isinstance(body, bytes) or len(body) != size:
            _RECEIVER._blocked("body_invalid", 400)
        try:
            text = body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            value = json.loads(
                text,
                object_pairs_hook=_RECEIVER._pairs,
                parse_constant=_RECEIVER._constant,
            )
        except (
            UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError,
        ):
            _RECEIVER._blocked("json_invalid", 400)
        if not (
            isinstance(value, dict)
            and set(value) == {"schema", "event_id", "site_id"}
            and value.get("schema") == _RECEIVER.STATUS_REQUEST_SCHEMA
            and _RECEIVER._token(value.get("event_id"))
            and _RECEIVER._token(value.get("site_id"))
            and _RECEIVER._canonical(value) == body
        ):
            _RECEIVER._blocked("request_invalid", 400)
        body_sha256 = hashlib.sha256(body).hexdigest()
        if headers.get("x-localization-terminal-status-sha256") != body_sha256:
            _RECEIVER._blocked("header_binding_invalid", 400)
        request = {
            "schema": _RECEIVER.AUTH_SCHEMA,
            "method": "POST",
            "origin": self.application.origin,
            "path": _RECEIVER.STATUS_PATH,
            "event_id": value["event_id"],
            "site_id": value["site_id"],
            "body_sha256": body_sha256,
        }
        self._authenticate_control(
            request, headers, value["site_id"], _RECEIVER.STATUS_SCOPE,
        )
        processing = self.inbox.processing_status(
            value["event_id"], now=self._clock(),
        )
        if processing.site_id != value["site_id"]:
            _RECEIVER._blocked("not_found", 404)
        response = {
            "schema": _RECEIVER.STATUS_RESPONSE_SCHEMA,
            "notification_id": processing.notification_id,
            "event_id": processing.event_id,
            "site_id": processing.site_id,
            "terminal_status": processing.terminal_status,
            "notification_sha256": processing.payload_sha256,
            "processing_status": processing.status,
            "attempts": processing.attempts,
            "max_attempts": processing.max_attempts,
            "next_attempt_at": processing.next_attempt_at,
            "lease_expires_at": processing.lease_expires_at,
            "lease_expired": processing.lease_expired,
            "last_error_code": processing.last_error_code,
            "processed_at": processing.processed_at,
            "capabilities_sha256": _RECEIVER.capabilities_payload(
                self.application.path
            )["sha256"],
        }
        return self.application._send(start_response, 200, response)

    def status(self, event_id: str):
        self._assert_owner()
        with self._lock:
            self._require_open()
            try:
                return self.inbox.status(event_id)
            except Exception as error:
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver status is unavailable"
                ) from error

    def health(self) -> DurableTerminalReceiverRuntimeHealth:
        self._assert_owner()
        with self._lock:
            self._require_open()
            try:
                health = self.inbox.health(now=self._clock())
            except Exception as error:
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver health is unavailable"
                ) from error
            if (
                health.status not in {"ok", "blocked"}
                or not isinstance(health.received, int)
                or not isinstance(health.counts, dict)
                or set(health.counts) != set(_RECEIVER.PROCESSING_STATUSES)
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    or value < 0
                    for value in health.counts.values()
                )
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    or value < 0
                    for value in (
                        health.due, health.expired_leases, health.failed,
                    )
                )
            ):
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver health is invalid"
                )
            return DurableTerminalReceiverRuntimeHealth(
                health.status,
                "open",
                health.received,
                dict(health.counts),
                health.due,
                health.expired_leases,
                health.failed,
            )

    def process_next(
        self,
        callback: Callable[[Mapping[str, Any]], Any],
        worker_id: str,
        *,
        now: float | int | None = None,
        lease_seconds: float | int = 600,
    ):
        """Run at most one lease-bound host processing callback."""

        self._assert_owner()
        with self._lock:
            self._require_open()
            try:
                return self.inbox.run_next_processing(
                    callback,
                    worker_id,
                    now=self._clock() if now is None else now,
                    lease_seconds=lease_seconds,
                )
            except Exception as error:
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver processing is unavailable"
                ) from error

    @staticmethod
    def _worker_configuration(
        callback: Any,
        worker_id: Any,
        lease_seconds: float | int,
        active_delay_seconds: float | int,
        idle_delay_seconds: float | int,
        blocked_delay_seconds: float | int,
    ) -> tuple[float, float, float, float]:
        if (
            not callable(callback)
            or not isinstance(worker_id, str)
            or not _RECEIVER._token(worker_id)
            or len(worker_id) > 128
        ):
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver worker configuration is invalid"
            )
        try:
            values = tuple(
                _RECEIVER._duration(value, "worker_duration_invalid")
                for value in (
                    lease_seconds,
                    active_delay_seconds,
                    idle_delay_seconds,
                    blocked_delay_seconds,
                )
            )
        except Exception as error:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver worker configuration is invalid"
            ) from error
        return values

    @staticmethod
    def _join_timeout(value: float | int) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 300
        ):
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver worker timeout is invalid"
            )
        return float(value)

    def _managed_worker(
        self,
        stop: threading.Event,
        callback: Callable[[Mapping[str, Any]], Any],
        worker_id: str,
        configuration: tuple[float, float, float, float],
    ) -> None:
        try:
            with self._lock:
                if self._worker_state != "starting" or self._worker_stop is not stop:
                    return
                self._worker_state = "running"
            lease, active, idle, blocked = configuration
            while not stop.is_set():
                outcome = self.process_next(
                    callback, worker_id, lease_seconds=lease,
                )
                delay = (
                    idle if outcome is None
                    else blocked if outcome.status in {"retry_wait", "failed"}
                    else active
                )
                stop.wait(delay)
        except Exception:
            with self._lock:
                self._worker_error_code = (
                    "notification_receiver.worker_blocked"
                )
                self._worker_state = "failed"
            return
        with self._lock:
            if self._worker_state in {"running", "stopping"}:
                self._worker_state = "stopped"

    def start_worker(
        self,
        callback: Callable[[Mapping[str, Any]], Any],
        worker_id: str,
        *,
        lease_seconds: float | int = 600,
        active_delay_seconds: float | int = 0.05,
        idle_delay_seconds: float | int = 1,
        blocked_delay_seconds: float | int = 5,
    ) -> None:
        """Start one process-owned, interruptible processing worker."""

        configuration = self._worker_configuration(
            callback,
            worker_id,
            lease_seconds,
            active_delay_seconds,
            idle_delay_seconds,
            blocked_delay_seconds,
        )
        self._assert_owner()
        with self._lock:
            self._require_open()
            if self._worker_state in {"starting", "running"}:
                return
            if self._worker_state in {"stopping", "failed"}:
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver worker is blocked"
                )
            stop = threading.Event()
            thread = threading.Thread(
                target=self._managed_worker,
                args=(stop, callback, worker_id, configuration),
                name="cms-terminal-receiver-worker",
                daemon=False,
            )
            self._worker_stop = stop
            self._worker_thread = thread
            self._worker_error_code = None
            self._worker_state = "starting"
            try:
                thread.start()
            except Exception as error:
                self._worker_thread = None
                self._worker_state = "failed"
                self._worker_error_code = (
                    "notification_receiver.worker_blocked"
                )
                raise DurableTerminalReceiverRuntimeBlocked(
                    "terminal receiver worker is blocked"
                ) from error

    def stop_worker(self, *, timeout_seconds: float | int = 30) -> None:
        """Stop and join the managed worker without closing its database."""

        timeout = self._join_timeout(timeout_seconds)
        self._assert_owner()
        thread = self._worker_thread
        state = self._worker_state
        if thread is None or state in {"unmanaged", "stopped"}:
            return
        if thread is threading.current_thread():
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver worker cannot stop itself"
            )
        if state != "failed":
            self._worker_state = "stopping"
        self._worker_stop.set()
        thread.join(timeout)
        if thread.is_alive():
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver worker stop timed out"
            )
        with self._lock:
            if self._worker_state == "stopping":
                self._worker_state = "stopped"

    def require_worker_ready(self) -> None:
        """Block hosted HTTP intake unless its processor is healthy."""

        self._assert_owner()
        if self._closed:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver runtime is closed"
            )
        if self._worker_state == "unmanaged":
            return
        if self._worker_state != "running" or self._worker_error_code is not None:
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver worker is not ready"
            )
        if self.health().status != "ok":
            raise DurableTerminalReceiverRuntimeBlocked(
                "terminal receiver storage is blocked"
            )

    def worker_readiness(self) -> dict[str, Any]:
        """Return a content-free readiness snapshot for a host supervisor."""

        self._assert_owner()
        with self._lock:
            state = "closed" if self._closed else self._worker_state
            error_code = self._worker_error_code
            if state != "running":
                return {
                    "schema": "blun.cms-terminal-receiver-readiness.v1",
                    "status": "not_ready",
                    "worker_state": state,
                    "inbox_status": None,
                    "error_code": error_code or (
                        "notification_receiver.worker_not_ready"
                    ),
                }
            try:
                inbox_status = self.health().status
            except Exception:
                inbox_status = "blocked"
            ready = inbox_status == "ok"
            return {
                "schema": "blun.cms-terminal-receiver-readiness.v1",
                "status": "ready" if ready else "not_ready",
                "worker_state": "running",
                "inbox_status": inbox_status,
                "error_code": None if ready else (
                    "notification_receiver.storage_blocked"
                ),
            }

    def worker_health(self) -> dict[str, Any]:
        """Return aggregate runtime, worker, and durable inbox health."""

        self._assert_owner()
        with self._lock:
            self._require_open()
            worker_state = self._worker_state
            worker_error = self._worker_error_code
            try:
                inbox = self.health()
            except Exception:
                return {
                    "schema": _RECEIVER.HEALTH_RESPONSE_SCHEMA,
                    "status": "blocked",
                    "runtime_state": "open",
                    "worker_state": worker_state,
                    "inbox_status": "blocked",
                    "received": None,
                    "processing_counts": None,
                    "processing_due": None,
                    "expired_leases": None,
                    "failed": None,
                    "error_code": "notification_receiver.storage_blocked",
                }
            worker_ok = (
                worker_state in {"unmanaged", "running"}
                and worker_error is None
            )
            healthy = worker_ok and inbox.status == "ok"
            if worker_error is not None:
                error_code = worker_error
            elif not worker_ok:
                error_code = "notification_receiver.worker_not_ready"
            elif inbox.status != "ok":
                error_code = "notification_receiver.storage_blocked"
            else:
                error_code = None
            return {
                "schema": _RECEIVER.HEALTH_RESPONSE_SCHEMA,
                "status": "ok" if healthy else "blocked",
                "runtime_state": "open",
                "worker_state": worker_state,
                "inbox_status": inbox.status,
                "received": inbox.received,
                "processing_counts": dict(inbox.processing_counts),
                "processing_due": inbox.processing_due,
                "expired_leases": inbox.expired_leases,
                "failed": inbox.failed,
                "error_code": error_code,
            }

    def close(self, *, worker_timeout_seconds: float | int = 30) -> None:
        self._assert_owner()
        self.stop_worker(timeout_seconds=worker_timeout_seconds)
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    @property
    def state(self) -> str:
        if os.getpid() != self._owner_pid:
            return "foreign-process"
        with self._lock:
            return "closed" if self._closed else "open"

    @property
    def worker_state(self) -> str:
        if os.getpid() != self._owner_pid:
            return "foreign-process"
        with self._lock:
            return "closed" if self._closed else self._worker_state

    def __enter__(self) -> "DurableTerminalNotificationReceiverRuntime":
        self._require_open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def open_durable_terminal_notification_receiver(
    database: str | os.PathLike[str],
    authenticate: Callable[[Mapping[str, Any], Mapping[str, str]], Any],
    *,
    origin: str,
    clock: Callable[[], float | int] = time.time,
    path: str = _RECEIVER.DEFAULT_PATH,
    require_https: bool = True,
    processing_max_attempts: int = 5,
    processing_base_delay_seconds: float | int = 5,
    processing_max_delay_seconds: float | int = 300,
) -> DurableTerminalNotificationReceiverRuntime:
    """Validate all configuration before opening one protected SQLite worker."""

    database_path = _database_path(database)
    _preflight_application(
        authenticate,
        origin=origin,
        clock=clock,
        path=path,
        require_https=require_https,
    )
    _preflight_processing(
        processing_max_attempts,
        processing_base_delay_seconds,
        processing_max_delay_seconds,
    )
    database_guard = _prepare_database_file(database_path)
    try:
        connection = sqlite3.connect(
            database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
    except (OSError, sqlite3.Error) as error:
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver database could not be opened"
        ) from error
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise sqlite3.DatabaseError("foreign keys unavailable")
        if connection.execute("PRAGMA secure_delete").fetchone()[0] != 1:
            raise sqlite3.DatabaseError("secure deletion unavailable")
        database_guard()
        inbox = _RECEIVER.DurableCMSTerminalNotificationInbox(
            connection,
            database_guard=database_guard,
            processing_max_attempts=processing_max_attempts,
            processing_base_delay_seconds=processing_base_delay_seconds,
            processing_max_delay_seconds=processing_max_delay_seconds,
        )
        application = _RECEIVER.CMSTerminalNotificationReceiverApplication(
            inbox,
            authenticate,
            origin=origin,
            clock=clock,
            path=path,
            require_https=require_https,
        )
    except Exception as error:
        connection.close()
        raise DurableTerminalReceiverRuntimeBlocked(
            "terminal receiver runtime initialization failed"
        ) from error
    return DurableTerminalNotificationReceiverRuntime(
        connection, inbox, application, database_guard, clock,
    )


def open_hosted_durable_terminal_notification_receiver(
    database: str | os.PathLike[str],
    authenticate: Callable[[Mapping[str, Any], Mapping[str, str]], Any],
    callback: Callable[[Mapping[str, Any]], Any],
    *,
    origin: str,
    worker_id: str,
    clock: Callable[[], float | int] = time.time,
    path: str = _RECEIVER.DEFAULT_PATH,
    require_https: bool = True,
    processing_max_attempts: int = 5,
    processing_base_delay_seconds: float | int = 5,
    processing_max_delay_seconds: float | int = 300,
    lease_seconds: float | int = 600,
    active_delay_seconds: float | int = 0.05,
    idle_delay_seconds: float | int = 1,
    blocked_delay_seconds: float | int = 5,
) -> DurableTerminalNotificationReceiverRuntime:
    """Open a durable receiver and start its supervised processor."""

    configuration = (
        callback,
        worker_id,
        lease_seconds,
        active_delay_seconds,
        idle_delay_seconds,
        blocked_delay_seconds,
    )
    DurableTerminalNotificationReceiverRuntime._worker_configuration(
        *configuration
    )
    runtime = open_durable_terminal_notification_receiver(
        database,
        authenticate,
        origin=origin,
        clock=clock,
        path=path,
        require_https=require_https,
        processing_max_attempts=processing_max_attempts,
        processing_base_delay_seconds=processing_base_delay_seconds,
        processing_max_delay_seconds=processing_max_delay_seconds,
    )
    try:
        runtime.start_worker(
            callback,
            worker_id,
            lease_seconds=lease_seconds,
            active_delay_seconds=active_delay_seconds,
            idle_delay_seconds=idle_delay_seconds,
            blocked_delay_seconds=blocked_delay_seconds,
        )
    except Exception:
        runtime.close()
        raise
    return runtime
