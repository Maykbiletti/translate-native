#!/usr/bin/env python3
"""Rotatable HMAC authentication for the source-delivery sidecar.

The client signer and server verifier bind the exact request, tenant identity,
idempotency fields, and both deployed capability contracts. A durable nonce
store rejects replay before the sidecar parses or persists website content.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import math
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


PROOF_SCHEMA = "blun.cms-source-delivery-sidecar-hmac-proof.v1"
REPLAY_SCHEMA_VERSION = 1
ALGORITHM = "hmac-sha256"
MAX_SECRET_BYTES = 4_096
MAX_CLOCK_SKEW_SECONDS = 300
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
SIGNATURE = re.compile(r"^[a-f0-9]{64}$")

HEADER_CREDENTIAL_ID = "X-Localization-Auth-Credential-Id"
HEADER_CREDENTIAL_VERSION = "X-Localization-Auth-Credential-Version"
HEADER_TIMESTAMP = "X-Localization-Auth-Timestamp"
HEADER_NONCE = "X-Localization-Auth-Nonce"
HEADER_SIGNATURE = "X-Localization-Auth-Signature"
HEADER_SITE_ID = "X-Localization-Auth-Site-Id"
HEADER_EVENT_ID = "X-Localization-Auth-Event-Id"
HEADER_REQUEST_ID = "X-Localization-Auth-Request-Id"
HEADER_PAYLOAD_SHA256 = "X-Localization-Auth-Payload-Sha256"

_COMMON_HEADERS = {
    HEADER_CREDENTIAL_ID.lower(),
    HEADER_CREDENTIAL_VERSION.lower(),
    HEADER_TIMESTAMP.lower(),
    HEADER_NONCE.lower(),
    HEADER_SIGNATURE.lower(),
}
_TENANT_HEADERS = {
    HEADER_SITE_ID.lower(),
    HEADER_EVENT_ID.lower(),
    HEADER_REQUEST_ID.lower(),
    HEADER_PAYLOAD_SHA256.lower(),
}
_REPLAY_META_COLUMNS = ("singleton", "schema_version")
_REPLAY_COLUMNS = (
    "credential_id", "credential_version", "nonce", "proof_sha256",
    "issued_at", "expires_at",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load HMAC authentication dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_HTTP = _load_module(
    "blun_website_localization_cms_source_delivery_auth_http",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_http.py",
)
_CLIENT = _load_module(
    "blun_website_localization_cms_source_delivery_auth_client",
    _ROOT / "integrations" / "website_localization_cms_source_delivery_client.py",
)


class SourceDeliveryHMACAuthenticationUnavailable(RuntimeError):
    """Stable content-free authentication infrastructure failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _unavailable(code: str) -> SourceDeliveryHMACAuthenticationUnavailable:
    return SourceDeliveryHMACAuthenticationUnavailable(
        "source_delivery_hmac." + code,
    )


def _token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and TOKEN.fullmatch(value) is not None
        and unicodedata.is_normalized("NFC", value)
    )


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _timestamp(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError("timestamp is invalid")
    return float(value)


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value), ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("proof is invalid") from None


@dataclass(frozen=True, repr=False)
class HMACCredential:
    """One explicit credential generation; the secret is never represented."""

    principal_id: str
    credential_id: str
    credential_version: str
    secret: bytes = field(repr=False, compare=False)
    scopes: tuple[str, ...]
    site_id: str | None = None

    def __post_init__(self) -> None:
        if not all(_token(value) for value in (
            self.principal_id, self.credential_id, self.credential_version,
        )):
            raise ValueError("credential identity is invalid")
        if (
            not isinstance(self.secret, bytes)
            or not 32 <= len(self.secret) <= MAX_SECRET_BYTES
        ):
            raise ValueError("credential secret must contain 32 to 4096 bytes")
        if (
            not isinstance(self.scopes, tuple)
            or not self.scopes
            or tuple(sorted(set(self.scopes))) != self.scopes
            or any(scope not in _HTTP.SCOPES.values() for scope in self.scopes)
        ):
            raise ValueError("credential scopes are invalid")
        tenant_scopes = {
            _HTTP.SCOPES[path] for path in _HTTP.TENANT_PATHS
        }
        if self.site_id is not None and not _token(self.site_id):
            raise ValueError("credential tenant is invalid")
        if tenant_scopes.intersection(self.scopes) and self.site_id is None:
            raise ValueError("tenant scopes require one site")

    def __repr__(self) -> str:
        return (
            "HMACCredential(principal_id={!r}, credential_id={!r}, "
            "credential_version={!r}, scopes={!r}, site_id={!r}, "
            "secret=<redacted>)"
        ).format(
            self.principal_id, self.credential_id, self.credential_version,
            self.scopes, self.site_id,
        )


class DurableHMACReplayStore:
    """Atomically consume proof nonces in caller-owned durable SQLite."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            try:
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS cms_source_delivery_hmac_meta (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                    )
                """)
                self.connection.execute("""
                    INSERT OR IGNORE INTO cms_source_delivery_hmac_meta
                    VALUES (1, 1)
                """)
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS cms_source_delivery_hmac_nonces (
                        credential_id TEXT NOT NULL,
                        credential_version TEXT NOT NULL,
                        nonce TEXT NOT NULL,
                        proof_sha256 TEXT NOT NULL,
                        issued_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        PRIMARY KEY (credential_id, credential_version, nonce)
                    )
                """)
                self.connection.commit()
                self._validate()
            except SourceDeliveryHMACAuthenticationUnavailable:
                raise
            except sqlite3.Error:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise _unavailable("replay_store_unavailable") from None

    def _validate(self) -> None:
        meta_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_delivery_hmac_meta)"
            ).fetchall()
        )
        nonce_columns = tuple(
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(cms_source_delivery_hmac_nonces)"
            ).fetchall()
        )
        meta = self.connection.execute(
            "SELECT singleton, schema_version FROM cms_source_delivery_hmac_meta"
        ).fetchall()
        if (
            meta_columns != _REPLAY_META_COLUMNS
            or nonce_columns != _REPLAY_COLUMNS
            or len(meta) != 1
            or tuple(meta[0]) != (1, REPLAY_SCHEMA_VERSION)
        ):
            raise _unavailable("replay_store_integrity")
        for row in self.connection.execute(
            "SELECT * FROM cms_source_delivery_hmac_nonces"
        ).fetchall():
            try:
                valid = (
                    _token(row["credential_id"])
                    and _token(row["credential_version"])
                    and isinstance(row["nonce"], str)
                    and NONCE.fullmatch(row["nonce"]) is not None
                    and _sha256(row["proof_sha256"])
                    and _timestamp(row["issued_at"])
                    <= _timestamp(row["expires_at"])
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise _unavailable("replay_store_integrity")

    def consume(
        self,
        credential_id: str,
        credential_version: str,
        nonce: str,
        proof_sha256: str,
        *,
        issued_at: float,
        expires_at: float,
        now: float,
    ) -> bool:
        """Return true exactly once for one valid proof identity."""

        if (
            not _token(credential_id)
            or not _token(credential_version)
            or not isinstance(nonce, str)
            or NONCE.fullmatch(nonce) is None
            or not _sha256(proof_sha256)
        ):
            raise _unavailable("replay_store_request_invalid")
        try:
            issued = _timestamp(issued_at)
            expiry = _timestamp(expires_at)
            current = _timestamp(now)
        except ValueError:
            raise _unavailable("replay_store_request_invalid") from None
        if issued > expiry or current > expiry:
            raise _unavailable("replay_store_request_invalid")
        with self._lock:
            if self.connection.in_transaction:
                raise _unavailable("replay_store_transaction_nested")
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self._validate()
                self.connection.execute(
                    "DELETE FROM cms_source_delivery_hmac_nonces "
                    "WHERE expires_at < ?",
                    (current,),
                )
                inserted = self.connection.execute("""
                    INSERT OR IGNORE INTO cms_source_delivery_hmac_nonces (
                        credential_id, credential_version, nonce, proof_sha256,
                        issued_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    credential_id, credential_version, nonce, proof_sha256,
                    issued, expiry,
                )).rowcount == 1
                self.connection.commit()
                return inserted
            except SourceDeliveryHMACAuthenticationUnavailable:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
            except sqlite3.Error:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise _unavailable("replay_store_unavailable") from None

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            try:
                self._validate()
                count = self.connection.execute(
                    "SELECT COUNT(*) FROM cms_source_delivery_hmac_nonces"
                ).fetchone()[0]
            except SourceDeliveryHMACAuthenticationUnavailable:
                raise
            except sqlite3.Error:
                raise _unavailable("replay_store_unavailable") from None
        return {
            "schema": "blun.cms-source-delivery-hmac-replay-health.v1",
            "status": "ok",
            "consumed_nonces": int(count),
        }


def _clock(clock: Callable[[], float | int]) -> float:
    try:
        return _timestamp(clock())
    except Exception:
        raise _unavailable("clock_unavailable") from None


def _nonce(factory: Callable[[], str]) -> str:
    try:
        value = factory()
    except Exception:
        raise _unavailable("nonce_unavailable") from None
    if not isinstance(value, str) or NONCE.fullmatch(value) is None:
        raise _unavailable("nonce_unavailable")
    return value


def _binding(
    credential: HMACCredential,
    *,
    origin: str,
    sidecar_capabilities_sha256: str,
    remote_capabilities_sha256: str,
    method: str,
    path: str,
    scope: str,
    body_sha256: str,
    issued_at: int,
    nonce: str,
    site_id: str | None,
    event_id: str | None,
    request_id: str | None,
    payload_sha256: str | None,
    idempotency_key: str | None,
    source_payload_sha256: str | None,
) -> dict[str, Any]:
    return {
        "schema": PROOF_SCHEMA,
        "algorithm": ALGORITHM,
        "origin": origin,
        "sidecar_capabilities_sha256": sidecar_capabilities_sha256,
        "remote_capabilities_sha256": remote_capabilities_sha256,
        "method": method,
        "path": path,
        "scope": scope,
        "body_sha256": body_sha256,
        "principal_id": credential.principal_id,
        "credential_id": credential.credential_id,
        "credential_version": credential.credential_version,
        "issued_at": issued_at,
        "nonce": nonce,
        "site_id": site_id,
        "event_id": event_id,
        "request_id": request_id,
        "payload_sha256": payload_sha256,
        "idempotency_key": idempotency_key,
        "source_payload_sha256": source_payload_sha256,
    }


class SourceDeliveryHMACSigner:
    """Produce request-bound authentication headers for the reference client."""

    def __init__(
        self,
        credential: HMACCredential,
        *,
        origin: str,
        sidecar_capabilities_sha256: str,
        remote_capabilities_sha256: str,
        clock: Callable[[], float | int] = time.time,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        allow_loopback_http: bool = False,
    ):
        if not isinstance(credential, HMACCredential):
            raise TypeError("credential must be HMACCredential")
        if not callable(clock) or not callable(nonce_factory):
            raise TypeError("clock and nonce factory must be callable")
        self.credential = credential
        self.origin = _CLIENT._endpoint(origin, allow_loopback_http)
        if not _sha256(sidecar_capabilities_sha256):
            raise ValueError("sidecar capability digest is invalid")
        if not _sha256(remote_capabilities_sha256):
            raise ValueError("remote capability digest is invalid")
        self.sidecar_capabilities_sha256 = sidecar_capabilities_sha256
        self.remote_capabilities_sha256 = remote_capabilities_sha256
        self.clock = clock
        self.nonce_factory = nonce_factory

    def __repr__(self) -> str:
        return "SourceDeliveryHMACSigner(configured=True, secret=<redacted>)"

    def __call__(self, context: Mapping[str, Any]) -> Mapping[str, str]:
        base_fields = {
            "schema", "method", "origin", "path", "scope", "body_sha256",
        }
        if not isinstance(context, Mapping):
            raise _unavailable("signing_context_invalid")
        path = context.get("path")
        tenant = path in _HTTP.TENANT_PATHS
        expected_fields = base_fields | (
            {"site_id", "event_id", "request_id", "payload_sha256"}
            if tenant else set()
        )
        if set(context) != expected_fields:
            raise _unavailable("signing_context_invalid")
        try:
            method = _HTTP.METHODS[path]
            scope = _HTTP.SCOPES[path]
        except (KeyError, TypeError):
            raise _unavailable("signing_context_invalid") from None
        if (
            context.get("schema") != _CLIENT.AUTH_CONTEXT_SCHEMA
            or context.get("method") != method
            or context.get("origin") != self.origin
            or context.get("scope") != scope
            or not _sha256(context.get("body_sha256"))
            or scope not in self.credential.scopes
        ):
            raise _unavailable("signing_context_invalid")
        site_id = event_id = request_id = payload_sha256 = None
        if tenant:
            site_id = context["site_id"]
            event_id = context["event_id"]
            request_id = context["request_id"]
            payload_sha256 = context["payload_sha256"]
            if (
                not all(_token(value) for value in (
                    site_id, event_id, request_id,
                ))
                or not _sha256(payload_sha256)
                or site_id != self.credential.site_id
            ):
                raise _unavailable("signing_context_invalid")
        issued_at = int(_clock(self.clock))
        nonce = _nonce(self.nonce_factory)
        write = path in {_HTTP.CHANGE_PATH, _HTTP.REMOVAL_PATH}
        binding = _binding(
            self.credential,
            origin=self.origin,
            sidecar_capabilities_sha256=self.sidecar_capabilities_sha256,
            remote_capabilities_sha256=self.remote_capabilities_sha256,
            method=method,
            path=path,
            scope=scope,
            body_sha256=context["body_sha256"],
            issued_at=issued_at,
            nonce=nonce,
            site_id=site_id,
            event_id=event_id,
            request_id=request_id,
            payload_sha256=payload_sha256,
            idempotency_key=request_id if write else None,
            source_payload_sha256=payload_sha256 if write else None,
        )
        signature = hmac.new(
            self.credential.secret, _canonical(binding), hashlib.sha256,
        ).hexdigest()
        headers = {
            HEADER_CREDENTIAL_ID: self.credential.credential_id,
            HEADER_CREDENTIAL_VERSION: self.credential.credential_version,
            HEADER_TIMESTAMP: str(issued_at),
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: signature,
        }
        if tenant:
            headers.update({
                HEADER_SITE_ID: site_id,
                HEADER_EVENT_ID: event_id,
                HEADER_REQUEST_ID: request_id,
                HEADER_PAYLOAD_SHA256: payload_sha256,
            })
        return headers


class SourceDeliveryHMACVerifier:
    """Authenticate exact sidecar requests and consume each proof once."""

    def __init__(
        self,
        credentials: Iterable[HMACCredential],
        replay_store: DurableHMACReplayStore,
        *,
        origin: str,
        sidecar_capabilities_sha256: str,
        remote_capabilities_sha256: str,
        clock: Callable[[], float | int] = time.time,
        max_age_seconds: float | int = 60,
        future_skew_seconds: float | int = 5,
        allow_loopback_http: bool = False,
    ):
        try:
            values = tuple(credentials)
        except TypeError:
            raise TypeError("credentials must be iterable") from None
        if not values or any(not isinstance(item, HMACCredential) for item in values):
            raise TypeError("credentials must contain HMACCredential values")
        keyed = {
            (item.credential_id, item.credential_version): item
            for item in values
        }
        if len(keyed) != len(values):
            raise ValueError("credential generation is duplicated")
        if not isinstance(replay_store, DurableHMACReplayStore):
            raise TypeError("replay_store must be DurableHMACReplayStore")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.origin = _CLIENT._endpoint(origin, allow_loopback_http)
        if not _sha256(sidecar_capabilities_sha256):
            raise ValueError("sidecar capability digest is invalid")
        if not _sha256(remote_capabilities_sha256):
            raise ValueError("remote capability digest is invalid")
        try:
            age = _timestamp(max_age_seconds)
            skew = _timestamp(future_skew_seconds)
        except ValueError:
            raise ValueError("authentication time window is invalid") from None
        if not 0 < age <= MAX_CLOCK_SKEW_SECONDS or skew > age:
            raise ValueError("authentication time window is invalid")
        self.credentials = keyed
        self.replay_store = replay_store
        self.sidecar_capabilities_sha256 = sidecar_capabilities_sha256
        self.remote_capabilities_sha256 = remote_capabilities_sha256
        self.clock = clock
        self.max_age_seconds = age
        self.future_skew_seconds = skew

    def __repr__(self) -> str:
        return "SourceDeliveryHMACVerifier(configured=True, secrets=<redacted>)"

    @staticmethod
    def _headers(request: Mapping[str, Any]) -> dict[str, str] | None:
        items = request.get("headers")
        if not isinstance(items, list):
            return None
        result: dict[str, str] = {}
        for item in items:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(part, str) for part in item)
            ):
                return None
            name, value = item[0].lower(), item[1]
            if name in result:
                return None
            result[name] = value
        return result

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, str] | None:
        if (
            not isinstance(request, Mapping)
            or set(request) != {
                "schema", "method", "path", "headers", "body_sha256",
            }
            or request.get("schema") != _HTTP.AUTH_REQUEST_SCHEMA
        ):
            return None
        path = request.get("path")
        try:
            method = _HTTP.METHODS[path]
            scope = _HTTP.SCOPES[path]
        except (KeyError, TypeError):
            return None
        if request.get("method") != method or not _sha256(request.get("body_sha256")):
            return None
        headers = self._headers(request)
        if headers is None or not _COMMON_HEADERS.issubset(headers):
            return None
        tenant = path in _HTTP.TENANT_PATHS
        if tenant != _TENANT_HEADERS.issubset(headers):
            return None
        if not tenant and _TENANT_HEADERS.intersection(headers):
            return None
        credential = self.credentials.get((
            headers[HEADER_CREDENTIAL_ID.lower()],
            headers[HEADER_CREDENTIAL_VERSION.lower()],
        ))
        if credential is None or scope not in credential.scopes:
            return None
        try:
            issued_text = headers[HEADER_TIMESTAMP.lower()]
            if not issued_text.isascii() or not issued_text.isdecimal():
                return None
            issued_at = int(issued_text)
            nonce = headers[HEADER_NONCE.lower()]
            signature = headers[HEADER_SIGNATURE.lower()]
            if (
                NONCE.fullmatch(nonce) is None
                or SIGNATURE.fullmatch(signature) is None
            ):
                return None
        except (KeyError, TypeError):
            return None
        current = _clock(self.clock)
        if (
            issued_at < current - self.max_age_seconds
            or issued_at > current + self.future_skew_seconds
        ):
            return None
        site_id = event_id = request_id = payload_sha256 = None
        if tenant:
            site_id = headers[HEADER_SITE_ID.lower()]
            event_id = headers[HEADER_EVENT_ID.lower()]
            request_id = headers[HEADER_REQUEST_ID.lower()]
            payload_sha256 = headers[HEADER_PAYLOAD_SHA256.lower()]
            if (
                not all(_token(value) for value in (
                    site_id, event_id, request_id,
                ))
                or not _sha256(payload_sha256)
                or site_id != credential.site_id
            ):
                return None
        write = path in {_HTTP.CHANGE_PATH, _HTTP.REMOVAL_PATH}
        idempotency_key = headers.get("idempotency-key")
        source_payload_sha256 = headers.get(
            "x-localization-source-payload-sha256"
        )
        if write:
            if (
                idempotency_key != request_id
                or source_payload_sha256 != payload_sha256
            ):
                return None
        elif idempotency_key is not None or source_payload_sha256 is not None:
            return None
        binding = _binding(
            credential,
            origin=self.origin,
            sidecar_capabilities_sha256=self.sidecar_capabilities_sha256,
            remote_capabilities_sha256=self.remote_capabilities_sha256,
            method=method,
            path=path,
            scope=scope,
            body_sha256=request["body_sha256"],
            issued_at=issued_at,
            nonce=nonce,
            site_id=site_id,
            event_id=event_id,
            request_id=request_id,
            payload_sha256=payload_sha256,
            idempotency_key=idempotency_key,
            source_payload_sha256=source_payload_sha256,
        )
        proof = _canonical(binding)
        expected = hmac.new(
            credential.secret, proof, hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        proof_sha256 = hashlib.sha256(proof).hexdigest()
        if not self.replay_store.consume(
            credential.credential_id,
            credential.credential_version,
            nonce,
            proof_sha256,
            issued_at=float(issued_at),
            expires_at=float(issued_at) + self.max_age_seconds,
            now=current,
        ):
            return None
        principal = {
            "schema": (
                _HTTP.TENANT_PRINCIPAL_SCHEMA
                if tenant else _HTTP.PRINCIPAL_SCHEMA
            ),
            "principal_id": credential.principal_id,
            "credential_id": credential.credential_id,
            "credential_version": credential.credential_version,
            "scope": scope,
        }
        if tenant:
            principal["site_id"] = site_id
        return principal
