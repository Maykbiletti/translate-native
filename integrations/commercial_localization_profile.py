"""Compatibility import for the portable, skill-bundled commercial validator.

The production website runtime and skill-only CLI deliberately execute the same
implementation. This module is not a service and does not install or start one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = (
    Path(__file__).resolve().parents[1]
    / "translate-native"
    / "scripts"
    / "commercial_localization_profile.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "translate_native_portable_commercial",
    _PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("commercial profile implementation unavailable")
_IMPL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_IMPL)

hashlib = _IMPL.hashlib
PUBLIC_PROFILE_SCHEMA = _IMPL.PUBLIC_PROFILE_SCHEMA
REVIEW_SUMMARY_CAPABILITIES_SCHEMA = _IMPL.REVIEW_SUMMARY_CAPABILITIES_SCHEMA
REVIEW_SUMMARY_SCHEMA = _IMPL.REVIEW_SUMMARY_SCHEMA
EVIDENCE_BINDING_SCHEMA = _IMPL.EVIDENCE_BINDING_SCHEMA
COMMERCIAL_LOCALE_PROFILE_SCHEMA = _IMPL.COMMERCIAL_LOCALE_PROFILE_SCHEMA
DIMENSIONS = _IMPL.DIMENSIONS
_canonical_json = _IMPL._canonical_json
evidence_sha256 = _IMPL.evidence_sha256
public_review_summary_contract = _IMPL.public_review_summary_contract
public_profile = _IMPL.public_profile
CREATION_GUIDANCE = _IMPL.CREATION_GUIDANCE
NATIVE_GUIDANCE = _IMPL.NATIVE_GUIDANCE
FIDELITY_GUIDANCE = _IMPL.FIDELITY_GUIDANCE
CommercialReviewBlocked = _IMPL.CommercialReviewBlocked
review_contract = _IMPL.review_contract
validate_review = _IMPL.validate_review
validate_summary = _IMPL.validate_summary
