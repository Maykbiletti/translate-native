"""Compatibility import for the portable, skill-bundled commercial validator.

The website worker and skill-only CLI deliberately execute the same code.
This module is not a service and does not install or start one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[1] / "translate-native" / "scripts" / "commercial_localization_profile.py"
_SPEC = importlib.util.spec_from_file_location("translate_native_portable_commercial", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("commercial profile implementation unavailable")
_IMPL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_IMPL)

DIMENSIONS = _IMPL.DIMENSIONS
CREATION_GUIDANCE = _IMPL.CREATION_GUIDANCE
NATIVE_GUIDANCE = _IMPL.NATIVE_GUIDANCE
FIDELITY_GUIDANCE = _IMPL.FIDELITY_GUIDANCE
CommercialReviewBlocked = _IMPL.CommercialReviewBlocked
review_contract = _IMPL.review_contract
validate_review = _IMPL.validate_review
