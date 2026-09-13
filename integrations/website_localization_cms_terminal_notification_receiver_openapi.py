#!/usr/bin/env python3
"""Canonical OpenAPI 3.1 profile for the terminal-notification receiver."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


DOCUMENT_SCHEMA = "blun.cms-terminal-receiver-openapi.v2"
RESPONSE_SCHEMA = "blun.cms-terminal-receiver-openapi-response.v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _ref(name: str) -> dict[str, str]:
    return {"$ref": "#/components/schemas/" + name}


def _object(
    properties: Mapping[str, Any], *, optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(set(properties) - set(optional)),
        "properties": dict(properties),
    }


def _exact(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _object({str(key): _exact(item) for key, item in value.items()})
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "const": value}
    if isinstance(value, int):
        return {"type": "integer", "const": value}
    if isinstance(value, str):
        return {"type": "string", "const": value}
    if isinstance(value, (list, tuple)):
        items = [_exact(item) for item in value]
        return {
            "type": "array", "prefixItems": items, "items": False,
            "minItems": len(items), "maxItems": len(items),
        }
    raise TypeError("unsupported exact capability value")


def _error_response(status: int, codes: tuple[str, ...]) -> dict[str, Any]:
    return {
        "description": "Fail-closed, content-free receiver error.",
        "content": {"application/json": {"schema": _object({
            "schema": {"const": "blun.cms-source-terminal-notification-http-error.v1"},
            "status": {"const": "BLOCK"},
            "error_code": {"type": "string", "enum": list(codes)},
        })}},
        "x-error-codes": list(codes),
        "x-http-status": status,
    }


_COMMON = {
    400: (
        "notification_receiver.body_invalid",
        "notification_receiver.framing_invalid",
        "notification_receiver.headers_invalid",
        "notification_receiver.https_required",
        "notification_receiver.query_rejected",
    ),
    401: ("notification_receiver.authentication_failed",),
    403: ("notification_receiver.authorization_failed",),
    405: ("notification_receiver.method_not_allowed",),
    503: (
        "notification_receiver.authentication_unavailable",
        "notification_receiver.capabilities_invalid",
        "notification_receiver.internal",
        "notification_receiver.runtime_unavailable",
    ),
}


def _errors(name: str) -> dict[int, tuple[str, ...]]:
    result = {status: tuple(codes) for status, codes in _COMMON.items()}
    if name != "capabilities":
        result[412] = ("notification_receiver.capabilities_precondition_failed",)
        result[428] = ("notification_receiver.capabilities_precondition_required",)
    if name in {"capabilities", "health", "openapi", "readiness"}:
        result[415] = ("notification_receiver.content_type",)
    if name == "health":
        result[503] += ("notification_receiver.health_invalid",)
    if name == "notification":
        result[400] += (
            "notification_receiver.canonical_body_required",
            "notification_receiver.header_binding_invalid",
            "notification_receiver.json_invalid",
            "notification_receiver.request_invalid",
        )
        result[409] = ("notification_receiver.idempotency_collision",)
        result[411] = ("notification_receiver.content_length_required",)
        result[413] = ("notification_receiver.body_too_large",)
        result[415] = ("notification_receiver.content_type",)
        result[503] += (
            "notification_receiver.clock_invalid",
            "notification_receiver.database_unsafe",
            "notification_receiver.foreign_process",
            "notification_receiver.integrity_failed",
            "notification_receiver.schema_altered",
            "notification_receiver.storage_unavailable",
            "notification_receiver.stored_binding_invalid",
            "notification_receiver.transaction_nested",
        )
    if name == "status":
        result[400] += (
            "notification_receiver.header_binding_invalid",
            "notification_receiver.json_invalid",
            "notification_receiver.request_invalid",
        )
        result[404] = ("notification_receiver.not_found",)
        result[411] = ("notification_receiver.content_length_required",)
        result[413] = ("notification_receiver.body_too_large",)
        result[415] = ("notification_receiver.content_type",)
    return {status: tuple(sorted(set(codes))) for status, codes in result.items()}


def _operation(
    name: str, contract: Mapping[str, Any], *, response: str,
    request: str | None = None, degraded: bool = False,
) -> dict[str, Any]:
    responses: dict[str, Any] = {
        str(contract["success_status"]): {
            "description": "Exact validated content-free response.",
            "content": {"application/json": {"schema": _ref(response)}},
        },
    }
    for status, codes in _errors(name).items():
        error = _error_response(status, codes)
        if degraded and status == 503:
            error["content"]["application/json"]["schema"] = {
                "oneOf": [_ref(response), error["content"]["application/json"]["schema"]],
            }
        responses[str(status)] = error
    operation: dict[str, Any] = {
        "operationId": {
            "capabilities": "discoverTerminalReceiverCapabilities",
            "health": "readTerminalReceiverHealth",
            "notification": "acceptTerminalNotification",
            "openapi": "readTerminalReceiverOpenApi",
            "readiness": "readTerminalReceiverReadiness",
            "status": "readTerminalNotificationStatus",
        }[name],
        "security": [{"hostAuthentication": []}],
        "x-authentication-scope": contract["scope"],
        "responses": dict(sorted(responses.items(), key=lambda item: int(item[0]))),
    }
    if request is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _ref(request)}},
        }
    parameters = []
    if name != "capabilities":
        parameters.append({
            "name": contract["capabilities_precondition_header"],
            "in": "header", "required": True, "schema": _ref("Sha256"),
        })
    if name == "notification":
        parameters.extend([
            {"name": "Idempotency-Key", "in": "header", "required": True,
             "schema": _ref("NotificationId")},
            {"name": "X-Localization-Terminal-Notification-Id", "in": "header",
             "required": True, "schema": _ref("NotificationId")},
            {"name": "X-Localization-Terminal-Notification-Sha256", "in": "header",
             "required": True, "schema": _ref("Sha256")},
        ])
    elif name == "status":
        parameters.append({
            "name": "X-Localization-Terminal-Status-SHA256", "in": "header",
            "required": True, "schema": _ref("Sha256"),
        })
    if parameters:
        operation["parameters"] = parameters
    return operation


def build_document(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    """Build the origin-free document for one exact capability generation."""
    operations = capabilities["operations"]
    sha256 = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    token = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,256}$"}
    nullable_time = {"oneOf": [{"type": "number", "minimum": 0}, {"type": "null"}]}
    nullable_error = {"oneOf": [_ref("ErrorCode"), {"type": "null"}]}
    count = {"type": "integer", "minimum": 0}
    processing_statuses = capabilities["processing_statuses"]
    terminal_statuses = capabilities["terminal_statuses"]
    notification = _object({
        "schema": {"const": operations["notification"]["request_schema"]},
        "notification_id": _ref("NotificationId"),
        "event_id": _ref("Token"), "site_id": _ref("Token"),
        "plan_id": _ref("Token"), "website_version": _ref("Token"),
        "source_sequence": {"type": "integer", "minimum": 1},
        "job_count": {"type": "integer", "minimum": 1},
        "change_sha256": _ref("Sha256"),
        "lifecycle_binding_sha256": _ref("Sha256"),
        "terminal_status": {"type": "string", "enum": terminal_statuses},
        "lifecycle_sha256": {"oneOf": [_ref("Sha256"), {"type": "null"}]},
    })
    notification["allOf"] = [{
        "if": {"properties": {"terminal_status": {"enum": ["cancelled", "superseded"]}}},
        "then": {"properties": {"lifecycle_sha256": {"type": "null"}}},
        "else": {"properties": {"lifecycle_sha256": _ref("Sha256")}},
    }]
    status_response = _object({
        "schema": {"const": operations["status"]["response_schema"]},
        "notification_id": _ref("NotificationId"),
        "event_id": _ref("Token"), "site_id": _ref("Token"),
        "terminal_status": {"type": "string", "enum": terminal_statuses},
        "notification_sha256": _ref("Sha256"),
        "processing_status": {"type": "string", "enum": processing_statuses},
        "attempts": {"type": "integer", "minimum": 0, "maximum": 20},
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
        "next_attempt_at": {"type": "number", "minimum": 0},
        "lease_expires_at": nullable_time, "lease_expired": {"type": "boolean"},
        "last_error_code": nullable_error, "processed_at": nullable_time,
        "capabilities_sha256": {"const": capabilities["sha256"]},
    })
    count_properties = {name: count for name in processing_statuses}
    health = _object({
        "schema": {"const": operations["health"]["response_schema"]},
        "status": {"type": "string", "enum": ["ok", "blocked"]},
        "runtime_state": {"const": "open"},
        "worker_state": {"type": "string", "enum": [
            "unmanaged", "starting", "running", "stopping", "stopped", "failed",
        ]},
        "inbox_status": {"type": "string", "enum": ["ok", "blocked"]},
        "received": {"oneOf": [count, {"type": "null"}]},
        "processing_counts": {"oneOf": [_object(count_properties), {"type": "null"}]},
        "processing_due": {"oneOf": [count, {"type": "null"}]},
        "expired_leases": {"oneOf": [count, {"type": "null"}]},
        "failed": {"oneOf": [count, {"type": "null"}]},
        "error_code": nullable_error,
        "capabilities_sha256": {"const": capabilities["sha256"]},
    })
    readiness = _object({
        "schema": {"const": operations["readiness"]["response_schema"]},
        "status": {"type": "string", "enum": ["ready", "not_ready"]},
        "worker_state": {"type": "string", "enum": [
            "unmanaged", "running", "stopping", "stopped", "failed", "closed",
        ]},
        "inbox_status": {"oneOf": [
            {"type": "string", "enum": ["ok", "blocked"]}, {"type": "null"},
        ]},
        "error_code": nullable_error,
        "capabilities_sha256": {"const": capabilities["sha256"]},
    })
    schemas = {
        "Sha256": sha256, "Token": token,
        "NotificationId": {"type": "string", "pattern": "^terminal-[a-f0-9]{64}$"},
        "ErrorCode": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,127}$"},
        "Capabilities": {**_exact(capabilities), "x-capabilities-sha256": capabilities["sha256"]},
        "CapabilitiesResponse": _object({
            "schema": {"const": operations["capabilities"]["response_schema"]},
            "capabilities": _ref("Capabilities"),
        }),
        "TerminalNotification": notification,
        "NotificationAck": _object({
            "schema": {"const": operations["notification"]["response_schema"]},
            "notification_id": _ref("NotificationId"), "event_id": _ref("Token"),
            "site_id": _ref("Token"), "status": {"const": "accepted"},
            "notification_sha256": _ref("Sha256"),
        }),
        "StatusRequest": _object({
            "schema": {"const": operations["status"]["request_schema"]},
            "event_id": _ref("Token"), "site_id": _ref("Token"),
        }),
        "StatusResponse": status_response,
        "HealthResponse": health,
        "ReadinessResponse": readiness,
        "OpenApiResponse": _object({
            "schema": {"const": RESPONSE_SCHEMA}, "openapi": {"type": "object"},
            "openapi_sha256": _ref("Sha256"),
            "capabilities_sha256": {"const": capabilities["sha256"]},
        }),
    }
    paths = {}
    definitions = {
        "capabilities": (None, "CapabilitiesResponse", False),
        "health": (None, "HealthResponse", True),
        "notification": ("TerminalNotification", "NotificationAck", False),
        "openapi": (None, "OpenApiResponse", False),
        "readiness": (None, "ReadinessResponse", True),
        "status": ("StatusRequest", "StatusResponse", False),
    }
    for name, (request, response, degraded) in definitions.items():
        contract = operations[name]
        paths[contract["path"]] = {
            contract["method"].lower(): _operation(
                name, contract, request=request, response=response, degraded=degraded,
            ),
        }
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "CMS Terminal Notification Receiver API",
            "version": capabilities["api_schema"],
            "description": (
                "Provider-neutral, fail-closed terminal callback receiver. "
                "Acknowledgement means durable intake only."
            ),
        },
        "paths": dict(sorted(paths.items())),
        "components": {
            "securitySchemes": {"hostAuthentication": {
                "type": "apiKey", "in": "header", "name": "Authorization",
                "x-host-defined": True,
            }},
            "schemas": schemas,
        },
        "x-schema": DOCUMENT_SCHEMA,
        "x-capabilities-sha256": capabilities["sha256"],
        "x-content-free-responses": True,
        "x-servers-omitted-intentionally": True,
    }


def document_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(document))).hexdigest()
