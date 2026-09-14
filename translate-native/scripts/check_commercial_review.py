#!/usr/bin/env python3
"""Offline evidence-shape check, never a semantic approval or release token."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from commercial_localization_profile import (
    CommercialReviewBlocked,
    public_review_evidence_contract,
    public_review_resolution_contract,
    public_review_summary_contract,
    review_contract,
    validate_review,
)


PROFILE = "translate-native.commercial.v9"
MAX_INPUT_BYTES = 2_000_000


def _read(path: str) -> str:
    # Bound each read; decode bytes directly to preserve CRLF and exact spans.
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input limit")
    text = raw.decode("utf-8")
    if not text.strip() or text.startswith("\ufeff"):
        raise ValueError("empty input or BOM")
    return text


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON number")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", action="store_true", help="print the review contract; not completed evidence")
    parser.add_argument("--source", help="complete source text, UTF-8 without BOM")
    parser.add_argument("--target", help="complete target text, UTF-8 without BOM")
    parser.add_argument("--review", help="source-aware review JSON, UTF-8 without BOM")
    parser.add_argument("--target-locale", help="exact BCP-47 target locale")
    parser.add_argument(
        "--commercial-quality-profile-version",
        help="exact target-locale commercial quality-profile version",
    )
    parser.add_argument(
        "--commercial-quality-profile-sha256",
        help="exact target-locale commercial quality-profile SHA-256",
    )
    args = parser.parse_args(argv)
    if args.contract:
        if any((
            args.source, args.target, args.review, args.target_locale,
            args.commercial_quality_profile_version,
            args.commercial_quality_profile_sha256,
        )):
            parser.error("--contract cannot be combined with input files")
        print(json.dumps({
            "status": "CONTRACT",
            "release_allowed": False,
            "commercial_review": review_contract(PROFILE),
            "commercial_review_evidence_contract": (
                public_review_evidence_contract(PROFILE)
            ),
            "commercial_review_summary_contract": (
                public_review_summary_contract(PROFILE)
            ),
            "commercial_review_resolution_contract": (
                public_review_resolution_contract(PROFILE)
            ),
        }, ensure_ascii=False, sort_keys=True))
        return 0
    if not all((
        args.source, args.target, args.review, args.target_locale,
        args.commercial_quality_profile_version,
        args.commercial_quality_profile_sha256,
    )):
        parser.error(
            "--source, --target, --review, --target-locale, "
            "--commercial-quality-profile-version and "
            "--commercial-quality-profile-sha256 are required"
        )
    result = {"schema": "translate-native.commercial-evidence-check.v2",
              "profile": PROFILE, "release_allowed": False}
    try:
        source, target, raw_review = (_read(path) for path in (args.source, args.target, args.review))
        review = json.loads(raw_review, object_pairs_hook=_object, parse_constant=_constant)
        summary = validate_review(
            review,
            source,
            target,
            PROFILE,
            target_locale=args.target_locale,
            commercial_quality_profile_version=(
                args.commercial_quality_profile_version
            ),
            commercial_quality_profile_sha256=(
                args.commercial_quality_profile_sha256
            ),
        )
    except CommercialReviewBlocked as error:
        result.update(status="BLOCK", reason=error.code)
    except (OSError, ValueError, RecursionError):
        result.update(status="BLOCK", reason="review.commercial.input_invalid")
    else:
        result.update(
            status="EVIDENCE_VALID", reason="independent_quality_and_signed_release_required",
            target_locale=args.target_locale,
            commercial_quality_profile_version=(
                args.commercial_quality_profile_version
            ),
            commercial_quality_profile_sha256=(
                args.commercial_quality_profile_sha256
            ),
            evidence_sha256=summary["evidence_sha256"],
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            target_sha256=hashlib.sha256(target.encode("utf-8")).hexdigest(),
            review_sha256=hashlib.sha256(raw_review.encode("utf-8")).hexdigest(),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "EVIDENCE_VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
