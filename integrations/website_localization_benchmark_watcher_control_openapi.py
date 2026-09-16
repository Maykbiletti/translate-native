#!/usr/bin/env python3
"""Canonical origin-free OpenAPI 3.1 contract for watcher recovery."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


DOCUMENT_SCHEMA = (
    "blun.website-localization-benchmark-watcher-control-openapi.v1"
)
RESPONSE_SCHEMA = (
    "blun.website-localization-benchmark-watcher-control-openapi-response.v1"
)


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
            "description": "Exact authenticated watcher control response.",
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
    result = {
        "operationId": value["operation_id"],
        "summary": value["summary"],
        "security": [{"operatorAuthentication": []}],
        "x-required-scope": value["scope"],
        "x-authentication-request-schema": value["auth_request_schema"],
        "x-principal-schema": value["principal_schema"],
        "x-success-status": value["success_status"],
        "x-error-statuses": list(value["error_statuses"]),
        "responses": dict(sorted(responses.items(), key=lambda item: int(item[0]))),
    }
    if value["request_component"] is not None:
        result["parameters"] = [{
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": _ref("Identifier"),
            "description": "Must equal the request_id in the canonical body.",
        }]
        result["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _ref(value["request_component"]),
                },
            },
        }
    return result


def _schemas(contract: Mapping[str, Any]) -> dict[str, Any]:
    identifier = {
        "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$",
    }
    error_code = {
        "type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
    }
    timestamp = {"type": "number", "minimum": 0, "maximum": 10**12}
    attempts = {
        "type": "integer", "minimum": 1, "maximum": contract["max_attempts"],
    }
    generation = _closed({
        "attempts": attempts,
        "failed_at": timestamp,
        "error_code": error_code,
    })
    failed_status = _closed({
        "schema": {"const": contract["status_schema"]},
        "checked_at": timestamp,
        "state": {"const": "failed"},
        "rearmable": {"const": True},
        "generation": generation,
    })
    inactive_status = _closed({
        "schema": {"const": contract["status_schema"]},
        "checked_at": timestamp,
        "state": {
            "type": "string",
            "enum": [state for state in contract["states"] if state != "failed"],
        },
        "rearmable": {"const": False},
        "generation": {"type": "null"},
    })
    request = _closed({
        "schema": {"const": contract["request_schema"]},
        "request_id": identifier,
        "expected_attempts": attempts,
        "expected_failed_at": timestamp,
        "expected_error_code": error_code,
    })
    receipt = _closed({
        "schema": {"const": contract["receipt_schema"]},
        "request_sha256": {
            "type": "string", "pattern": "^[0-9a-f]{64}$",
        },
        "previous_state": {"const": "failed"},
        "previous_attempts": attempts,
        "previous_error_code": error_code,
        "failed_at": timestamp,
        "state": {"const": "pending"},
        "rearmed_at": timestamp,
    })
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    return {
        "Identifier": identifier,
        "FailedGeneration": generation,
        "WatcherStatus": {"oneOf": [failed_status, inactive_status]},
        "StatusResponse": _closed({
            "schema": {"const": contract["status_response_schema"]},
            "status": _ref("WatcherStatus"),
        }),
        "RearmRequest": request,
        "RearmReceipt": receipt,
        "RearmResponse": _closed({
            "schema": {"const": contract["rearm_response_schema"]},
            "receipt": _ref("RearmReceipt"),
        }),
        "OpenAPIResponse": _closed({
            "schema": {"const": RESPONSE_SCHEMA},
            "contract_sha256": sha,
            "openapi_sha256": sha,
            "openapi": {"type": "object"},
        }),
        "ErrorResponse": _closed({
            "schema": {"const": contract["error_response_schema"]},
            "error_code": error_code,
            "retryable": {"type": "boolean"},
        }),
    }


def build_document(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Build the only accepted origin-free document for a closed contract."""
    required = {
        "schema", "version", "auth_request_schema", "principal_schema",
        "request_schema", "rearm_response_schema", "receipt_schema",
        "status_response_schema", "status_schema", "error_response_schema",
        "openapi_response_schema", "states", "max_request_bytes",
        "max_response_bytes", "max_attempts", "operations",
    }
    if not isinstance(contract, Mapping) or set(contract) != required:
        raise ValueError("watcher control OpenAPI contract is invalid")
    if (
        contract["openapi_response_schema"] != RESPONSE_SCHEMA
        or not isinstance(contract["version"], str)
        or not contract["version"]
        or not isinstance(contract["max_request_bytes"], int)
        or not isinstance(contract["max_response_bytes"], int)
        or not 1 <= contract["max_request_bytes"] < contract["max_response_bytes"]
        or not isinstance(contract["max_attempts"], int)
        or contract["max_attempts"] < 1
        or tuple(contract["states"]) != (
            "pending", "leased", "retry_wait", "succeeded", "failed",
        )
    ):
        raise ValueError("watcher control OpenAPI values are invalid")
    operations = contract["operations"]
    if not isinstance(operations, Mapping) or set(operations) != {
        "openapi", "rearm", "status",
    }:
        raise ValueError("watcher control OpenAPI operations are invalid")
    paths: dict[str, Any] = {}
    for name in sorted(operations):
        operation = operations[name]
        expected = {
            "name", "method", "path", "scope", "operation_id", "summary",
            "auth_request_schema", "principal_schema", "success_status",
            "response_component", "request_component", "error_statuses",
        }
        if (
            not isinstance(operation, Mapping) or set(operation) != expected
            or operation["name"] != name
            or operation["method"] not in {"GET", "POST"}
            or not isinstance(operation["path"], str)
            or operation["path"] in paths
            or not isinstance(operation["scope"], str)
            or not operation["scope"]
            or not isinstance(operation["error_statuses"], tuple)
            or tuple(sorted(set(operation["error_statuses"])))
            != operation["error_statuses"]
        ):
            raise ValueError("watcher control OpenAPI operation is invalid")
        paths[operation["path"]] = {
            operation["method"].lower(): _operation(operation),
        }
    document = {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "Website Localization Benchmark Watcher Control API",
            "version": contract["version"],
            "description": (
                "Authenticated, content-free watcher status, idempotent failed-"
                "generation recovery, and this origin-free exact contract."
            ),
        },
        "tags": [{"name": "Benchmark watcher control"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "operatorAuthentication": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Example transport only; the host authenticator and exact "
                        "per-operation scope remain authoritative."
                    ),
                },
            },
            "schemas": _schemas(contract),
        },
        "x-schema": DOCUMENT_SCHEMA,
        "x-contract-schema": contract["schema"],
        "x-contract-sha256": document_sha256(contract),
        "x-max-request-bytes": contract["max_request_bytes"],
        "x-max-response-bytes": contract["max_response_bytes"],
        "x-origin-free": True,
        "x-runtime-validation-authoritative": True,
    }
    return json.loads(_canonical(document))
