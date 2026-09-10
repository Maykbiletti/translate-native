#!/usr/bin/env python3
"""Validated composition root for the durable CMS reference receiver.

The factory validates the complete receiver boundary before opening SQLite, then
owns exactly one connection, durable store, and WSGI application. Deployments
must construct one runtime per WSGI worker rather than sharing it across worker
processes or request threads.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required CMS runtime dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_RECEIVER = _load_module(
    "blun_website_localization_composed_cms_receiver",
    _ROOT / "integrations" / "website_localization_cms_receiver.py",
)
_STORE = _load_module(
    "blun_website_localization_composed_cms_receiver_store",
    _ROOT / "integrations" / "website_localization_cms_receiver_store.py",
)


class DurableCMSReceiverRuntimeBlocked(RuntimeError):
    """Private composition failure that must not be exposed to HTTP clients."""


def _database_path(value: Any) -> str:
    try:
        result = os.fspath(value)
    except TypeError as error:
        raise DurableCMSReceiverRuntimeBlocked("database path is invalid") from error
    if isinstance(result, bytes):
        raise DurableCMSReceiverRuntimeBlocked("database path must be Unicode")
    if (
        not isinstance(result, str)
        or not result
        or "\x00" in result
        or result.startswith("file:")
    ):
        raise DurableCMSReceiverRuntimeBlocked("database path is invalid")
    return result


def _unused(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
    return {}


def _preflight_receiver(
    publication_authority: Any,
    acknowledgement_authority: Any,
    authenticate: Callable[[Mapping[str, str]], bool],
    *,
    contract_sha256: str,
    clock: Callable[[], float | int],
    path: str,
    require_https: bool,
) -> None:
    if publication_authority is acknowledgement_authority:
        raise DurableCMSReceiverRuntimeBlocked(
            "publication and acknowledgement authorities must be independent"
        )
    try:
        _RECEIVER.CMSReceiverApplication(
            publication_authority=publication_authority,
            acknowledgement_authority=acknowledgement_authority,
            authenticate=authenticate,
            resolve_publication_expectation=_unused,
            commit=_unused,
            resolve_tombstone_expectation=_unused,
            delete=_unused,
            check=_unused,
            contract_sha256=contract_sha256,
            clock=clock,
            path=path,
            require_https=require_https,
        )
    except (TypeError, ValueError) as error:
        raise DurableCMSReceiverRuntimeBlocked(
            "CMS receiver configuration is invalid"
        ) from error


class DurableCMSReceiverRuntime:
    """One worker-owned SQLite store and its exact HTTPS-only WSGI boundary."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        store: Any,
        application: Any,
    ):
        self._connection = connection
        self._store = store
        self.application = application
        self._closed = False

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"DurableCMSReceiverRuntime(state={state!r})"

    def __enter__(self) -> "DurableCMSReceiverRuntime":
        if self._closed:
            raise DurableCMSReceiverRuntimeBlocked("CMS receiver runtime is closed")
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]):
        return self.application(environ, start_response)

    def _active_store(self):
        if self._closed:
            raise DurableCMSReceiverRuntimeBlocked("CMS receiver runtime is closed")
        return self._store

    def register_source(self, expectation: Any) -> Mapping[str, Any]:
        """Register a trusted current-source expectation."""

        return self._active_store().register_source(expectation)

    def register_tombstone(self, expectation: Any) -> Mapping[str, Any]:
        """Pre-authorize deletion of one exact acknowledged publication."""

        return self._active_store().register_tombstone(expectation)

    def read_active_bundle(
        self, site_id: str, source_id: str,
    ) -> Mapping[str, Any] | None:
        """Return target prose only to trusted CMS rendering code."""

        return self._active_store().read_active_bundle(site_id, source_id)

    def close(self) -> None:
        """Close the worker-owned connection; repeated close is harmless."""

        if not self._closed:
            self._connection.close()
            self._closed = True


def open_durable_cms_receiver(
    database: str | os.PathLike[str],
    publication_authority: Any,
    acknowledgement_authority: Any,
    authenticate: Callable[[Mapping[str, str]], bool],
    *,
    contract_sha256: str,
    clock: Callable[[], float | int] = time.time,
    path: str = _RECEIVER.RECEIVER_PATH,
    require_https: bool = True,
) -> DurableCMSReceiverRuntime:
    """Validate configuration, then open one complete durable receiver worker."""

    database_path = _database_path(database)
    _preflight_receiver(
        publication_authority,
        acknowledgement_authority,
        authenticate,
        contract_sha256=contract_sha256,
        clock=clock,
        path=path,
        require_https=require_https,
    )
    try:
        connection = sqlite3.connect(
            database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=True,
        )
    except (OSError, sqlite3.Error) as error:
        raise DurableCMSReceiverRuntimeBlocked(
            "CMS receiver database could not be opened"
        ) from error
    try:
        store = _STORE.DurableCMSReceiverStore(connection, clock=clock)
        application = _RECEIVER.CMSReceiverApplication(
            publication_authority=publication_authority,
            acknowledgement_authority=acknowledgement_authority,
            authenticate=authenticate,
            resolve_publication_expectation=store.resolve_publication_expectation,
            commit=store.commit,
            resolve_tombstone_expectation=store.resolve_tombstone_expectation,
            delete=store.delete,
            check=store.check,
            contract_sha256=contract_sha256,
            clock=clock,
            path=path,
            require_https=require_https,
        )
    except (_STORE.CMSReceiverStoreBlocked, TypeError, ValueError) as error:
        connection.close()
        raise DurableCMSReceiverRuntimeBlocked(
            "CMS receiver runtime initialization failed"
        ) from error
    except Exception:
        connection.close()
        raise
    return DurableCMSReceiverRuntime(connection, store, application)
