#!/usr/bin/env python3
"""Durable SQLite host callbacks for the fail-closed CMS receiver.

The trusted host registers its current source and tombstone expectations. The
receiver invokes this store only after transport and signature verification.
Every callback rechecks the current binding inside one SQLite write transaction
so a concurrent source change cannot publish or delete the wrong generation.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping


SCHEMA_VERSION = 1
MAX_JSON_BYTES = 4_000_000
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CONTENT_TYPES = {
    "headline", "cta", "marketing", "ui", "documentation", "seo", "legal",
    "commercial",
}
PUBLICATION_EXPECTATION_FIELDS = (
    "event_id", "site_id", "website_version", "plan_id", "source_id",
    "source_revision", "source_sequence", "source_sha256", "required_locales",
    "content_type", "commercial_profile",
)
TOMBSTONE_EXPECTATION_FIELDS = (
    "tombstone_id", "event_id", "site_id", "website_version", "plan_id",
    "source_id", "source_sequence", "publication_delivery_id",
    "publication_payload_sha256", "locales",
)


class CMSReceiverStoreBlocked(RuntimeError):
    """A private durable-host failure; the HTTP receiver redacts its detail."""


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise CMSReceiverStoreBlocked("store JSON is invalid") from error
    raw = encoded.encode("utf-8")
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise CMSReceiverStoreBlocked("store JSON is outside the supported size")
    return encoded


def _decode_json(value: Any, *, field: str) -> Any:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_JSON_BYTES:
        raise CMSReceiverStoreBlocked(f"{field} is invalid")
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as error:
        raise CMSReceiverStoreBlocked(f"{field} is invalid") from error
    if _canonical_json(decoded) != value:
        raise CMSReceiverStoreBlocked(f"{field} is not canonical")
    return decoded


def _mapping(value: Any, fields: tuple[str, ...], *, name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
        if set(result) != set(fields):
            raise CMSReceiverStoreBlocked(f"{name} fields are invalid")
        return result
    missing = object()
    result = {field: getattr(value, field, missing) for field in fields}
    if any(item is missing for item in result.values()):
        raise CMSReceiverStoreBlocked(f"{name} is invalid")
    return result


def _token(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or TOKEN.fullmatch(value) is None
        or not unicodedata.is_normalized("NFC", value)
    ):
        raise CMSReceiverStoreBlocked(f"{field} is invalid")
    return value


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CMSReceiverStoreBlocked(f"{field} is invalid")
    return value


def _sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CMSReceiverStoreBlocked("source_sequence is invalid")
    return value


def _locales(value: Any, *, field: str) -> tuple[str, ...]:
    if isinstance(value, list):
        value = tuple(value)
    if (
        not isinstance(value, tuple)
        or not value
        or value != tuple(sorted(set(value)))
        or any(_token(locale, field=field) != locale for locale in value)
    ):
        raise CMSReceiverStoreBlocked(f"{field} is invalid")
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CMSReceiverStoreBlocked("clock returned an invalid timestamp")
    result = float(value)
    if result < 0 or result != result or result in {float("inf"), float("-inf")}:
        raise CMSReceiverStoreBlocked("clock returned an invalid timestamp")
    return result


def _binding_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _publication_expectation(value: Any) -> dict[str, Any]:
    result = _mapping(
        value, PUBLICATION_EXPECTATION_FIELDS, name="publication expectation",
    )
    for field in (
        "event_id", "site_id", "website_version", "plan_id", "source_id",
        "source_revision",
    ):
        result[field] = _token(result[field], field=field)
    result["source_sequence"] = _sequence(result["source_sequence"])
    result["source_sha256"] = _sha256(
        result["source_sha256"], field="source_sha256",
    )
    result["required_locales"] = _locales(
        result["required_locales"], field="required_locales",
    )
    result["content_type"] = _token(result["content_type"], field="content_type")
    if result["content_type"] not in CONTENT_TYPES:
        raise CMSReceiverStoreBlocked("content_type is invalid")
    commercial = result["commercial_profile"]
    if commercial is not None:
        commercial = _token(commercial, field="commercial_profile")
    if (result["content_type"] == "commercial") != (commercial is not None):
        raise CMSReceiverStoreBlocked("commercial profile scope is invalid")
    result["commercial_profile"] = commercial
    return result


def _tombstone_expectation(value: Any) -> dict[str, Any]:
    result = _mapping(
        value, TOMBSTONE_EXPECTATION_FIELDS, name="tombstone expectation",
    )
    for field in (
        "tombstone_id", "event_id", "site_id", "website_version", "plan_id",
        "source_id", "publication_delivery_id",
    ):
        result[field] = _token(result[field], field=field)
    result["source_sequence"] = _sequence(result["source_sequence"])
    result["publication_payload_sha256"] = _sha256(
        result["publication_payload_sha256"],
        field="publication_payload_sha256",
    )
    result["locales"] = _locales(result["locales"], field="locales")
    return result


def _verified(value: Any, *, kind: str) -> tuple[str, str, dict[str, Any], str]:
    delivery_id = _token(getattr(value, "delivery_id", None), field="delivery_id")
    payload_sha256 = _sha256(
        getattr(value, "payload_sha256", None), field="payload_sha256",
    )
    payload = getattr(value, "payload", None)
    if not isinstance(payload, dict):
        raise CMSReceiverStoreBlocked(f"verified {kind} payload is invalid")
    payload_json = _canonical_json(payload)
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
        raise CMSReceiverStoreBlocked(f"verified {kind} hash is invalid")
    if payload.get("delivery_id") != delivery_id:
        raise CMSReceiverStoreBlocked(f"verified {kind} delivery is invalid")
    return delivery_id, payload_sha256, payload, payload_json


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise CMSReceiverStoreBlocked("store cannot join an external transaction")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class DurableCMSReceiverStore:
    """Dedicated crash-safe store implementing every receiver host callback."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], float | int] = time.time,
    ):
        if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
            raise CMSReceiverStoreBlocked("connection must be an idle SQLite connection")
        if not callable(clock):
            raise CMSReceiverStoreBlocked("clock must be callable")
        self.connection = connection
        self.clock = clock
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA secure_delete = ON")
        if int(self.connection.execute("PRAGMA secure_delete").fetchone()[0]) != 1:
            raise CMSReceiverStoreBlocked("secure deletion is unavailable")
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise CMSReceiverStoreBlocked("unsupported CMS receiver store schema")
        if version == 0:
            self._create_schema()
        self._verify_schema()

    def _create_schema(self) -> None:
        with _transaction(self.connection):
            version = int(
                self.connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version == SCHEMA_VERSION:
                return
            if version != 0:
                raise CMSReceiverStoreBlocked("CMS receiver store schema changed")
            self.connection.execute("""
                CREATE TABLE cms_receiver_sources (
                    site_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    website_version TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL CHECK (source_sequence > 0),
                    source_sha256 TEXT NOT NULL,
                    required_locales_json TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    commercial_profile TEXT,
                    expectation_sha256 TEXT NOT NULL,
                    active_delivery_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (site_id, source_id)
                )
            """)
            self.connection.execute("""
                CREATE TABLE cms_receiver_publications (
                    delivery_id TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL UNIQUE,
                    site_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL CHECK (source_sequence > 0),
                    source_revision TEXT NOT NULL,
                    payload_json TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'superseded', 'deleted')
                    ),
                    tombstone_delivery_id TEXT,
                    tombstone_payload_sha256 TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (site_id, source_id, source_sequence)
                )
            """)
            self.connection.execute("""
                CREATE TABLE cms_receiver_localizations (
                    delivery_id TEXT NOT NULL,
                    locale TEXT NOT NULL,
                    target_text TEXT NOT NULL,
                    target_sha256 TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    approval_expires_at REAL NOT NULL,
                    release_evidence_json TEXT NOT NULL,
                    PRIMARY KEY (delivery_id, locale),
                    FOREIGN KEY (delivery_id)
                        REFERENCES cms_receiver_publications (delivery_id)
                        ON DELETE CASCADE
                )
            """)
            self.connection.execute("""
                CREATE TABLE cms_receiver_tombstones (
                    tombstone_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    website_version TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL CHECK (source_sequence > 0),
                    publication_delivery_id TEXT NOT NULL UNIQUE,
                    publication_payload_sha256 TEXT NOT NULL,
                    locales_json TEXT NOT NULL,
                    expectation_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'deleted')),
                    delivery_id TEXT UNIQUE,
                    payload_sha256 TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.connection.execute(
                "CREATE INDEX cms_receiver_publications_source "
                "ON cms_receiver_publications (site_id, source_id, status)"
            )
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _verify_schema(self) -> None:
        if int(self.connection.execute("PRAGMA secure_delete").fetchone()[0]) != 1:
            raise CMSReceiverStoreBlocked("secure deletion was disabled")
        expected = {
            "cms_receiver_sources": (
                "site_id", "source_id", "event_id", "website_version", "plan_id",
                "source_revision", "source_sequence", "source_sha256",
                "required_locales_json", "content_type", "commercial_profile",
                "expectation_sha256", "active_delivery_id", "created_at",
                "updated_at",
            ),
            "cms_receiver_publications": (
                "delivery_id", "payload_sha256", "site_id", "source_id",
                "source_sequence", "source_revision", "payload_json", "status",
                "tombstone_delivery_id", "tombstone_payload_sha256", "created_at",
                "updated_at",
            ),
            "cms_receiver_localizations": (
                "delivery_id", "locale", "target_text", "target_sha256",
                "approval_id", "approval_expires_at", "release_evidence_json",
            ),
            "cms_receiver_tombstones": (
                "tombstone_id", "event_id", "site_id", "website_version",
                "plan_id", "source_id", "source_sequence",
                "publication_delivery_id", "publication_payload_sha256",
                "locales_json", "expectation_sha256", "status", "delivery_id",
                "payload_sha256", "created_at", "updated_at",
            ),
        }
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise CMSReceiverStoreBlocked("CMS receiver store schema version is invalid")
        for table, columns in expected.items():
            actual = tuple(
                row[1] for row in self.connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            )
            if actual != columns:
                raise CMSReceiverStoreBlocked(f"CMS receiver store table {table} drifted")

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> dict[str, Any]:
        value = {
            "event_id": row["event_id"],
            "site_id": row["site_id"],
            "website_version": row["website_version"],
            "plan_id": row["plan_id"],
            "source_id": row["source_id"],
            "source_revision": row["source_revision"],
            "source_sequence": row["source_sequence"],
            "source_sha256": row["source_sha256"],
            "required_locales": tuple(_decode_json(
                row["required_locales_json"], field="required_locales_json",
            )),
            "content_type": row["content_type"],
            "commercial_profile": row["commercial_profile"],
        }
        result = _publication_expectation(value)
        if _binding_sha256(result) != row["expectation_sha256"]:
            raise CMSReceiverStoreBlocked("source expectation hash is invalid")
        return result

    @staticmethod
    def _tombstone_from_row(row: sqlite3.Row) -> dict[str, Any]:
        value = {
            "tombstone_id": row["tombstone_id"],
            "event_id": row["event_id"],
            "site_id": row["site_id"],
            "website_version": row["website_version"],
            "plan_id": row["plan_id"],
            "source_id": row["source_id"],
            "source_sequence": row["source_sequence"],
            "publication_delivery_id": row["publication_delivery_id"],
            "publication_payload_sha256": row["publication_payload_sha256"],
            "locales": tuple(_decode_json(row["locales_json"], field="locales_json")),
        }
        result = _tombstone_expectation(value)
        if _binding_sha256(result) != row["expectation_sha256"]:
            raise CMSReceiverStoreBlocked("tombstone expectation hash is invalid")
        return result

    def _validate_publication_row(
        self, row: sqlite3.Row,
    ) -> dict[str, Any] | None:
        delivery_id = _token(row["delivery_id"], field="delivery_id")
        payload_sha256 = _sha256(row["payload_sha256"], field="payload_sha256")
        _token(row["site_id"], field="site_id")
        _token(row["source_id"], field="source_id")
        _sequence(row["source_sequence"])
        _token(row["source_revision"], field="source_revision")
        localizations = self.connection.execute("""
            SELECT locale, target_text, target_sha256, approval_id,
                   approval_expires_at, release_evidence_json
            FROM cms_receiver_localizations
            WHERE delivery_id = ? ORDER BY locale
        """, (delivery_id,)).fetchall()
        if row["status"] in {"deleted", "superseded"}:
            if (
                row["payload_json"] is not None
                or localizations
            ):
                raise CMSReceiverStoreBlocked(
                    "inactive publication retained localized content"
                )
            if row["status"] == "superseded":
                if (
                    row["tombstone_delivery_id"] is not None
                    or row["tombstone_payload_sha256"] is not None
                ):
                    raise CMSReceiverStoreBlocked(
                        "superseded publication is invalid"
                    )
                return None
            if (
                _token(
                    row["tombstone_delivery_id"], field="tombstone_delivery_id",
                ) != row["tombstone_delivery_id"]
                or _sha256(
                    row["tombstone_payload_sha256"],
                    field="tombstone_payload_sha256",
                ) != row["tombstone_payload_sha256"]
            ):
                raise CMSReceiverStoreBlocked("deleted publication is invalid")
            return None
        if (
            row["status"] != "active"
            or row["tombstone_delivery_id"] is not None
            or row["tombstone_payload_sha256"] is not None
        ):
            raise CMSReceiverStoreBlocked("publication state is invalid")
        payload = _decode_json(row["payload_json"], field="payload_json")
        if (
            not isinstance(payload, dict)
            or payload.get("delivery_id") != delivery_id
            or payload.get("site_id") != row["site_id"]
            or payload.get("source_id") != row["source_id"]
            or payload.get("source_sequence") != row["source_sequence"]
            or payload.get("source_revision") != row["source_revision"]
            or hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
            != payload_sha256
        ):
            raise CMSReceiverStoreBlocked("stored publication binding is invalid")
        expected = payload.get("localizations")
        if not isinstance(expected, list) or not all(
            isinstance(item, dict) for item in expected
        ):
            raise CMSReceiverStoreBlocked("stored locale bundle is incomplete")
        locales = [item.get("locale") for item in expected]
        if any(not isinstance(locale, str) for locale in locales):
            raise CMSReceiverStoreBlocked("stored locale bundle is incomplete")
        if len(localizations) != len(expected) or locales != sorted(set(locales)):
            raise CMSReceiverStoreBlocked("stored locale bundle is incomplete")
        for stored, item in zip(localizations, expected):
            if not isinstance(item, dict):
                raise CMSReceiverStoreBlocked("stored locale is invalid")
            target_text = stored["target_text"]
            evidence = _decode_json(
                stored["release_evidence_json"], field="release_evidence_json",
            )
            if (
                stored["locale"] != item.get("locale")
                or target_text != item.get("target_text")
                or stored["target_sha256"] != item.get("target_sha256")
                or stored["approval_id"] != item.get("approval_id")
                or stored["approval_expires_at"] != item.get("approval_expires_at")
                or evidence != item.get("release_evidence")
                or not isinstance(target_text, str)
                or hashlib.sha256(target_text.encode("utf-8")).hexdigest()
                != stored["target_sha256"]
            ):
                raise CMSReceiverStoreBlocked("stored locale binding is invalid")
        return payload

    @staticmethod
    def _assert_publication_binding(
        payload: Mapping[str, Any], expectation: Mapping[str, Any],
    ) -> None:
        for field in (
            "event_id", "site_id", "website_version", "plan_id", "source_id",
            "source_revision", "source_sequence", "source_sha256",
        ):
            if payload.get(field) != expectation[field]:
                raise CMSReceiverStoreBlocked("publication is no longer current")
        localizations = payload.get("localizations")
        if not isinstance(localizations, list):
            raise CMSReceiverStoreBlocked("publication localizations are invalid")
        locales = tuple(item.get("locale") for item in localizations if isinstance(item, dict))
        if locales != expectation["required_locales"] or len(locales) != len(localizations):
            raise CMSReceiverStoreBlocked("publication locale set is no longer current")
        for item in localizations:
            evidence = item.get("release_evidence")
            commercial_quality = (
                evidence.get("commercial_quality_profile")
                if isinstance(evidence, dict)
                else None
            )
            if expectation["content_type"] == "commercial":
                quality_valid = (
                    isinstance(commercial_quality, dict)
                    and set(commercial_quality) == {"profile", "version", "sha256"}
                    and commercial_quality.get("profile")
                    == expectation["commercial_profile"]
                    and isinstance(commercial_quality.get("version"), str)
                    and TOKEN.fullmatch(commercial_quality["version"]) is not None
                    and isinstance(commercial_quality.get("sha256"), str)
                    and SHA256.fullmatch(commercial_quality["sha256"]) is not None
                )
            else:
                quality_valid = commercial_quality is None
            if (
                not isinstance(evidence, dict)
                or evidence.get("content_type") != expectation["content_type"]
                or evidence.get("commercial_profile")
                != expectation["commercial_profile"]
                or not quality_valid
            ):
                raise CMSReceiverStoreBlocked("publication scope is no longer current")

    def register_source(self, expectation: Any) -> Mapping[str, Any]:
        """Register or monotonically advance one trusted current source."""

        value = _publication_expectation(expectation)
        now = _timestamp(self.clock())
        locales_json = _canonical_json(list(value["required_locales"]))
        expectation_sha256 = _binding_sha256(value)
        with _transaction(self.connection):
            self._verify_schema()
            row = self.connection.execute(
                "SELECT * FROM cms_receiver_sources WHERE site_id = ? AND source_id = ?",
                (value["site_id"], value["source_id"]),
            ).fetchone()
            if row is not None:
                current = self._source_from_row(row)
                if value["source_sequence"] < current["source_sequence"]:
                    raise CMSReceiverStoreBlocked("source sequence moved backwards")
                if value["source_sequence"] == current["source_sequence"]:
                    if value != current:
                        raise CMSReceiverStoreBlocked("source sequence was reused")
                    return {
                        "site_id": value["site_id"],
                        "source_id": value["source_id"],
                        "source_sequence": value["source_sequence"],
                        "status": "current",
                    }
                if row["active_delivery_id"] is not None and self.connection.execute(
                    "SELECT 1 FROM cms_receiver_tombstones "
                    "WHERE publication_delivery_id = ? AND status = 'pending'",
                    (row["active_delivery_id"],),
                ).fetchone() is not None:
                    raise CMSReceiverStoreBlocked(
                        "source cannot advance during a pending tombstone"
                    )
                try:
                    self.connection.execute("""
                        UPDATE cms_receiver_sources
                        SET event_id = ?, website_version = ?, plan_id = ?,
                            source_revision = ?, source_sequence = ?, source_sha256 = ?,
                            required_locales_json = ?, content_type = ?,
                            commercial_profile = ?, expectation_sha256 = ?,
                            updated_at = ?
                        WHERE site_id = ? AND source_id = ?
                    """, (
                        value["event_id"], value["website_version"], value["plan_id"],
                        value["source_revision"], value["source_sequence"],
                        value["source_sha256"], locales_json, value["content_type"],
                        value["commercial_profile"], expectation_sha256, now,
                        value["site_id"], value["source_id"],
                    ))
                except sqlite3.IntegrityError as error:
                    raise CMSReceiverStoreBlocked("source identity collided") from error
            else:
                try:
                    self.connection.execute("""
                        INSERT INTO cms_receiver_sources (
                            site_id, source_id, event_id, website_version, plan_id,
                            source_revision, source_sequence, source_sha256,
                            required_locales_json, content_type, commercial_profile,
                            expectation_sha256, active_delivery_id, created_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """, (
                        value["site_id"], value["source_id"], value["event_id"],
                        value["website_version"], value["plan_id"],
                        value["source_revision"], value["source_sequence"],
                        value["source_sha256"], locales_json, value["content_type"],
                        value["commercial_profile"], expectation_sha256, now, now,
                    ))
                except sqlite3.IntegrityError as error:
                    raise CMSReceiverStoreBlocked("source identity collided") from error
        return {
            "site_id": value["site_id"],
            "source_id": value["source_id"],
            "source_sequence": value["source_sequence"],
            "status": "current",
        }

    def resolve_publication_expectation(self, publication: Any) -> Mapping[str, Any]:
        _, _, payload, _ = _verified(publication, kind="publication")
        site_id = _token(payload.get("site_id"), field="site_id")
        source_id = _token(payload.get("source_id"), field="source_id")
        self._verify_schema()
        row = self.connection.execute(
            "SELECT * FROM cms_receiver_sources WHERE site_id = ? AND source_id = ?",
            (site_id, source_id),
        ).fetchone()
        if row is None:
            raise CMSReceiverStoreBlocked("source expectation is missing")
        return self._source_from_row(row)

    def commit(self, publication: Any) -> Mapping[str, Any]:
        """Install a complete bundle, then atomically scrub its predecessor."""

        delivery_id, payload_sha256, payload, payload_json = _verified(
            publication, kind="publication",
        )
        now = _timestamp(self.clock())
        with _transaction(self.connection):
            self._verify_schema()
            row = self.connection.execute(
                "SELECT * FROM cms_receiver_sources WHERE site_id = ? AND source_id = ?",
                (payload.get("site_id"), payload.get("source_id")),
            ).fetchone()
            if row is None:
                raise CMSReceiverStoreBlocked("source expectation is missing")
            current = self._source_from_row(row)
            self._assert_publication_binding(payload, current)
            existing = self.connection.execute(
                "SELECT * FROM cms_receiver_publications WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_sha256"] == payload_sha256
                    and existing["status"] == "active"
                    and row["active_delivery_id"] == delivery_id
                    and self._validate_publication_row(existing) == payload
                ):
                    return {
                        "delivery_id": delivery_id,
                        "payload_sha256": payload_sha256,
                        "status": "committed",
                    }
                raise CMSReceiverStoreBlocked("publication delivery collided")
            if self.connection.execute(
                "SELECT 1 FROM cms_receiver_publications "
                "WHERE site_id = ? AND source_id = ? AND source_sequence = ?",
                (current["site_id"], current["source_id"], current["source_sequence"]),
            ).fetchone() is not None:
                raise CMSReceiverStoreBlocked("publication generation collided")
            old_delivery_id = row["active_delivery_id"]
            if old_delivery_id is not None:
                changed = self.connection.execute("""
                    UPDATE cms_receiver_publications
                    SET status = 'superseded', updated_at = ?
                    WHERE delivery_id = ? AND status = 'active'
                """, (now, old_delivery_id)).rowcount
                if changed != 1:
                    raise CMSReceiverStoreBlocked("active publication state is invalid")
            try:
                self.connection.execute("""
                    INSERT INTO cms_receiver_publications (
                        delivery_id, payload_sha256, site_id, source_id,
                        source_sequence, source_revision, payload_json, status,
                        tombstone_delivery_id, tombstone_payload_sha256,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?)
                """, (
                    delivery_id, payload_sha256, current["site_id"],
                    current["source_id"], current["source_sequence"],
                    current["source_revision"], payload_json, now, now,
                ))
                for item in payload["localizations"]:
                    self.connection.execute("""
                        INSERT INTO cms_receiver_localizations (
                            delivery_id, locale, target_text, target_sha256,
                            approval_id, approval_expires_at, release_evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        delivery_id, item["locale"], item["target_text"],
                        item["target_sha256"], item["approval_id"],
                        item["approval_expires_at"],
                        _canonical_json(item["release_evidence"]),
                    ))
            except (KeyError, TypeError, sqlite3.IntegrityError) as error:
                raise CMSReceiverStoreBlocked("publication bundle is invalid") from error
            changed = self.connection.execute("""
                UPDATE cms_receiver_sources
                SET active_delivery_id = ?, updated_at = ?
                WHERE site_id = ? AND source_id = ? AND source_sequence = ?
                  AND source_revision = ? AND source_sha256 = ?
            """, (
                delivery_id, now, current["site_id"], current["source_id"],
                current["source_sequence"], current["source_revision"],
                current["source_sha256"],
            )).rowcount
            if changed != 1:
                raise CMSReceiverStoreBlocked("source changed during publication")
            if old_delivery_id is not None:
                try:
                    self.connection.execute(
                        "DELETE FROM cms_receiver_localizations WHERE delivery_id = ?",
                        (old_delivery_id,),
                    )
                    changed = self.connection.execute("""
                        UPDATE cms_receiver_publications
                        SET payload_json = NULL, updated_at = ?
                        WHERE delivery_id = ? AND status = 'superseded'
                    """, (now, old_delivery_id)).rowcount
                except sqlite3.Error as error:
                    raise CMSReceiverStoreBlocked(
                        "superseded publication cleanup failed"
                    ) from error
                if changed != 1:
                    raise CMSReceiverStoreBlocked(
                        "superseded publication cleanup was not atomic"
                    )
        return {
            "delivery_id": delivery_id,
            "payload_sha256": payload_sha256,
            "status": "committed",
        }

    def read_active_bundle(self, expectation: Any) -> Mapping[str, Any] | None:
        """Read only the active bundle for one exact trusted source binding."""

        expected = _publication_expectation(expectation)
        site_id = expected["site_id"]
        source_id = expected["source_id"]
        self._verify_schema()
        source = self.connection.execute(
            "SELECT * FROM cms_receiver_sources "
            "WHERE site_id = ? AND source_id = ?",
            (site_id, source_id),
        ).fetchone()
        if source is None:
            return None
        self._source_from_row(source)
        if source["active_delivery_id"] is None:
            return None
        publication = self.connection.execute(
            "SELECT * FROM cms_receiver_publications WHERE delivery_id = ?",
            (source["active_delivery_id"],),
        ).fetchone()
        if (
            publication is None
            or publication["status"] != "active"
            or publication["site_id"] != site_id
            or publication["source_id"] != source_id
        ):
            raise CMSReceiverStoreBlocked("active publication is invalid")
        payload = self._validate_publication_row(publication)
        if payload is None:
            raise CMSReceiverStoreBlocked("active publication was deleted")
        self._assert_publication_binding(payload, expected)
        return payload

    def register_tombstone(self, expectation: Any) -> Mapping[str, Any]:
        """Pre-authorize one exact deletion of the currently active publication."""

        value = _tombstone_expectation(expectation)
        now = _timestamp(self.clock())
        locales_json = _canonical_json(list(value["locales"]))
        expectation_sha256 = _binding_sha256(value)
        with _transaction(self.connection):
            self._verify_schema()
            existing = self.connection.execute(
                "SELECT * FROM cms_receiver_tombstones WHERE tombstone_id = ?",
                (value["tombstone_id"],),
            ).fetchone()
            if existing is not None:
                if self._tombstone_from_row(existing) != value:
                    raise CMSReceiverStoreBlocked("tombstone identity collided")
                return {
                    "tombstone_id": value["tombstone_id"],
                    "publication_delivery_id": value["publication_delivery_id"],
                    "status": existing["status"],
                }
            publication = self.connection.execute(
                "SELECT * FROM cms_receiver_publications WHERE delivery_id = ?",
                (value["publication_delivery_id"],),
            ).fetchone()
            source = self.connection.execute(
                "SELECT active_delivery_id FROM cms_receiver_sources "
                "WHERE site_id = ? AND source_id = ?",
                (value["site_id"], value["source_id"]),
            ).fetchone()
            if (
                publication is None
                or publication["status"] != "active"
                or publication["payload_sha256"]
                != value["publication_payload_sha256"]
                or publication["site_id"] != value["site_id"]
                or publication["source_id"] != value["source_id"]
                or publication["source_sequence"] != value["source_sequence"]
                or source is None
                or source["active_delivery_id"] != value["publication_delivery_id"]
            ):
                raise CMSReceiverStoreBlocked("tombstone publication is not active")
            payload = self._validate_publication_row(publication)
            if (
                not isinstance(payload, dict)
                or payload.get("event_id") != value["event_id"]
                or payload.get("website_version") != value["website_version"]
                or payload.get("plan_id") != value["plan_id"]
                or tuple(item.get("locale") for item in payload.get("localizations", []))
                != value["locales"]
            ):
                raise CMSReceiverStoreBlocked("tombstone publication binding is invalid")
            try:
                self.connection.execute("""
                    INSERT INTO cms_receiver_tombstones (
                        tombstone_id, event_id, site_id, website_version, plan_id,
                        source_id, source_sequence, publication_delivery_id,
                        publication_payload_sha256, locales_json,
                        expectation_sha256, status, delivery_id, payload_sha256,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
                """, (
                    value["tombstone_id"], value["event_id"], value["site_id"],
                    value["website_version"], value["plan_id"], value["source_id"],
                    value["source_sequence"], value["publication_delivery_id"],
                    value["publication_payload_sha256"], locales_json,
                    expectation_sha256, now, now,
                ))
            except sqlite3.IntegrityError as error:
                raise CMSReceiverStoreBlocked("tombstone publication collided") from error
        return {
            "tombstone_id": value["tombstone_id"],
            "publication_delivery_id": value["publication_delivery_id"],
            "status": "pending",
        }

    def resolve_tombstone_expectation(self, tombstone: Any) -> Mapping[str, Any]:
        _, _, payload, _ = _verified(tombstone, kind="tombstone")
        tombstone_id = _token(payload.get("tombstone_id"), field="tombstone_id")
        self._verify_schema()
        row = self.connection.execute(
            "SELECT * FROM cms_receiver_tombstones WHERE tombstone_id = ?",
            (tombstone_id,),
        ).fetchone()
        if row is None:
            raise CMSReceiverStoreBlocked("tombstone expectation is missing")
        return self._tombstone_from_row(row)

    def delete(self, tombstone: Any) -> Mapping[str, Any]:
        """Atomically remove one exact active bundle and retain content-free evidence."""

        delivery_id, payload_sha256, payload, _ = _verified(
            tombstone, kind="tombstone",
        )
        now = _timestamp(self.clock())
        with _transaction(self.connection):
            self._verify_schema()
            row = self.connection.execute(
                "SELECT * FROM cms_receiver_tombstones WHERE tombstone_id = ?",
                (payload.get("tombstone_id"),),
            ).fetchone()
            if row is None:
                raise CMSReceiverStoreBlocked("tombstone expectation is missing")
            expected = self._tombstone_from_row(row)
            for field, value in expected.items():
                actual = tuple(payload.get(field, ())) if field == "locales" else payload.get(field)
                if actual != value:
                    raise CMSReceiverStoreBlocked("tombstone is no longer current")
            if row["status"] == "deleted":
                if row["delivery_id"] == delivery_id and row["payload_sha256"] == payload_sha256:
                    publication = self.connection.execute(
                        "SELECT * FROM cms_receiver_publications WHERE delivery_id = ?",
                        (expected["publication_delivery_id"],),
                    ).fetchone()
                    source = self.connection.execute(
                        "SELECT active_delivery_id FROM cms_receiver_sources "
                        "WHERE site_id = ? AND source_id = ?",
                        (expected["site_id"], expected["source_id"]),
                    ).fetchone()
                    if (
                        publication is None
                        or self._validate_publication_row(publication) is not None
                        or publication["status"] != "deleted"
                        or publication["payload_sha256"]
                        != expected["publication_payload_sha256"]
                        or publication["tombstone_delivery_id"] != delivery_id
                        or publication["tombstone_payload_sha256"] != payload_sha256
                        or source is None
                        or source["active_delivery_id"]
                        == expected["publication_delivery_id"]
                    ):
                        raise CMSReceiverStoreBlocked(
                            "deleted publication evidence is invalid"
                        )
                    return {
                        "delivery_id": delivery_id,
                        "payload_sha256": payload_sha256,
                        "status": "deleted",
                    }
                raise CMSReceiverStoreBlocked("tombstone delivery collided")
            publication = self.connection.execute(
                "SELECT * FROM cms_receiver_publications WHERE delivery_id = ?",
                (expected["publication_delivery_id"],),
            ).fetchone()
            source = self.connection.execute(
                "SELECT active_delivery_id FROM cms_receiver_sources "
                "WHERE site_id = ? AND source_id = ?",
                (expected["site_id"], expected["source_id"]),
            ).fetchone()
            if (
                publication is None
                or publication["status"] != "active"
                or publication["payload_sha256"]
                != expected["publication_payload_sha256"]
                or source is None
                or source["active_delivery_id"]
                != expected["publication_delivery_id"]
            ):
                raise CMSReceiverStoreBlocked("tombstone publication changed")
            if self._validate_publication_row(publication) is None:
                raise CMSReceiverStoreBlocked("tombstone publication was deleted")
            self.connection.execute(
                "DELETE FROM cms_receiver_localizations WHERE delivery_id = ?",
                (expected["publication_delivery_id"],),
            )
            changed = self.connection.execute("""
                UPDATE cms_receiver_publications
                SET payload_json = NULL, status = 'deleted',
                    tombstone_delivery_id = ?, tombstone_payload_sha256 = ?,
                    updated_at = ?
                WHERE delivery_id = ? AND status = 'active'
            """, (
                delivery_id, payload_sha256, now,
                expected["publication_delivery_id"],
            )).rowcount
            if changed != 1:
                raise CMSReceiverStoreBlocked("publication deletion was not atomic")
            changed = self.connection.execute("""
                UPDATE cms_receiver_sources
                SET active_delivery_id = NULL, updated_at = ?
                WHERE site_id = ? AND source_id = ? AND active_delivery_id = ?
            """, (
                now, expected["site_id"], expected["source_id"],
                expected["publication_delivery_id"],
            )).rowcount
            if changed != 1:
                raise CMSReceiverStoreBlocked("source deletion was not atomic")
            changed = self.connection.execute("""
                UPDATE cms_receiver_tombstones
                SET status = 'deleted', delivery_id = ?, payload_sha256 = ?,
                    updated_at = ?
                WHERE tombstone_id = ? AND status = 'pending'
            """, (
                delivery_id, payload_sha256, now, expected["tombstone_id"],
            )).rowcount
            if changed != 1:
                raise CMSReceiverStoreBlocked("tombstone deletion was not atomic")
        return {
            "delivery_id": delivery_id,
            "payload_sha256": payload_sha256,
            "status": "deleted",
        }

    def check(self, probe: Any) -> Mapping[str, Any]:
        """Return the exact content-free health receipt after durable checks."""

        probe_id = _token(getattr(probe, "probe_id", None), field="probe_id")
        contract_sha256 = _sha256(
            getattr(probe, "contract_sha256", None), field="contract_sha256",
        )
        self._verify_schema()
        if self.connection.in_transaction:
            raise CMSReceiverStoreBlocked("health cannot join an external transaction")
        quick = self.connection.execute("PRAGMA quick_check").fetchall()
        if [tuple(row) for row in quick] != [("ok",)]:
            raise CMSReceiverStoreBlocked("SQLite integrity check failed")
        foreign_keys = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise CMSReceiverStoreBlocked("SQLite foreign-key check failed")
        sources = self.connection.execute(
            "SELECT * FROM cms_receiver_sources ORDER BY site_id, source_id"
        ).fetchall()
        for source in sources:
            self._source_from_row(source)
        invalid_active = self.connection.execute("""
            SELECT 1
            FROM cms_receiver_sources AS source
            LEFT JOIN cms_receiver_publications AS publication
              ON publication.delivery_id = source.active_delivery_id
            WHERE source.active_delivery_id IS NOT NULL AND (
                   publication.delivery_id IS NULL
                OR publication.status != 'active'
                OR publication.site_id != source.site_id
                OR publication.source_id != source.source_id
            )
            LIMIT 1
        """).fetchone()
        orphan_active = self.connection.execute("""
            SELECT 1
            FROM cms_receiver_publications AS publication
            LEFT JOIN cms_receiver_sources AS source
              ON source.site_id = publication.site_id
             AND source.source_id = publication.source_id
             AND source.active_delivery_id = publication.delivery_id
            WHERE publication.status = 'active' AND source.site_id IS NULL
            LIMIT 1
        """).fetchone()
        if invalid_active is not None or orphan_active is not None:
            raise CMSReceiverStoreBlocked("durable receiver state is inconsistent")
        publications = self.connection.execute(
            "SELECT * FROM cms_receiver_publications ORDER BY delivery_id"
        ).fetchall()
        for publication in publications:
            self._validate_publication_row(publication)
        tombstones = self.connection.execute(
            "SELECT * FROM cms_receiver_tombstones ORDER BY tombstone_id"
        ).fetchall()
        for tombstone in tombstones:
            value = self._tombstone_from_row(tombstone)
            if tombstone["status"] == "pending":
                publication = self.connection.execute("""
                    SELECT publication.status, publication.payload_sha256,
                           source.active_delivery_id
                    FROM cms_receiver_publications AS publication
                    LEFT JOIN cms_receiver_sources AS source
                      ON source.site_id = publication.site_id
                     AND source.source_id = publication.source_id
                    WHERE publication.delivery_id = ?
                """, (value["publication_delivery_id"],)).fetchone()
                if (
                    tombstone["delivery_id"] is not None
                    or tombstone["payload_sha256"] is not None
                    or publication is None
                    or publication["status"] != "active"
                    or publication["payload_sha256"]
                    != value["publication_payload_sha256"]
                    or publication["active_delivery_id"]
                    != value["publication_delivery_id"]
                ):
                    raise CMSReceiverStoreBlocked("pending tombstone is invalid")
            elif tombstone["status"] == "deleted":
                delivery_id = _token(tombstone["delivery_id"], field="delivery_id")
                payload_sha256 = _sha256(
                    tombstone["payload_sha256"], field="payload_sha256",
                )
                publication = self.connection.execute(
                    "SELECT status, payload_sha256, tombstone_delivery_id, "
                    "tombstone_payload_sha256 "
                    "FROM cms_receiver_publications WHERE delivery_id = ?",
                    (value["publication_delivery_id"],),
                ).fetchone()
                if (
                    publication is None
                    or publication["status"] != "deleted"
                    or publication["payload_sha256"]
                    != value["publication_payload_sha256"]
                    or publication["tombstone_delivery_id"] != delivery_id
                    or publication["tombstone_payload_sha256"] != payload_sha256
                ):
                    raise CMSReceiverStoreBlocked("deleted tombstone is invalid")
            else:
                raise CMSReceiverStoreBlocked("tombstone state is invalid")
        return {
            "probe_id": probe_id,
            "contract_sha256": contract_sha256,
            "status": "healthy",
        }
