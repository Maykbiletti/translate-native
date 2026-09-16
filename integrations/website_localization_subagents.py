"""Host-owned, isolated review delegation for the existing localization worker.

The host is a trusted dependency, not a model tool. It must enforce isolation,
deadlines and token ceilings and verify receipts against its durable execution
ledger. Model-generated claims about identity or isolation are never evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Protocol


SCHEMA = "translate-native.host-subagent-review.v1"
PROVIDER_PREFIX = "host-subagents-v1-"
MAX_BYTES = 4_000_000
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class SubagentReviewBlocked(RuntimeError):
    localization_provider_failure = True

    def __init__(self, code: str, *, retryable: bool = False):
        self.code = "subagents." + code
        self.retryable = retryable
        super().__init__(self.code)


class ReviewHost(Protocol):
    def run_isolated(self, task: dict, *, control: dict) -> Mapping[str, Any]: ...

    def verify_execution(self, receipt: dict, *, control: dict) -> bool: ...


def _raw(value: Any) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError
        return raw
    except (TypeError, ValueError, UnicodeError):
        raise SubagentReviewBlocked("invalid_payload") from None


def _copy(value: Any) -> Any:
    return json.loads(_raw(value))


def _hash(value: Any) -> str:
    return hashlib.sha256(_raw(value)).hexdigest()


def _id(value: Any) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise SubagentReviewBlocked("invalid_identity")
    return value


class HostSubagentProvider:
    """One job attempt: creator, isolated native editor, isolated fidelity editor.

    Register ``provider_id`` in the plan, never the creator's old provider ID.
    The policy-derived identity invalidates old jobs/cache on configuration
    changes. Recreate this adapter for each queue attempt; retry ownership stays
    with the durable queue. The host deduplicates by control.execution_key.
    """

    def __init__(self, creator: Any, host: ReviewHost, *, creator_id: str,
                 creator_session_id: str, model_id: str, model_version: str,
                 host_policy_version: str, native_brief: Mapping[str, Any],
                 timeout_seconds: int = 60,
                 max_output_tokens: int = 4096):
        if not callable(getattr(creator, "invoke", None)):
            raise SubagentReviewBlocked("creator_unavailable")
        if any(not callable(getattr(host, method, None))
               for method in ("run_isolated", "verify_execution")):
            raise SubagentReviewBlocked("host_unavailable")
        if (type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300
                or type(max_output_tokens) is not int
                or not 128 <= max_output_tokens <= 32768):
            raise SubagentReviewBlocked("invalid_budget")
        self._creator, self._host = creator, host
        brief = _copy(native_brief)
        if (not isinstance(brief, dict)
                or set(brief) != {"audience", "tone_profile", "target_terms"}
                or any(not isinstance(brief[key], str) or not brief[key].strip()
                       or len(brief[key]) > 2000 for key in ("audience", "tone_profile"))
                or not isinstance(brief["target_terms"], list)
                or len(brief["target_terms"]) > 100
                or any(not isinstance(term, str) or not term.strip() or len(term) > 256
                       for term in brief["target_terms"])):
            raise SubagentReviewBlocked("native_brief_required")
        self._policy = {
            "schema": SCHEMA,
            "creator_id": _id(creator_id),
            "creator_session_id": _id(creator_session_id),
            "model_id": _id(model_id), "model_version": _id(model_version),
            "host_policy_version": _id(host_policy_version),
            "native_brief": brief,
            "timeout_seconds": timeout_seconds,
            "max_output_tokens": max_output_tokens,
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
        }
        self.provider_id = PROVIDER_PREFIX + _hash(self._policy)
        self._creation = None
        self._candidate = None
        self._native_receipt = None
        self._finished = False
        self._evidence: dict[str, tuple[str, dict]] = {}

    def _request(self, request: Any) -> dict:
        try:
            payload = _copy(request.as_payload())
            if (set(payload) != {"schema", "request_id", "phase", "provider_id",
                                 "model_id", "model_version", "system_instruction", "input"}
                    or payload["schema"] != "blun.website-localization-worker.v9"
                    or payload["provider_id"] != self.provider_id
                    or payload["model_id"] != self._policy["model_id"]
                    or payload["model_version"] != self._policy["model_version"]
                    or payload["phase"] not in {"transcreation", "target_native", "source_fidelity"}
                    or not isinstance(payload["input"], dict)):
                raise ValueError
            expected = "blun-l10n-call-" + _hash({
                "worker_schema": payload["schema"],
                "job_id": payload["input"]["job_id"],
                "phase": payload["phase"],
                "input_sha256": _hash(payload["input"]),
            })
            if payload["request_id"] != expected:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise SubagentReviewBlocked("request_invalid") from None
        return payload

    def invoke(self, request: Any) -> Mapping[str, Any]:
        payload = self._request(request)
        phase, data = payload["phase"], payload["input"]
        if phase == "transcreation":
            if self._creation is not None:
                raise SubagentReviewBlocked("phase_order")
            # Reserve before external work. A failure requires a new queue attempt.
            self._creation = payload
            response = _copy(self._creator.invoke(request))
            if (not isinstance(response, dict)
                    or set(response) != {"schema", "phase", "locale", "candidate"}
                    or response["schema"] != "blun.website-localization-candidate.v1"
                    or response["phase"] != phase
                    or response["locale"] != data["target"]["locale"]
                    or not isinstance(response["candidate"], str)
                    or not response["candidate"]):
                raise SubagentReviewBlocked("candidate_invalid")
            self._candidate = response["candidate"]
            return response
        if self._creation is None or self._candidate is None or self._finished:
            raise SubagentReviewBlocked("phase_order")
        creation = self._creation["input"]
        if (data.get("candidate") != self._candidate
                or any(data.get(key) != creation.get(key) for key in (
                    "job_id", "target", "content_type", "glossary_version",
                    "policy_version", "quality_profile", "commercial_quality_profile"))):
            raise SubagentReviewBlocked("candidate_binding")
        if phase == "target_native":
            if self._native_receipt is not None:
                raise SubagentReviewBlocked("phase_order")
            # Allowlist only target material; no job/source IDs, glossary notes,
            # project metadata, audience/tone free text, inherited messages or tools.
            task_input = {key: _copy(data[key]) for key in (
                "candidate", "target", "content_type", "quality_profile", "response_schema")}
            # Independently authored, source-free host brief, not creator assets.
            task_input.update(_copy(self._policy["native_brief"]))
            if "commercial_quality_profile" in data:
                task_input["commercial_quality_profile"] = _copy(data["commercial_quality_profile"])
        else:
            if self._native_receipt is None:
                raise SubagentReviewBlocked("phase_order")
            if (data.get("source") != creation.get("source")
                    or data.get("glossary") != creation.get("glossary")):
                raise SubagentReviewBlocked("source_binding")
            task_input = _copy(data)
        task = {"schema": SCHEMA, "phase": phase,
                "system_instruction": payload["system_instruction"], "input": task_input}
        previous = _hash(self._native_receipt) if self._native_receipt else None
        control = {
            **_copy(self._policy),
            "provider_id": self.provider_id,
            "execution_key": _hash({"request": payload, "policy": self._policy,
                                    "previous_receipt_sha256": previous}),
            "request_sha256": _hash(payload), "task_sha256": _hash(task),
            "phase": phase,
            "previous_receipt_sha256": previous,
        }
        # No retries/corrections or nested delegation in this adapter.
        self._finished = True
        try:
            reply = self._host.run_isolated(_copy(task), control=_copy(control))
        except TimeoutError:
            raise SubagentReviewBlocked("timeout", retryable=True) from None
        except Exception:
            raise SubagentReviewBlocked("host_failed", retryable=True) from None
        reply = _copy(reply)
        if not isinstance(reply, dict) or set(reply) != {"response", "receipt"}:
            raise SubagentReviewBlocked("receipt_invalid")
        response, receipt = reply["response"], reply["receipt"]
        self._validate_receipt(receipt, response, control)
        if (not isinstance(response, dict)
                or response.get("schema") != "blun.website-localization-review.v2"
                or response.get("phase") != phase
                or response.get("locale") != data["target"]["locale"]
                or response.get("status") not in {"PASS", "FAIL"}
                or response.get("confidence") not in {"high", "low"}
                or not isinstance(response.get("blocking_defects"), list)
                or not isinstance(response.get("major_defects"), list)):
            raise SubagentReviewBlocked("review_invalid")
        # Existing worker validates full findings and commercial evidence.
        self._evidence[_hash(payload)] = (_hash(response), _copy(receipt))
        if (phase == "target_native" and response["status"] == "PASS"
                and not response["blocking_defects"] and not response["major_defects"]):
            self._native_receipt = receipt
            self._finished = False
        return response

    def _validate_receipt(self, receipt: Any, response: Any, control: dict) -> None:
        expected = {"schema", "execution_key", "request_sha256", "task_sha256",
                    "phase", "previous_receipt_sha256", "response_sha256",
                    "agent_id", "session_id", "model_id", "model_version",
                    "inherit_context", "tools", "max_delegation_depth"}
        if not isinstance(receipt, dict) or set(receipt) != expected:
            raise SubagentReviewBlocked("receipt_invalid")
        bound = {key: control[key] for key in expected - {
            "response_sha256", "agent_id", "session_id"}}
        if (_raw({key: receipt[key] for key in bound}) != _raw(bound)
                or receipt["response_sha256"] != _hash(response)):
            raise SubagentReviewBlocked("receipt_binding")
        agent, session = _id(receipt["agent_id"]), _id(receipt["session_id"])
        if (agent == self._policy["creator_id"]
                or session == self._policy["creator_session_id"]
                or (self._native_receipt is not None and (
                    agent == self._native_receipt["agent_id"]
                    or session == self._native_receipt["session_id"]))):
            raise SubagentReviewBlocked("self_review")
        try:
            valid = self._host.verify_execution(_copy(receipt), control=_copy(control))
        except Exception:
            raise SubagentReviewBlocked("verification_unavailable", retryable=True) from None
        if valid is not True:
            raise SubagentReviewBlocked("unverified_execution")

    def verified_call_evidence(self, request: Any, response: Any) -> dict | None:
        """Called by the worker, never by a model; commits the verified receipt."""
        payload = self._request(request)
        if payload["phase"] == "transcreation":
            return None
        saved = self._evidence.get(_hash(payload))
        if saved is None or saved[0] != _hash(response):
            raise SubagentReviewBlocked("evidence_missing")
        return _copy(saved[1])
