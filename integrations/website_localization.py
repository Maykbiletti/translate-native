#!/usr/bin/env python3
"""Deterministic, provider-neutral planning for EU website localization jobs.

The planner does not call a model or publish content. It turns one trusted
website source object into one independently retryable job per target locale.
Every job key is bound to the source and all policy inputs that can affect the
result, so a queue can safely deduplicate retries without reusing stale copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "blun.website-localization-plan.v1"
JOB_SCHEMA = "blun.website-localization-job.v1"
EU_LANGUAGE_SOURCE = (
    "https://european-union.europa.eu/principles-countries-history/languages_en"
)
MAX_SOURCE_BYTES = 1_000_000
MAX_FIELD_LENGTH = 256
EXACT_LOCALE = re.compile(
    r"^[A-Za-z]{2,8}(?:-(?:[A-Za-z]{4}|[A-Za-z]{2}|[0-9]{3}|[A-Za-z0-9]{5,8}))*$"
)
CONTENT_TYPES = frozenset({
    "headline",
    "cta",
    "marketing",
    "ui",
    "documentation",
    "seo",
    "legal",
})
QUALITY_PASSES = ("target_native", "source_fidelity")


class LocalizationPlanBlocked(ValueError):
    """Raised when trusted localization metadata is incomplete or ambiguous."""


@dataclass(frozen=True)
class LocaleProfile:
    locale: str
    eu_code: str
    language: str
    native_name: str
    script: str
    direction: str = "ltr"


# The European Union currently has 24 official languages. A single explicit
# website locale is selected for each language so every queue item has regional
# conventions rather than an ambiguous bare language tag. German defaults to
# Austrian usage for BLUN; callers may add future profiles in a schema revision.
EU_OFFICIAL_LOCALES: tuple[LocaleProfile, ...] = (
    LocaleProfile("bg-BG", "BG", "bg", "български", "Cyrl"),
    LocaleProfile("hr-HR", "HR", "hr", "hrvatski", "Latn"),
    LocaleProfile("cs-CZ", "CS", "cs", "čeština", "Latn"),
    LocaleProfile("da-DK", "DA", "da", "dansk", "Latn"),
    LocaleProfile("nl-NL", "NL", "nl", "Nederlands", "Latn"),
    LocaleProfile("en-IE", "EN", "en", "English", "Latn"),
    LocaleProfile("et-EE", "ET", "et", "eesti", "Latn"),
    LocaleProfile("fi-FI", "FI", "fi", "suomi", "Latn"),
    LocaleProfile("fr-FR", "FR", "fr", "français", "Latn"),
    LocaleProfile("de-AT", "DE", "de", "Deutsch", "Latn"),
    LocaleProfile("el-GR", "EL", "el", "ελληνικά", "Grek"),
    LocaleProfile("hu-HU", "HU", "hu", "magyar", "Latn"),
    LocaleProfile("ga-IE", "GA", "ga", "Gaeilge", "Latn"),
    LocaleProfile("it-IT", "IT", "it", "italiano", "Latn"),
    LocaleProfile("lv-LV", "LV", "lv", "latviešu", "Latn"),
    LocaleProfile("lt-LT", "LT", "lt", "lietuvių", "Latn"),
    LocaleProfile("mt-MT", "MT", "mt", "Malti", "Latn"),
    LocaleProfile("pl-PL", "PL", "pl", "polski", "Latn"),
    LocaleProfile("pt-PT", "PT", "pt", "português", "Latn"),
    LocaleProfile("ro-RO", "RO", "ro", "română", "Latn"),
    LocaleProfile("sk-SK", "SK", "sk", "slovenčina", "Latn"),
    LocaleProfile("sl-SI", "SL", "sl", "slovenščina", "Latn"),
    LocaleProfile("es-ES", "ES", "es", "español", "Latn"),
    LocaleProfile("sv-SE", "SV", "sv", "svenska", "Latn"),
)
_PROFILE_BY_LOCALE = {profile.locale: profile for profile in EU_OFFICIAL_LOCALES}


@dataclass(frozen=True)
class LocalizationJob:
    job_id: str
    source_id: str
    source_revision: str
    source_text: str
    source_hash: str
    source_locale: str
    target: LocaleProfile
    content_type: str
    glossary_version: str
    policy_version: str
    provider_id: str
    model_id: str
    model_version: str
    software_version: str

    def as_payload(self) -> dict[str, Any]:
        """Return the provider-neutral queue payload for one target locale."""
        return {
            "schema": JOB_SCHEMA,
            "job_id": self.job_id,
            "idempotency_key": self.job_id,
            "source": {
                "id": self.source_id,
                "revision": self.source_revision,
                "text": self.source_text,
                "sha256": self.source_hash,
                "locale": self.source_locale,
            },
            "target": asdict(self.target),
            "content_type": self.content_type,
            "glossary_version": self.glossary_version,
            "policy_version": self.policy_version,
            "provider": {
                "id": self.provider_id,
                "model_id": self.model_id,
                "model_version": self.model_version,
            },
            "software_version": self.software_version,
            "quality_passes": list(QUALITY_PASSES),
            "release_required": True,
        }


@dataclass(frozen=True)
class LocalizationPlan:
    plan_id: str
    source_hash: str
    source_locale: str
    jobs: tuple[LocalizationJob, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "plan_id": self.plan_id,
            "source_hash": self.source_hash,
            "source_locale": self.source_locale,
            "job_count": len(self.jobs),
            "jobs": [job.as_payload() for job in self.jobs],
        }


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _bound_field(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise LocalizationPlanBlocked(f"{name} must be a string")
    if not value or value != value.strip():
        raise LocalizationPlanBlocked(f"{name} must be non-empty without surrounding whitespace")
    if "\x00" in value or len(value) > MAX_FIELD_LENGTH:
        raise LocalizationPlanBlocked(f"{name} is invalid or too long")
    if not unicodedata.is_normalized("NFC", value):
        raise LocalizationPlanBlocked(f"{name} must use NFC Unicode")
    return value


def _source_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalizationPlanBlocked("source_text must be a non-empty string")
    if "\x00" in value:
        raise LocalizationPlanBlocked("source_text contains NUL")
    if not unicodedata.is_normalized("NFC", value):
        raise LocalizationPlanBlocked("source_text must use NFC Unicode")
    if len(value.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise LocalizationPlanBlocked("source_text exceeds the byte limit")
    return value


def canonicalize_locale(value: Any) -> str:
    value = _bound_field("locale", value)
    if value.casefold() in {"auto", "all"}:
        raise LocalizationPlanBlocked("locale must name one exact language or locale")
    if not EXACT_LOCALE.fullmatch(value):
        raise LocalizationPlanBlocked("locale must be an exact BCP-47 language tag")
    parts = value.split("-")
    canonical = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            canonical.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            canonical.append(part.upper())
        else:
            canonical.append(part.lower())
    return "-".join(canonical)


def _target_profiles(
    source_locale: str,
    target_locales: Iterable[Any] | None,
) -> tuple[LocaleProfile, ...]:
    source_language = source_locale.split("-", 1)[0]
    if target_locales is None:
        return tuple(
            profile for profile in EU_OFFICIAL_LOCALES
            if profile.language != source_language
        )
    if isinstance(target_locales, (str, bytes)):
        raise LocalizationPlanBlocked("target_locales must be an array")
    requested = list(target_locales)
    if not requested:
        raise LocalizationPlanBlocked("target_locales must not be empty")
    canonical = [canonicalize_locale(item) for item in requested]
    if len(set(canonical)) != len(canonical):
        raise LocalizationPlanBlocked("target_locales contains duplicates")
    unknown = [locale for locale in canonical if locale not in _PROFILE_BY_LOCALE]
    if unknown:
        raise LocalizationPlanBlocked(
            "target_locales contains unsupported EU locale: " + ", ".join(unknown)
        )
    if any(_PROFILE_BY_LOCALE[locale].language == source_language for locale in canonical):
        raise LocalizationPlanBlocked("target_locales must exclude the source language")
    selected = set(canonical)
    return tuple(profile for profile in EU_OFFICIAL_LOCALES if profile.locale in selected)


def plan_website_localization(
    *,
    source_id: Any,
    source_revision: Any,
    source_text: Any,
    source_locale: Any,
    content_type: Any,
    glossary_version: Any,
    policy_version: Any,
    provider_id: Any,
    model_id: Any,
    model_version: Any,
    software_version: Any,
    target_locales: Iterable[Any] | None = None,
) -> LocalizationPlan:
    """Create one deterministic, independently releasable job per EU locale."""
    source_id = _bound_field("source_id", source_id)
    source_revision = _bound_field("source_revision", source_revision)
    source_text = _source_text(source_text)
    source_locale = canonicalize_locale(source_locale)
    content_type = _bound_field("content_type", content_type)
    if content_type not in CONTENT_TYPES:
        raise LocalizationPlanBlocked("content_type is not a supported website content type")
    glossary_version = _bound_field("glossary_version", glossary_version)
    policy_version = _bound_field("policy_version", policy_version)
    provider_id = _bound_field("provider_id", provider_id)
    model_id = _bound_field("model_id", model_id)
    model_version = _bound_field("model_version", model_version)
    software_version = _bound_field("software_version", software_version)
    profiles = _target_profiles(source_locale, target_locales)
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()

    common = {
        "schema": JOB_SCHEMA,
        "source_id": source_id,
        "source_revision": source_revision,
        "source_hash": source_hash,
        "source_locale": source_locale,
        "content_type": content_type,
        "glossary_version": glossary_version,
        "policy_version": policy_version,
        "provider_id": provider_id,
        "model_id": model_id,
        "model_version": model_version,
        "software_version": software_version,
        "quality_passes": QUALITY_PASSES,
    }
    jobs = tuple(
        LocalizationJob(
            job_id="blun-l10n-" + _digest({**common, "target_locale": profile.locale}),
            source_id=source_id,
            source_revision=source_revision,
            source_text=source_text,
            source_hash=source_hash,
            source_locale=source_locale,
            target=profile,
            content_type=content_type,
            glossary_version=glossary_version,
            policy_version=policy_version,
            provider_id=provider_id,
            model_id=model_id,
            model_version=model_version,
            software_version=software_version,
        )
        for profile in profiles
    )
    plan_binding = {
        **common,
        "target_locales": [job.target.locale for job in jobs],
        "job_ids": [job.job_id for job in jobs],
    }
    return LocalizationPlan(
        plan_id="blun-l10n-plan-" + _digest(plan_binding),
        source_hash=source_hash,
        source_locale=source_locale,
        jobs=jobs,
    )


_PLAN_FIELDS = frozenset({
    "source_id",
    "source_revision",
    "source_text",
    "source_locale",
    "content_type",
    "glossary_version",
    "policy_version",
    "provider_id",
    "model_id",
    "model_version",
    "software_version",
    "target_locales",
})


def plan_from_mapping(value: Any) -> LocalizationPlan:
    if not isinstance(value, dict):
        raise LocalizationPlanBlocked("input must be a JSON object")
    unknown = sorted(set(value) - _PLAN_FIELDS)
    if unknown:
        raise LocalizationPlanBlocked("input contains unknown fields: " + ", ".join(unknown))
    missing = sorted((_PLAN_FIELDS - {"target_locales"}) - set(value))
    if missing:
        raise LocalizationPlanBlocked("input is missing fields: " + ", ".join(missing))
    return plan_website_localization(**value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create deterministic EU website-localization queue jobs",
    )
    parser.add_argument("--input", type=Path, help="JSON request; defaults to stdin")
    args = parser.parse_args(argv)
    try:
        raw = args.input.read_text(encoding="utf-8-sig") if args.input else sys.stdin.read().lstrip("\ufeff")
        plan = plan_from_mapping(json.loads(raw))
    except (OSError, json.JSONDecodeError, LocalizationPlanBlocked) as error:
        print(json.dumps({"status": "BLOCK", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "PASS", **plan.as_payload()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
