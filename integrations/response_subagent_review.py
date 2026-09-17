"""Trusted-host delegation for source-blind review of ordinary responses.

The caller supplies no source text.  Creator identity, isolation controls and
reviewer execution facts are owned by the host integration, never by the model
whose candidate is being reviewed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Protocol


SCHEMA = "translate-native.response-subagent-review.v1"
RESPONSE_SCHEMA = "translate-native.response-native-review.v1"
EVIDENCE_SCHEMA = "translate-native.response-review-evidence.v1"
MAX_BYTES = 4_000_000
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
LOCALE = re.compile(r"^(?:[A-Za-z]{2,8}|x)(?:-[A-Za-z0-9]{1,8})*$")
CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,117}$")


class ResponseReviewBlocked(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = "response_review." + code
        self.retryable = retryable
        super().__init__(self.code)


class ReviewHost(Protocol):
    def run_isolated(self, task: dict, *, control: dict) -> Mapping[str, Any]: ...
    def verify_execution(self, receipt: dict, *, control: dict) -> bool: ...


def _raw(value: Any) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ResponseReviewBlocked("invalid_payload") from None
    if not encoded or len(encoded) > MAX_BYTES:
        raise ResponseReviewBlocked("invalid_payload")
    return encoded


def _copy(value: Any) -> Any:
    return json.loads(_raw(value))


def _hash(value: Any) -> str:
    return hashlib.sha256(_raw(value)).hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ResponseReviewBlocked("invalid_identity")
    return value


def _sha256_identifier(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ResponseReviewBlocked("invalid_identity")
    return value


class ResponseSubagentReviewer:
    """Run exactly one target-only native-language review through a trusted host."""

    def __init__(self, host: ReviewHost, *, model_id: str, model_version: str,
                 host_policy_version: str, quality_profile_version: str,
                 prompt_version: str, software_version: str,
                 native_brief: Mapping[str, Any], timeout_seconds: int = 60,
                 max_output_tokens: int = 4096):
        if any(not callable(getattr(host, method, None))
               for method in ("run_isolated", "verify_execution")):
            raise ResponseReviewBlocked("host_unavailable")
        if (type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300
                or type(max_output_tokens) is not int
                or not 128 <= max_output_tokens <= 32768):
            raise ResponseReviewBlocked("invalid_budget")
        brief = _copy(native_brief)
        if (not isinstance(brief, dict)
                or set(brief) != {"audience", "tone_profile", "target_terms"}
                or any(not isinstance(brief[key], str) or not brief[key].strip()
                       or len(brief[key]) > 2000
                       for key in ("audience", "tone_profile"))
                or not isinstance(brief["target_terms"], list)
                or len(brief["target_terms"]) > 100
                or any(not isinstance(term, str) or not term.strip()
                       or len(term) > 256 for term in brief["target_terms"])):
            raise ResponseReviewBlocked("native_brief_required")
        self._host = host
        self._policy = {
            "schema": SCHEMA,
            "model_id": _identifier(model_id),
            "model_version": _identifier(model_version),
            "host_policy_version": _identifier(host_policy_version),
            "native_brief": brief,
            "timeout_seconds": timeout_seconds,
            "max_output_tokens": max_output_tokens,
            "inherit_context": False,
            "tools": [],
            "max_delegation_depth": 0,
        }
        self._profile = {
            "quality_profile_version": _identifier(quality_profile_version),
            "prompt_version": _identifier(prompt_version),
            "software_version": _identifier(software_version),
        }

    def review(self, target_text: str, target_locale: str,
               content_type: str, *, creator_id_sha256: str,
               creator_session_id_sha256: str) -> dict[str, Any]:
        if (not isinstance(target_text, str) or not target_text
                or not isinstance(target_locale, str)
                or LOCALE.fullmatch(target_locale) is None
                or not isinstance(content_type, str) or not content_type.strip()):
            raise ResponseReviewBlocked("request_invalid")
        target = {"locale": target_locale}
        task = {
            "schema": SCHEMA,
            "phase": "target_native",
            "system_instruction": (
                "Review only the supplied target-language candidate. Do not infer or request "
                "a source, prior messages, creator context, tools, publication, or signatures. "
                "Assess nativeness, idiom, register, rhythm, translationese, native script, "
                "diacritics and punctuation. Return only the required structured review."
            ),
            "input": {
                "candidate": target_text,
                "target": target,
                "content_type": content_type,
                "quality_profile": _copy(self._profile),
                "response_schema": RESPONSE_SCHEMA,
                **_copy(self._policy["native_brief"]),
            },
        }
        request = {
            "schema": SCHEMA,
            "phase": "target_native",
            "target_sha256": hashlib.sha256(target_text.encode("utf-8")).hexdigest(),
            "target_locale": target_locale,
            "content_type": content_type,
            "profile": _copy(self._profile),
        }
        policy = {
            **_copy(self._policy),
            "creator_id_sha256": _sha256_identifier(creator_id_sha256),
            "creator_session_id_sha256": _sha256_identifier(
                creator_session_id_sha256
            ),
        }
        control = {
            **policy,
            "reviewer_role": "target-native-reviewer",
            "assignment_id": _hash({
                "request": request,
                "role": "target-native-reviewer",
            }),
            "provider_id": "host-response-review-v1-" + _hash({
                "policy": policy, "profile": self._profile,
            }),
            "execution_key": _hash({"request": request, "policy": policy}),
            "request_sha256": _hash(request),
            "task_sha256": _hash(task),
            "phase": "target_native",
            "previous_receipt_sha256": None,
        }
        try:
            reply = _copy(self._host.run_isolated(_copy(task), control=_copy(control)))
        except TimeoutError:
            raise ResponseReviewBlocked("timeout", retryable=True) from None
        except ResponseReviewBlocked:
            raise
        except Exception as error:
            if (getattr(error, "host_subagent_failure", False) is True
                    and isinstance(getattr(error, "code", None), str)
                    and CODE.fullmatch(error.code) is not None
                    and type(getattr(error, "retryable", None)) is bool):
                raise ResponseReviewBlocked(error.code, retryable=error.retryable) from None
            raise ResponseReviewBlocked("host_failed", retryable=True) from None
        if not isinstance(reply, dict) or set(reply) != {"response", "receipt"}:
            raise ResponseReviewBlocked("receipt_invalid")
        response, receipt = reply["response"], reply["receipt"]
        self._validate_response(response, target_locale)
        host_evidence = self._validate_receipt(receipt, response, control)
        blockers = [finding for finding in response["findings"]
                    if finding["severity"] in {"major", "blocking"}]
        uncertain_findings = [
            finding for finding in response["findings"]
            if finding["uncertainty"].strip()
        ]
        if (response["status"] != "PASS" or response["confidence"] != "high"
                or blockers or uncertain_findings or response["uncertainties"]):
            raise ResponseReviewBlocked("independent_review_required")
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "phase": "target_native",
            "target_sha256": request["target_sha256"],
            "target_locale": target_locale,
            "content_type": content_type,
            "profile": _copy(self._profile),
            "response": _copy(response),
            "host_evidence": host_evidence,
        }
        return {"evidence": evidence, "evidence_sha256": _hash(evidence)}

    @staticmethod
    def _validate_response(response: Any, locale: str) -> None:
        if (not isinstance(response, dict)
                or set(response) != {"schema", "phase", "locale", "status",
                                     "confidence", "findings", "uncertainties"}
                or response.get("schema") != RESPONSE_SCHEMA
                or response.get("phase") != "target_native"
                or response.get("locale") != locale
                or response.get("status") not in {"PASS", "FAIL"}
                or response.get("confidence") not in {"high", "low"}
                or not isinstance(response.get("findings"), list)
                or not isinstance(response.get("uncertainties"), list)
                or any(not isinstance(item, str) or not item.strip()
                       for item in response["uncertainties"])):
            raise ResponseReviewBlocked("review_invalid")
        for finding in response["findings"]:
            if (not isinstance(finding, dict)
                    or set(finding) != {"code", "severity", "reason", "uncertainty"}
                    or not isinstance(finding["code"], str)
                    or CODE.fullmatch(finding["code"]) is None
                    or finding["severity"] not in {"minor", "major", "blocking"}
                    or not isinstance(finding["reason"], str) or not finding["reason"].strip()
                    or not isinstance(finding["uncertainty"], str)):
                raise ResponseReviewBlocked("review_invalid")

    def _validate_receipt(self, receipt: Any, response: Any, control: dict) -> dict:
        expected = {"schema", "execution_key", "request_sha256", "task_sha256",
                    "phase", "previous_receipt_sha256", "response_sha256",
                    "agent_id", "session_id", "model_id", "model_version",
                    "inherit_context", "tools", "max_delegation_depth",
                    "reviewer_role", "assignment_id", "usage"}
        if not isinstance(receipt, dict) or set(receipt) != expected:
            raise ResponseReviewBlocked("receipt_invalid")
        bound = {key: control[key] for key in expected - {
            "response_sha256", "agent_id", "session_id", "usage"}}
        if (_raw({key: receipt.get(key) for key in bound}) != _raw(bound)
                or receipt.get("response_sha256") != _hash(response)
                or not isinstance(receipt.get("usage"), dict)):
            raise ResponseReviewBlocked("receipt_binding")
        agent = _identifier(receipt.get("agent_id"))
        session = _identifier(receipt.get("session_id"))
        if (hashlib.sha256(agent.encode("utf-8")).hexdigest()
                == control["creator_id_sha256"]
                or hashlib.sha256(session.encode("utf-8")).hexdigest()
                == control["creator_session_id_sha256"]):
            raise ResponseReviewBlocked("self_review")
        try:
            valid = self._host.verify_execution(_copy(receipt), control=_copy(control))
        except Exception as error:
            self._raise_verification_error(error)
        if valid is not True:
            raise ResponseReviewBlocked("unverified_execution")
        evidence = _copy(receipt)
        evidence_method = getattr(self._host, "verified_execution_evidence", None)
        if callable(evidence_method):
            try:
                evidence = _copy(evidence_method(_copy(receipt), control=_copy(control)))
            except Exception as error:
                self._raise_verification_error(error)
            if not isinstance(evidence, dict):
                raise ResponseReviewBlocked("unverified_execution")
        return evidence

    @staticmethod
    def _raise_verification_error(error: Exception) -> None:
        if (getattr(error, "host_subagent_failure", False) is True
                and isinstance(getattr(error, "code", None), str)
                and CODE.fullmatch(error.code) is not None
                and type(getattr(error, "retryable", None)) is bool):
            raise ResponseReviewBlocked(error.code, retryable=error.retryable) from None
        raise ResponseReviewBlocked("verification_unavailable", retryable=True) from None
