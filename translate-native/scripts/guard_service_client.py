#!/usr/bin/env python3
"""Zero-dependency client for the isolated BLUN Language Guard service."""

from __future__ import annotations

import json
import socket
from typing import Any


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GuardServiceError(RuntimeError):
    """Raised when the isolated guard cannot evaluate a request safely."""


def parse_endpoint(endpoint: str) -> tuple[str, str | tuple[str, int]]:
    value = str(endpoint or "").strip()
    if value.startswith("unix:") and value[5:]:
        return "unix", value[5:]
    if value.startswith("tcp:"):
        host_port = value[4:]
        host, separator, raw_port = host_port.rpartition(":")
        if separator and host in {"127.0.0.1", "localhost", "::1"}:
            try:
                port = int(raw_port)
            except ValueError as error:
                raise GuardServiceError("invalid guard service TCP port") from error
            if 1 <= port <= 65535:
                return "tcp", (host, port)
    raise GuardServiceError("guard service endpoint must be unix:/path or loopback tcp:host:port")


def call_guard_service(
    endpoint: str,
    request: dict[str, Any],
    *,
    auth_token: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    transport, address = parse_endpoint(endpoint)
    payload = dict(request)
    if auth_token:
        payload["service_token"] = auth_token
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise GuardServiceError("guard service request is too large")
    try:
        if transport == "unix":
            if not hasattr(socket, "AF_UNIX"):
                raise GuardServiceError("Unix sockets are unavailable on this platform")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            client = socket.create_connection(address, timeout=timeout)
        with client:
            client.settimeout(timeout)
            if transport == "unix":
                client.connect(address)
            client.sendall(encoded)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise GuardServiceError("guard service response is too large")
                if b"\n" in chunk:
                    break
    except (OSError, TimeoutError) as error:
        raise GuardServiceError("guard service is unavailable") from error
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GuardServiceError("guard service returned an invalid response") from error
    if not isinstance(response, dict):
        raise GuardServiceError("guard service response must be an object")
    return response
