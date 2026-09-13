#!/usr/bin/env python3
"""Canonical OpenAPI 3.1 profile for the public CMS submission sidecar."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


DOCUMENT_SCHEMA = "blun.cms-public-submission-dispatch-openapi.v1"
RESPONSE_SCHEMA = "blun.cms-public-submission-dispatch-openapi-response.v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _schema_ref(name: str) -> dict[str, str]:
    return {"$ref": "#/components/schemas/" + name}


def _operation(
    contract: Mapping[str, Any], *, operation_id: str,
    tag: str, summary: str, request_schema: str | None,
    response_component: str, headers: bool = False,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "summary": summary,
        "tags": [tag],
        "security": [{"hostAuthentication": []}],
        "x-authentication-scope": contract["scope"],
        "x-principal-schema": contract["principal_schema"],
        "x-success-status": contract["success_status"],
        "responses": {
            str(contract["success_status"]): {
                "description": "Exact validated content-free response.",
                "content": {
                    "application/json": {
                        "schema": _schema_ref(response_component),
                    },
                },
            },
            "default": {
                "description": "Fail-closed content-free error.",
                "content": {
                    "application/json": {"schema": _schema_ref("Error")},
                },
            },
        },
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
    if headers:
        operation["parameters"] = [
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
        ]
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
        "response_sha256": nullable_sha,
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": sorted(properties), "properties": properties,
        "description": "Content-free durable submission state; accepted is not publication.",
    }


def build_document(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    """Build one origin-free document from an already verified capability."""
    operations = capabilities["operations"]
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
        "Error": {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "status", "error_code"],
            "properties": {
                "schema": {"const": "blun.cms-public-submission-dispatch-http-error.v1"},
                "status": {"const": "BLOCK"}, "error_code": _schema_ref("ErrorCode"),
            },
        },
        "SourcePayload": {
            "type": "object",
            "description": (
                "One complete canonical change, cancellation, or tombstone. "
                "The downstream public capability supplies the exact versioned payload schema."
            ),
            "x-downstream-capabilities-sha256": capabilities["public_submission_capabilities_sha256"],
        },
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
        },
        "ReadinessResponse": envelope(
            operations["readiness"]["response_schema"], "readiness", _schema_ref("Readiness")
        ),
        "CapabilitiesResponse": {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "capabilities"],
            "properties": {
                "schema": {"const": operations["capabilities"]["response_schema"]},
                "capabilities": {
                    "type": "object",
                    "description": "Must equal the canonical active capability document byte-for-byte.",
                    "x-capabilities-sha256": capabilities["sha256"],
                },
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
