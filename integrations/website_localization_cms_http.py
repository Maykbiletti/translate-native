#!/usr/bin/env python3
"""Secure provider-neutral HTTP publisher for signed CMS localization bundles."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


MAX_ENDPOINT_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 4096
MAX_RESPONSE_BYTES = 65_536
MAX_REQUEST_BYTES = 4_000_000
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SIGNATURE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+=-]{1,4096}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
RESERVED_HEADERS = {
    "accept", "accept-encoding", "connection", "content-length", "content-type",
    "host", "idempotency-key", "transfer-encoding", "x-localization-delivery-id",
    "x-localization-payload-sha256",
}
PUBLICATION_FIELDS = {
    "schema", "delivery_id", "event_id", "site_id", "website_version", "plan_id",
    "source_id", "source_revision", "source_sequence", "source_sha256", "localizations",
}
TOMBSTONE_FIELDS = {
    "schema", "delivery_id", "tombstone_id", "event_id", "site_id",
    "website_version", "plan_id", "source_id", "source_sequence",
    "publication_delivery_id", "publication_payload_sha256", "locales",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required HTTP publisher dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[1]
_CMS = _load_module(
    "blun_website_localization_http_publisher_cms",
    _ROOT / "integrations" / "website_localization_cms.py",
)
REQUEST_SCHEMA = _CMS.PUBLICATION_HTTP_REQUEST_SCHEMA
RESPONSE_SCHEMA = _CMS.PUBLICATION_HTTP_RESPONSE_SCHEMA
TOMBSTONE_REQUEST_SCHEMA = _CMS.TOMBSTONE_HTTP_REQUEST_SCHEMA
TOMBSTONE_RESPONSE_SCHEMA = _CMS.TOMBSTONE_HTTP_RESPONSE_SCHEMA
RESERVED_HEADERS.update(
    name.lower() for name, _ in _CMS.PUBLICATION_HTTP_BINDING_HEADERS
)


class HTTPPublisherFailed(RuntimeError):
    """Content-free failure understood by the durable CMS outbox."""

    cms_publish_failure = True

    def __init__(self, code: str, *, retryable: bool):
        if not isinstance(code, str) or ERROR_CODE.fullmatch(code) is None:
            raise ValueError("HTTP publisher error code is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("HTTP publisher retryability must be boolean")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HTTPTransport(Protocol):
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
    ) -> HTTPResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class URLTransport:
    """One bounded stdlib HTTP attempt with redirects disabled."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        timeout: float,
    ) -> HTTPResult:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response_headers = tuple(error.headers.items()) if error.headers is not None else ()
            return HTTPResult(int(error.code), response_headers, b"")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise HTTPPublisherFailed("network", retryable=True) from None
        try:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            return HTTPResult(int(response.status), tuple(response.headers.items()), body)
        except (TimeoutError, socket.timeout, OSError):
            raise HTTPPublisherFailed("network", retryable=True) from None
        finally:
            response.close()


def _canonical_json(value: Any, *, code: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise HTTPPublisherFailed(code, retryable=False) from None
    if not encoded or len(encoded) > maximum:
        raise HTTPPublisherFailed(code, retryable=False)
    return encoded


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _endpoint(value: Any, allow_loopback_http: bool) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or not value.isascii()
        or len(value) > MAX_ENDPOINT_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint is invalid") from None
    if (
        not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ValueError("endpoint is invalid")
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and allow_loopback_http
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        pass
    else:
        raise ValueError("endpoint must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint is invalid")
    return value


def _authentication_headers(provider: Callable[[], Mapping[str, str]]) -> dict[str, str]:
    try:
        supplied = provider()
    except Exception:
        raise HTTPPublisherFailed("authentication", retryable=False) from None
    if not isinstance(supplied, Mapping) or not supplied:
        raise HTTPPublisherFailed("authentication", retryable=False)
    result: dict[str, str] = {}
    normalized_names: set[str] = set()
    for name, value in supplied.items():
        normalized = name.lower() if isinstance(name, str) else ""
        if (
            not isinstance(name, str)
            or HEADER_NAME.fullmatch(name) is None
            or normalized in RESERVED_HEADERS
            or normalized in normalized_names
        ):
            raise HTTPPublisherFailed("authentication", retryable=False)
        if (
            not isinstance(value, str)
            or not value
            or not value.isascii()
            or len(value) > MAX_HEADER_VALUE_LENGTH
            or "\r" in value
            or "\n" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise HTTPPublisherFailed("authentication", retryable=False)
        normalized_names.add(normalized)
        result[name] = value
    return result


def _signature(value: Any) -> tuple[_CMS.CMSMessageSignature, dict[str, str]]:
    try:
        algorithm = value.algorithm
        key_id = value.key_id
        signature = value.signature
    except Exception:
        raise HTTPPublisherFailed("request_invalid", retryable=False) from None
    if (
        not isinstance(algorithm, str)
        or TOKEN.fullmatch(algorithm) is None
        or not isinstance(key_id, str)
        or TOKEN.fullmatch(key_id) is None
        or not isinstance(signature, str)
        or SIGNATURE_VALUE.fullmatch(signature) is None
    ):
        raise HTTPPublisherFailed("request_invalid", retryable=False)
    return (
        _CMS.CMSMessageSignature(algorithm, key_id, signature),
        {"algorithm": algorithm, "key_id": key_id, "signature": signature},
    )


def _response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise HTTPPublisherFailed("transport_invalid", retryable=True)
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise HTTPPublisherFailed("transport_invalid", retryable=True)
        name, content = item[0].lower(), item[1].strip()
        if name in {"content-type", "content-length"}:
            if name in result:
                raise HTTPPublisherFailed("response_headers", retryable=True)
            result[name] = content
    return result


class HTTPPublisherAdapter:
    """Publish one exact signed outbox bundle and verify one signed CMS acknowledgement."""

    def __init__(
        self,
        endpoint: str,
        authentication_headers: Callable[[], Mapping[str, str]],
        acknowledgement_verifier: Any,
        *,
        transport: HTTPTransport | None = None,
        timeout: float = 30.0,
        allow_loopback_http: bool = False,
    ):
        if not isinstance(allow_loopback_http, bool):
            raise TypeError("allow_loopback_http must be boolean")
        self.endpoint = _endpoint(endpoint, allow_loopback_http)
        if not callable(authentication_headers):
            raise TypeError("authentication_headers must be callable")
        if not callable(getattr(acknowledgement_verifier, "verify", None)):
            raise TypeError("acknowledgement_verifier must provide verify")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 300
        ):
            raise ValueError("timeout is outside the supported range")
        self.authentication_headers = authentication_headers
        self.acknowledgement_verifier = acknowledgement_verifier
        self.transport = URLTransport() if transport is None else transport
        if not callable(getattr(self.transport, "post", None)):
            raise TypeError("transport must provide post")
        self.timeout = float(timeout)

    def publish(self, request: Any) -> Mapping[str, Any]:
        try:
            delivery_id = request.delivery_id
            payload = request.payload
            payload_sha256 = request.payload_sha256
            raw_signature = request.signature
        except Exception:
            raise HTTPPublisherFailed("request_invalid", retryable=False) from None
        tombstone = isinstance(payload, dict) and payload.get("schema") == _CMS.TOMBSTONE_DELIVERY_SCHEMA
        expected_fields = TOMBSTONE_FIELDS if tombstone else PUBLICATION_FIELDS
        if (
            not isinstance(delivery_id, str)
            or TOKEN.fullmatch(delivery_id) is None
            or not isinstance(payload_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", payload_sha256) is None
            or not isinstance(payload, dict)
            or set(payload) != expected_fields
            or payload.get("schema") not in {
                _CMS.PUBLICATION_SCHEMA, _CMS.TOMBSTONE_DELIVERY_SCHEMA,
            }
            or payload.get("delivery_id") != delivery_id
            or isinstance(payload.get("source_sequence"), bool)
            or not isinstance(payload.get("source_sequence"), int)
            or payload["source_sequence"] <= 0
            or not isinstance(
                payload.get("locales") if tombstone else payload.get("localizations"),
                list,
            )
            or not (payload.get("locales") if tombstone else payload.get("localizations"))
        ):
            raise HTTPPublisherFailed("request_invalid", retryable=False)
        if tombstone and (
            payload["locales"] != sorted(set(payload["locales"]))
            or any(
                not isinstance(locale, str) or TOKEN.fullmatch(locale) is None
                for locale in payload["locales"]
            )
            or not isinstance(payload.get("publication_payload_sha256"), str)
            or re.fullmatch(
                r"[a-f0-9]{64}", payload["publication_payload_sha256"],
            ) is None
        ):
            raise HTTPPublisherFailed("request_invalid", retryable=False)
        payload_bytes = _canonical_json(
            payload,
            code="request_invalid",
            maximum=MAX_REQUEST_BYTES,
        )
        if hashlib.sha256(payload_bytes).hexdigest() != payload_sha256:
            raise HTTPPublisherFailed("request_binding", retryable=False)
        _, signature_payload = _signature(raw_signature)
        body = _canonical_json(
            {
                "schema": TOMBSTONE_REQUEST_SCHEMA if tombstone else REQUEST_SCHEMA,
                "payload_sha256": payload_sha256,
                "tombstone" if tombstone else "publication": payload,
                "signature": signature_payload,
            },
            code="request_invalid",
            maximum=MAX_REQUEST_BYTES,
        )
        headers = _authentication_headers(self.authentication_headers)
        binding_values = {
            "delivery_id": delivery_id,
            "payload_sha256": payload_sha256,
        }
        try:
            protocol_headers = {
                name: binding_values[binding]
                for name, binding in _CMS.PUBLICATION_HTTP_BINDING_HEADERS
            }
        except (KeyError, TypeError, ValueError):
            raise HTTPPublisherFailed("request_invalid", retryable=False) from None
        headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            **protocol_headers,
        })
        try:
            result = self.transport.post(
                self.endpoint,
                headers,
                body,
                timeout=self.timeout,
            )
        except HTTPPublisherFailed:
            raise
        except Exception:
            raise HTTPPublisherFailed("network", retryable=True) from None
        if (
            not isinstance(result, HTTPResult)
            or isinstance(result.status, bool)
            or not isinstance(result.status, int)
            or not 100 <= result.status <= 599
        ):
            raise HTTPPublisherFailed("transport_invalid", retryable=True)
        if result.status != 200:
            if 300 <= result.status <= 399:
                raise HTTPPublisherFailed("redirect", retryable=False)
            retryable = result.status in {408, 425, 429} or 500 <= result.status <= 599
            raise HTTPPublisherFailed("http_status", retryable=retryable)
        response_headers = _response_headers(result.headers)
        content_type = response_headers.get("content-type", "").lower().replace(" ", "")
        if content_type not in {"application/json", "application/json;charset=utf-8"}:
            raise HTTPPublisherFailed("response_content_type", retryable=True)
        if (
            not isinstance(result.body, bytes)
            or not result.body
            or len(result.body) > MAX_RESPONSE_BYTES
        ):
            raise HTTPPublisherFailed("response_size", retryable=True)
        declared = response_headers.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdecimal()
            or int(declared) != len(result.body)
        ):
            raise HTTPPublisherFailed("response_size", retryable=True)
        try:
            text = result.body.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("BOM rejected")
            envelope = json.loads(
                text,
                object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise HTTPPublisherFailed("response_json", retryable=True) from None
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema", "acknowledgement", "signature",
        }:
            raise HTTPPublisherFailed("acknowledgement_invalid", retryable=False)
        acknowledgement = envelope["acknowledgement"]
        expected = {
            "schema": _CMS.TOMBSTONE_ACK_SCHEMA if tombstone else _CMS.ACK_SCHEMA,
            "delivery_id": delivery_id,
            "payload_sha256": payload_sha256,
            "status": "deleted" if tombstone else "accepted",
        }
        expected_response_schema = TOMBSTONE_RESPONSE_SCHEMA if tombstone else RESPONSE_SCHEMA
        if envelope["schema"] != expected_response_schema or acknowledgement != expected:
            raise HTTPPublisherFailed("acknowledgement_binding", retryable=False)
        signature_mapping = envelope["signature"]
        if not isinstance(signature_mapping, dict) or set(signature_mapping) != {
            "algorithm", "key_id", "signature",
        }:
            raise HTTPPublisherFailed("acknowledgement_invalid", retryable=False)
        try:
            acknowledgement_signature, _ = _signature(type("Signature", (), signature_mapping)())
        except HTTPPublisherFailed:
            raise HTTPPublisherFailed("acknowledgement_invalid", retryable=False) from None
        acknowledgement_bytes = _canonical_json(
            acknowledgement,
            code="acknowledgement_invalid",
            maximum=MAX_RESPONSE_BYTES,
        )
        try:
            verified = self.acknowledgement_verifier.verify(
                acknowledgement_bytes,
                acknowledgement_signature,
            ) is True
        except Exception:
            verified = False
        if not verified:
            raise HTTPPublisherFailed("acknowledgement_signature", retryable=False)
        return acknowledgement
