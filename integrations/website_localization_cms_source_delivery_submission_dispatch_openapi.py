#!/usr/bin/env python3
"""Canonical OpenAPI 3.1 profile for the public CMS submission sidecar."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


DOCUMENT_SCHEMA = "blun.cms-public-submission-dispatch-openapi.v8"
RESPONSE_SCHEMA = "blun.cms-public-submission-dispatch-openapi-response.v1"
EU_TARGET_LOCALES = (
    "bg-BG", "hr-HR", "cs-CZ", "da-DK", "nl-NL", "en-IE", "et-EE",
    "fi-FI", "fr-FR", "de-AT", "el-GR", "hu-HU", "ga-IE", "it-IT",
    "lv-LV", "lt-LT", "mt-MT", "pl-PL", "pt-PT", "ro-RO", "sk-SK",
    "sl-SI", "es-ES", "sv-SE",
)
CONTENT_TYPES = (
    "headline", "cta", "marketing", "ui", "documentation", "seo",
    "legal", "commercial",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _schema_ref(name: str) -> dict[str, str]:
    return {"$ref": "#/components/schemas/" + name}


def _closed_object(
    properties: Mapping[str, Any], *, optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(set(properties) - set(optional)),
        "properties": dict(properties),
    }


def _exact_schema(value: Any) -> dict[str, Any]:
    """Describe one content-free capability value without allowing drift."""
    if isinstance(value, Mapping):
        properties = {
            str(name): _exact_schema(content)
            for name, content in value.items()
        }
        return _closed_object(properties)
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "const": value}
    if isinstance(value, int):
        return {"type": "integer", "const": value}
    if isinstance(value, float):
        return {"type": "number", "const": value}
    if isinstance(value, str):
        return {"type": "string", "const": value}
    if isinstance(value, (list, tuple)):
        items = [_exact_schema(item) for item in value]
        return {
            "type": "array", "prefixItems": items, "items": False,
            "minItems": len(items), "maxItems": len(items),
        }
    raise TypeError("unsupported exact OpenAPI capability value")


def _bounded_text(*, source: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "string", "minLength": 1, "x-unicode-normalization": "NFC",
        "x-nul-forbidden": True,
    }
    if source:
        schema.update({"maxLength": 1_000_000, "x-max-utf8-bytes": 1_000_000})
    else:
        schema["maxLength"] = 256
    return schema


def _source_payload_schemas(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    contracts = capabilities["source_payload_schemas"]
    localization = _closed_object({
        "source_id": _bounded_text(),
        "source_revision": _bounded_text(),
        "source_text": _bounded_text(source=True),
        "source_locale": {
            **_bounded_text(),
            "pattern": "^[A-Za-z]{2,8}(?:-(?:[A-Za-z]{4}|[A-Za-z]{2}|[0-9]{3}|[A-Za-z0-9]{5,8}))*$",
            "x-canonical-bcp47": True,
        },
        "content_type": {"type": "string", "enum": list(CONTENT_TYPES)},
        "glossary_version": _bounded_text(),
        "policy_version": _bounded_text(),
        "provider_id": _bounded_text(),
        "model_id": _bounded_text(),
        "model_version": _bounded_text(),
        "software_version": _bounded_text(),
        "target_locales": {
            "type": "array", "minItems": 1, "maxItems": 24,
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(EU_TARGET_LOCALES)},
            "x-source-language-excluded": True,
        },
    }, optional=("target_locales",))
    common = {
        "event_id": _schema_ref("Token"),
        "site_id": _schema_ref("Token"),
        "website_version": _schema_ref("Token"),
        "source_sequence": {"type": "integer", "minimum": 1},
    }
    change = _closed_object({
        "schema": {"const": contracts["change"]},
        **common,
        "localization": _schema_ref("LocalizationRequest"),
    })
    cancellation = _closed_object({
        "schema": {"const": contracts["cancellation"]},
        "cancellation_id": _schema_ref("Token"),
        **common,
        "source_id": _schema_ref("Token"),
    })
    tombstone = _closed_object({
        "schema": {"const": contracts["tombstone"]},
        "tombstone_id": _schema_ref("Token"),
        **common,
        "source_id": _schema_ref("Token"),
    })
    return {
        "LocalizationRequest": localization,
        "ContentChange": change,
        "ContentCancellation": cancellation,
        "ContentTombstone": tombstone,
        "SourcePayload": {
            "oneOf": [
                _schema_ref("ContentChange"),
                _schema_ref("ContentCancellation"),
                _schema_ref("ContentTombstone"),
            ],
            "discriminator": {
                "propertyName": "schema",
                "mapping": {
                    contracts["change"]: "#/components/schemas/ContentChange",
                    contracts["cancellation"]: "#/components/schemas/ContentCancellation",
                    contracts["tombstone"]: "#/components/schemas/ContentTombstone",
                },
            },
            "description": (
                "One complete canonical change, cancellation, or tombstone; "
                "runtime validation remains authoritative for NFC, UTF-8 byte "
                "limits, BCP-47 canonicalization, and source-language exclusion."
            ),
            "x-downstream-capabilities-sha256": capabilities[
                "public_submission_capabilities_sha256"
            ],
        },
    }


def _operation(
    contract: Mapping[str, Any], *, operation_id: str,
    tag: str, summary: str, request_schema: str | None,
    response_component: str, headers: bool = False,
    degraded_response_component: str | None = None,
) -> dict[str, Any]:
    responses = {
        str(contract["success_status"]): {
            "description": "Exact validated content-free response.",
            "content": {
                "application/json": {
                    "schema": _schema_ref(response_component),
                },
            },
        },
    }
    for status in contract["error_statuses"]:
        codes = list(contract["error_codes"][str(status)])
        schema: dict[str, Any] = _closed_object({
            "schema": {
                "const": "blun.cms-public-submission-dispatch-http-error.v1",
            },
            "status": {"const": "BLOCK"},
            "error_code": {"type": "string", "enum": codes},
        })
        description = "Fail-closed content-free error."
        if status == 503 and degraded_response_component is not None:
            schema = {
                "oneOf": [
                    _schema_ref(degraded_response_component),
                    schema,
                ],
            }
            description = (
                "Validated degraded monitor state or fail-closed "
                "content-free error."
            )
        responses[str(status)] = {
            "description": description,
            "content": {"application/json": {"schema": schema}},
            "x-error-codes": codes,
        }
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "summary": summary,
        "tags": [tag],
        "security": [{"hostAuthentication": []}],
        "x-authentication-scope": contract["scope"],
        "x-principal-schema": contract["principal_schema"],
        "x-success-status": contract["success_status"],
        "x-error-statuses": list(contract["error_statuses"]),
        "x-error-codes": dict(contract["error_codes"]),
        "x-response-invariants": list(contract["response_invariants"]),
        "responses": dict(sorted(responses.items(), key=lambda item: int(item[0]))),
    }
    if request_schema is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _schema_ref(request_schema),
                },
            },
        }
    parameters = []
    precondition_header = contract["capabilities_precondition_header"]
    if precondition_header is not None:
        parameters.append({
            "name": precondition_header, "in": "header", "required": True,
            "schema": _schema_ref("Sha256"),
            "description": (
                "Must equal the complete capability SHA-256 obtained from the "
                "immediately preceding authenticated discovery response."
            ),
        })
    if headers:
        parameters.extend([
            {
                "name": "Idempotency-Key", "in": "header", "required": True,
                "schema": _schema_ref("Token"),
                "description": "Must equal the immutable request identity.",
            },
            {
                "name": "X-Localization-Source-Payload-SHA256",
                "in": "header", "required": True,
                "schema": _schema_ref("Sha256"),
                "description": "SHA-256 of the complete canonical source payload.",
            },
        ])
    if parameters:
        operation["parameters"] = parameters
    return operation


def _status_schema() -> dict[str, Any]:
    nullable_sha = {"oneOf": [_schema_ref("Sha256"), {"type": "null"}]}
    nullable_count = {
        "oneOf": [
            {"type": "integer", "minimum": 0, "maximum": 20},
            {"type": "null"},
        ],
    }
    nullable_time = {
        "oneOf": [{"type": "number", "minimum": 0}, {"type": "null"}],
    }
    properties = {
        "operation": {"type": "string", "enum": ["change", "cancellation", "tombstone"]},
        "request_id": _schema_ref("Token"), "event_id": _schema_ref("Token"),
        "site_id": _schema_ref("Token"), "payload_sha256": _schema_ref("Sha256"),
        "source_max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
        "delivery_max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
        "client_max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
        "status": {"type": "string", "enum": ["accepted", "failed", "leased", "pending", "retry_wait"]},
        "attempts": {"type": "integer", "minimum": 0, "maximum": 20},
        "next_attempt_at": {"type": "number", "minimum": 0},
        "lease_expires_at": nullable_time, "lease_expired": {"type": "boolean"},
        "last_error_code": {
            "oneOf": [_schema_ref("ErrorCode"), {"type": "null"}],
        },
        "remote_status": {
            "type": ["string", "null"],
            "enum": [None, "failed", "leased", "pending", "retry_wait", "succeeded"],
        },
        "remote_attempts": nullable_count,
        "remote_capabilities_sha256": nullable_sha,
        "remote_binding_sha256": nullable_sha,
        "remote_website_capability_binding": {
            "oneOf": [
                _schema_ref("WebsiteCapabilityBinding"), {"type": "null"},
            ],
        },
        "response_sha256": nullable_sha,
    }
    remote_complete = {
        "properties": {
            "remote_status": {
                "type": "string",
                "enum": ["failed", "leased", "pending", "retry_wait", "succeeded"],
            },
            "remote_attempts": {
                "type": "integer", "minimum": 0, "maximum": 20,
            },
            "remote_capabilities_sha256": _schema_ref("Sha256"),
            "remote_binding_sha256": _schema_ref("Sha256"),
            "remote_website_capability_binding": _schema_ref(
                "WebsiteCapabilityBinding"
            ),
            "response_sha256": _schema_ref("Sha256"),
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": sorted(properties), "properties": properties,
        "allOf": [
            {
                "if": {"properties": {"status": {"const": "leased"}}},
                "then": {"properties": {"lease_expires_at": {"type": "number", "minimum": 0}}},
                "else": {"properties": {"lease_expires_at": {"type": "null"}}},
            },
            {
                "if": {"properties": {"status": {"const": "accepted"}}},
                "then": remote_complete,
                "else": {"not": remote_complete},
            },
        ],
        "x-invariants": [
            "attempts_lte_client_max_attempts",
            "leased_iff_lease_expires_at",
            "accepted_iff_remote_website_binding_complete_and_valid",
        ],
        "description": "Content-free durable submission state; accepted is not publication.",
    }


def build_document(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    """Build one origin-free document from an already verified capability."""
    operations = capabilities["operations"]
    all_error_codes = sorted({
        code
        for operation in operations.values()
        for codes in operation["error_codes"].values()
        for code in codes
    })
    path_specs = {
        operations["capabilities"]["path"]: {
            "get": _operation(
                operations["capabilities"], operation_id="discoverCapabilities",
                tag="Discovery", summary="Discover the exact active sidecar contract",
                request_schema=None, response_component="CapabilitiesResponse",
            ),
        },
        operations["enqueue"]["path"]: {
            "post": _operation(
                operations["enqueue"], operation_id="enqueueSubmission",
                tag="Tenant submissions",
                summary="Durably enqueue one change, cancellation, or tombstone",
                request_schema="EnqueueRequest", response_component="QueueResponse",
                headers=True,
            ),
        },
        operations["health"]["path"]: {
            "get": _operation(
                operations["health"], operation_id="readHealth",
                tag="Operations", summary="Read durable outbox health",
                request_schema=None, response_component="HealthResponse",
                degraded_response_component="HealthResponse",
            ),
        },
        operations["openapi"]["path"]: {
            "get": _operation(
                operations["openapi"], operation_id="readOpenApi",
                tag="Discovery", summary="Read this capability-bound OpenAPI profile",
                request_schema=None, response_component="OpenApiResponse",
            ),
        },
        operations["readiness"]["path"]: {
            "get": _operation(
                operations["readiness"], operation_id="readReadiness",
                tag="Operations", summary="Read supervised worker readiness",
                request_schema=None, response_component="ReadinessResponse",
                degraded_response_component="ReadinessResponse",
            ),
        },
        operations["status"]["path"]: {
            "post": _operation(
                operations["status"], operation_id="readSubmissionStatus",
                tag="Tenant submissions", summary="Read one fully identified submission",
                request_schema="StatusRequest", response_component="StatusResponse",
            ),
        },
    }
    envelope = lambda schema, key, reference: {
        "type": "object", "additionalProperties": False,
        "required": ["schema", key, "capabilities_sha256"],
        "properties": {
            "schema": {"const": schema}, key: reference,
            "capabilities_sha256": {"const": capabilities["sha256"]},
        },
    }
    status_envelope = lambda schema: {
        "type": "object", "additionalProperties": False,
        "required": [
            "schema", "api_schema", "status", "capabilities_sha256",
            "accepted_implies_publication",
        ],
        "properties": {
            "schema": {"const": schema},
            "api_schema": {"const": capabilities["api_schema"]},
            "status": _schema_ref("SubmissionStatus"),
            "capabilities_sha256": {"const": capabilities["sha256"]},
            "accepted_implies_publication": {"const": False},
        },
    }
    count_map = {
        "type": "object", "additionalProperties": False,
        "required": ["accepted", "failed", "leased", "pending", "retry_wait"],
        "properties": {
            name: {"type": "integer", "minimum": 0, "maximum": 1_000_000}
            for name in ("accepted", "failed", "leased", "pending", "retry_wait")
        },
    }
    operation_map = {
        "type": "object", "additionalProperties": False,
        "required": ["cancellation", "change", "tombstone"],
        "properties": {
            name: {"type": "integer", "minimum": 0, "maximum": 1_000_000}
            for name in ("cancellation", "change", "tombstone")
        },
    }
    schemas = {
        "Sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "Token": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,256}$"},
        "ErrorCode": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,127}$"},
        "WebsiteCapabilityBinding": _closed_object({
            "schema": {
                "const": "blun.cms-source-delivery-runtime-capability-binding.v2",
            },
            "status": {"const": "verified"},
            "database_role": {"const": "source_delivery"},
            "delivery_capabilities_sha256": _schema_ref("Sha256"),
            "runtime_capabilities_sha256": _schema_ref("Sha256"),
            "commercial_rendering_registry_sha256": _schema_ref("Sha256"),
            "terminal_receiver_capabilities_sha256": _schema_ref("Sha256"),
            "binding_sha256": _schema_ref("Sha256"),
        }),
        "Error": {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "status", "error_code"],
            "properties": {
                "schema": {"const": "blun.cms-public-submission-dispatch-http-error.v1"},
                "status": {"const": "BLOCK"},
                "error_code": {"type": "string", "enum": all_error_codes},
            },
        },
        "Capabilities": {
            **_exact_schema(capabilities),
            "description": (
                "The complete immutable capability generation. Every nested "
                "field is closed and fixed to this document's active pins."
            ),
            "x-capabilities-sha256": capabilities["sha256"],
        },
        **_source_payload_schemas(capabilities),
        "EnqueueRequest": {
            "type": "object", "additionalProperties": False,
            "required": [
                "schema", "payload", "source_max_attempts",
                "delivery_max_attempts", "client_max_attempts",
            ],
            "properties": {
                "schema": {"const": operations["enqueue"]["request_schema"]},
                "payload": _schema_ref("SourcePayload"),
                "source_max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
                "delivery_max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
                "client_max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
        "StatusRequest": {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "operation", "request_id", "event_id", "site_id", "payload_sha256"],
            "properties": {
                "schema": {"const": operations["status"]["request_schema"]},
                "operation": {"type": "string", "enum": ["change", "cancellation", "tombstone"]},
                "request_id": _schema_ref("Token"), "event_id": _schema_ref("Token"),
                "site_id": _schema_ref("Token"), "payload_sha256": _schema_ref("Sha256"),
            },
        },
        "SubmissionStatus": _status_schema(),
        "QueueResponse": status_envelope(operations["enqueue"]["response_schema"]),
        "StatusResponse": status_envelope(operations["status"]["response_schema"]),
        "Health": {
            "type": "object", "additionalProperties": False,
            "required": ["status", "counts", "operations", "due", "expired_leases", "failed", "expected_capabilities_sha256"],
            "properties": {
                "status": {"type": "string", "enum": ["blocked", "ok"]},
                "counts": count_map, "operations": operation_map,
                "due": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                "expired_leases": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                "failed": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                "expected_capabilities_sha256": {"const": capabilities["public_submission_capabilities_sha256"]},
            },
            "allOf": [{
                "if": {"properties": {"status": {"const": "ok"}}},
                "then": {"properties": {
                    "expired_leases": {"const": 0}, "failed": {"const": 0},
                }},
                "else": {"anyOf": [
                    {"properties": {"expired_leases": {"minimum": 1}}},
                    {"properties": {"failed": {"minimum": 1}}},
                ]},
            }],
            "x-invariants": list(operations["health"]["response_invariants"]),
        },
        "HealthResponse": envelope(
            operations["health"]["response_schema"], "health", _schema_ref("Health")
        ),
        "Readiness": {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "status", "worker_state", "outbox_status", "error_code", "capabilities_sha256"],
            "properties": {
                "schema": {"const": "blun.cms-public-submission-worker-readiness.v1"},
                "status": {"type": "string", "enum": ["not_ready", "ready"]},
                "worker_state": {"type": "string", "enum": ["closed", "failed", "running", "stopped", "stopping", "unmanaged"]},
                "outbox_status": {"type": ["string", "null"], "enum": [None, "blocked", "ok"]},
                "error_code": {"oneOf": [_schema_ref("ErrorCode"), {"type": "null"}]},
                "capabilities_sha256": {"const": capabilities["public_submission_capabilities_sha256"]},
            },
            "oneOf": [
                {"properties": {
                    "status": {"const": "ready"},
                    "worker_state": {"const": "running"},
                    "outbox_status": {"const": "ok"},
                    "error_code": {"type": "null"},
                }},
                {"properties": {
                    "status": {"const": "not_ready"},
                    "error_code": _schema_ref("ErrorCode"),
                }},
            ],
            "x-invariants": list(operations["readiness"]["response_invariants"]),
        },
        "ReadinessResponse": envelope(
            operations["readiness"]["response_schema"], "readiness", _schema_ref("Readiness")
        ),
        "CapabilitiesResponse": {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "capabilities"],
            "properties": {
                "schema": {"const": operations["capabilities"]["response_schema"]},
                "capabilities": _schema_ref("Capabilities"),
            },
        },
        "OpenApiResponse": {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "openapi", "openapi_sha256", "capabilities_sha256"],
            "properties": {
                "schema": {"const": RESPONSE_SCHEMA},
                "openapi": {"type": "object"},
                "openapi_sha256": _schema_ref("Sha256"),
                "capabilities_sha256": {"const": capabilities["sha256"]},
            },
        },
    }
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "CMS Public Submission Sidecar API",
            "version": capabilities["api_schema"],
            "description": (
                "Provider-neutral, fail-closed transport for durable website "
                "localization submissions. Acceptance never implies publication."
            ),
        },
        "tags": [
            {"name": "Discovery"}, {"name": "Tenant submissions"},
            {"name": "Operations"},
        ],
        "paths": dict(sorted(path_specs.items())),
        "components": {
            "securitySchemes": {
                "hostAuthentication": {
                    "type": "apiKey", "in": "header", "name": "Authorization",
                    "description": (
                        "Deployment-owned credential placeholder. The host may require "
                        "additional signed headers and exact body-hash binding."
                    ),
                    "x-host-defined": True,
                },
            },
            "schemas": schemas,
        },
        "x-schema": DOCUMENT_SCHEMA,
        "x-capabilities-sha256": capabilities["sha256"],
        "x-downstream-capabilities-sha256": capabilities["public_submission_capabilities_sha256"],
        "x-retry-ownership": capabilities["retry_ownership"],
        "x-fail-closed-semantics": capabilities["semantics"],
        "x-servers-omitted-intentionally": True,
    }


def document_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(document))).hexdigest()
