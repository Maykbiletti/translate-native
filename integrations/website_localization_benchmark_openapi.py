#!/usr/bin/env python3
"""Canonical OpenAPI 3.1 document for benchmark evidence readers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


DOCUMENT_SCHEMA = "blun.website-localization-benchmark-openapi.v1"
RESPONSE_SCHEMA = "blun.website-localization-benchmark-openapi-response.v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def document_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _ref(name: str) -> dict[str, str]:
    return {"$ref": "#/components/schemas/" + name}


def _closed(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": dict(properties),
    }


def _operation(value: Mapping[str, Any]) -> dict[str, Any]:
    responses = {
        str(value["success_status"]): {
            "description": "Exact authenticated benchmark reader response.",
            "content": {
                "application/json": {
                    "schema": _ref(value["response_component"]),
                },
            },
        },
    }
    for status in value["error_statuses"]:
        responses[str(status)] = {
            "description": "Fail-closed content-free error.",
            "content": {
                "application/json": {"schema": _ref("ErrorResponse")},
            },
        }
    return {
        "operationId": value["operation_id"],
        "summary": value["summary"],
        "security": [{"readerAuthentication": []}],
        "x-authentication-request-schema": value["auth_request_schema"],
        "x-principal-schema": value["principal_schema"],
        "x-success-status": value["success_status"],
        "x-error-statuses": list(value["error_statuses"]),
        "responses": dict(sorted(responses.items(), key=lambda item: int(item[0]))),
    }


def _schemas(contract: Mapping[str, Any]) -> dict[str, Any]:
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    campaign_id = {
        "type": "string", "pattern": "^benchmark-campaign-[0-9a-f]{64}$",
    }
    count = {"type": "integer", "minimum": 0}
    finalization = _closed({
        "status": {"type": "string", "enum": list(contract["statuses"])},
        "attempt": count,
        "max_attempts": {"type": "integer", "minimum": 1},
        "next_attempt_at": {"type": "number", "minimum": 0},
        "error_code": {
            "oneOf": [
                {"type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,127}$"},
                {"type": "null"},
            ],
        },
    })
    campaign = _closed({
        "campaign_id": campaign_id,
        "policy_sha256": sha,
        "suite_sha256": sha,
        "valid_until": {"type": "number", "minimum": 0},
        "work_count": {"type": "integer", "minimum": 1},
        "counts": _closed({name: count for name in contract["statuses"]}),
        "error_counts": {
            "type": "object",
            "additionalProperties": count,
            "propertyNames": {"pattern": "^[a-z][a-z0-9_.-]{0,127}$"},
        },
        "complete": {"type": "boolean"},
        "blocked": {"type": "boolean"},
        "report_finalization": finalization,
    })
    report = _closed({
        "schema": {"const": contract["benchmark_report_schema"]},
        "benchmark_version": {"type": "string", "minLength": 1},
        "valid_until": {"type": "number", "minimum": 0},
        "suite": {"type": "object"},
        "candidate": {"type": "object"},
        "quality_profiles": {"type": "array"},
        "native_references": {"type": "object"},
        "baseline": {"type": "object"},
        "reviewer": {"type": "object"},
        "case_evidence_sha256": sha,
        "baseline_evidence_sha256": sha,
        "required_locales": {"type": "array", "items": {"type": "string"}},
        "decision_policy": {"type": "object"},
        "claim_scope": {"type": "object"},
        "configured_lanes_status": {"type": "string", "enum": ["PASS", "BLOCK"]},
        "claim_block_reasons": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["PASS", "BLOCK"]},
        "superiority_claim_allowed": {"type": "boolean"},
        "locales": {"type": "array", "items": {"type": "object"}},
        "attestation": {"type": "object"},
    })
    return {
        "CampaignStatus": campaign,
        "StatusResponse": _closed({
            "schema": {"const": contract["status_response_schema"]},
            "campaign": _ref("CampaignStatus"),
        }),
        "BenchmarkReport": report,
        "ReportResponse": _closed({
            "schema": {"const": contract["report_response_schema"]},
            "campaign_id": campaign_id,
            "report_sha256": sha,
            "report": _ref("BenchmarkReport"),
        }),
        "OpenAPIResponse": _closed({
            "schema": {"const": RESPONSE_SCHEMA},
            "contract_sha256": sha,
            "openapi_sha256": sha,
            "openapi": {"type": "object"},
        }),
        "ErrorResponse": _closed({
            "schema": {"const": contract["error_response_schema"]},
            "error_code": {
                "type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
            },
            "retryable": {"type": "boolean"},
        }),
    }


def build_document(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Build an origin-free document from one validated closed contract."""
    required = {
        "schema", "version", "auth_request_schema", "principal_schema",
        "status_response_schema", "report_response_schema",
        "error_response_schema", "benchmark_report_schema", "statuses",
        "max_response_bytes", "operations",
    }
    if not isinstance(contract, Mapping) or set(contract) != required:
        raise ValueError("benchmark OpenAPI contract is invalid")
    operations = contract["operations"]
    if not isinstance(operations, Mapping) or not operations:
        raise ValueError("benchmark OpenAPI operations are invalid")
    paths: dict[str, Any] = {}
    for name in sorted(operations):
        operation = operations[name]
        if not isinstance(operation, Mapping) or operation.get("name") != name:
            raise ValueError("benchmark OpenAPI operation is invalid")
        paths[operation["path"]] = {
            operation["method"].lower(): _operation(operation),
        }
    return json.loads(_canonical({
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "Website Localization Benchmark Evidence API",
            "version": contract["version"],
            "description": (
                "Authenticated read-only access to content-free campaign status, "
                "a stored reverified benchmark report, and this origin-free contract."
            ),
        },
        "tags": [{"name": "Benchmark evidence"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "readerAuthentication": {
                    "type": "http", "scheme": "bearer",
                    "description": (
                        "Example transport only; the host authenticator remains "
                        "provider-neutral and authoritative."
                    ),
                },
            },
            "schemas": _schemas(contract),
        },
        "x-contract-schema": contract["schema"],
        "x-contract-sha256": document_sha256(contract),
        "x-max-response-bytes": contract["max_response_bytes"],
        "x-origin-free": True,
        "x-runtime-validation-authoritative": True,
    }))
