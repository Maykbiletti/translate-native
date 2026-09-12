from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from tests import test_website_localization_cms_client as cms_support
from tests import test_website_localization_cms_source_http as source_support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLIENT = load(
    "blun_test_website_localization_cms_source_client",
    ROOT / "integrations" / "website_localization_cms_source_client.py",
)
HTTP = source_support.HTTP
RUNTIME = source_support.RUNTIME


class WSGITransport:
    def __init__(self, application):
        self.application = application
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        parsed = urlsplit(url)
        raw = b"" if body is None else body
        environ = {
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": method,
            "wsgi.url_scheme": parsed.scheme,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        for name, value in headers.items():
            normalized = name.upper().replace("-", "_")
            if normalized == "CONTENT_TYPE":
                environ[normalized] = value
            elif normalized != "CONTENT_LENGTH":
                environ["HTTP_" + normalized] = value
        captured = {}
        response_body = b"".join(self.application(
            environ,
            lambda status, response_headers: captured.update(
                status=status, headers=tuple(response_headers),
            ),
        ))
        return CLIENT.HTTPResult(
            int(captured["status"].split(" ", 1)[0]),
            captured["headers"],
            response_body,
        )


class TransformingTransport:
    def __init__(self, transport, transform):
        self.transport = transport
        self.transform = transform
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        result = self.transport.request(
            method, url, headers, body, timeout=timeout,
        )
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.transform(len(self.calls), result)


def replace_json(result, transform):
    value = json.loads(result.body)
    transform(value)
    body = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = tuple(
        (name, str(len(body)) if name.lower() == "content-length" else content)
        for name, content in result.headers
    )
    return CLIENT.HTTPResult(result.status, headers, body)


class SourceClientTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.remote_client = source_support.ScriptedClient()
        self.authenticator = source_support.Authenticator()
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.runtime = RUNTIME.open_durable_cms_source(
            root / "changes.sqlite3",
            root / "removals.sqlite3",
            root / "lifecycle.sqlite3",
            self.remote_client,
            change_worker_id="source-change-worker",
            removal_worker_id="source-removal-worker",
            lifecycle_worker_id="source-lifecycle-worker",
            http_authenticator=self.authenticator,
            clock=lambda: self.now,
            change_lease_seconds=60,
            removal_lease_seconds=60,
            lifecycle_lease_seconds=60,
            lifecycle_poll_interval_seconds=30,
            capability_preflight=True,
        )
        self.transport = WSGITransport(self.runtime.http)
        self.auth_contexts = []
        self.digest = HTTP._capabilities_payload()["sha256"]
        binding = self.runtime.capability_binding()
        self.runtime_digest = binding["capabilities_sha256"]
        self.rendering_digest = binding[
            "commercial_rendering_registry_sha256"
        ]
        self.client = self.make_client()

    def tearDown(self):
        if os.getpid() == self.runtime._owner_pid:
            self.runtime.close()
        self.directory.cleanup()

    def make_client(self, *, transport=None, digest=None):
        def headers(context):
            self.auth_contexts.append(copy.deepcopy(context))
            return {"Authorization": "Bearer source-client-test"}

        return CLIENT.CMSLocalizationSourceHTTPClient(
            "https://source.example",
            self.digest if digest is None else digest,
            headers,
            expected_runtime_capabilities_sha256=self.runtime_digest,
            expected_commercial_rendering_registry_sha256=(
                self.rendering_digest
            ),
            transport=self.transport if transport is None else transport,
        )

    def test_change_and_removal_are_contract_and_payload_bound(self):
        change = cms_support.event()
        accepted = self.client.submit_change(change, max_attempts=4)
        replayed = self.client.submit_change(change, max_attempts=4)
        removal = cms_support.cancellation(change)
        removed = self.client.submit_removal(removal, max_attempts=3)

        self.assertEqual(accepted, replayed)
        self.assertEqual((accepted["operation"], accepted["request_id"]), (
            "change", change["event_id"],
        ))
        self.assertEqual((removed["operation"], removed["request_id"]), (
            "cancellation", removal["cancellation_id"],
        ))
        self.assertEqual(accepted["capabilities_sha256"], self.digest)
        self.assertEqual(removed["capabilities_sha256"], self.digest)
        writes = [call for call in self.transport.calls if call[0] == "POST"]
        self.assertEqual(len(writes), 3)
        for _method, _url, headers, body, _timeout in writes:
            self.assertEqual(
                headers["X-Localization-Source-Payload-Sha256"],
                CLIENT.hashlib.sha256(body).hexdigest(),
            )
            self.assertIn("Idempotency-Key", headers)

    def test_status_health_and_readiness_are_content_free_and_bound(self):
        change = cms_support.event()
        source_text = change["localization"]["source_text"]
        self.client.submit_change(change)

        status = self.client.status(change["event_id"], change["site_id"])
        health = self.client.health()
        readiness = self.client.readiness()

        self.assertEqual(status["status"]["dispatch_status"], "pending")
        self.assertEqual(health["health"]["status"], "ok")
        self.assertEqual(readiness["readiness"]["status"], "not_ready")
        self.assertTrue(all(
            value["capabilities_sha256"] == self.digest
            for value in (status, health, readiness)
        ))
        self.assertNotIn(source_text, json.dumps((status, health, readiness)))

    def test_fresh_capability_drift_blocks_before_mutation(self):
        client = self.make_client(digest="0" * 64)
        change = cms_support.event()

        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            client.submit_change(change)

        self.assertEqual(caught.exception.code, "source_client.capabilities_binding")
        self.assertFalse(caught.exception.retryable)
        count = self.runtime._service.changes.connection.execute(
            "SELECT COUNT(*) FROM cms_source_change_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_changed_capability_binding_in_response_blocks(self):
        def transform(index, result):
            if index == 2:
                return replace_json(
                    result,
                    lambda value: value.update(capabilities_sha256="0" * 64),
                )
            return result

        client = self.make_client(
            transport=TransformingTransport(self.transport, transform),
        )
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            client.submit_change(cms_support.event())

        self.assertEqual(caught.exception.code, "source_client.enqueue_binding")

    def test_runtime_binding_replacement_blocks_capabilities_and_writes(self):
        def replace_binding(index, result):
            if index == 1:
                return replace_json(
                    result,
                    lambda value: value["capability_binding"].update(
                        capabilities_sha256="0" * 64,
                    ),
                )
            return result

        replaced = self.make_client(transport=TransformingTransport(
            self.transport, replace_binding,
        ))
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            replaced.capabilities()
        self.assertEqual(
            caught.exception.code, "source_client.runtime_capability_binding",
        )

        def replace_write_binding(index, result):
            if index == 2:
                return replace_json(
                    result,
                    lambda value: value["capability_binding"].update(
                        commercial_rendering_registry_sha256="0" * 64,
                    ),
                )
            return result

        replaced = self.make_client(transport=TransformingTransport(
            self.transport, replace_write_binding,
        ))
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            replaced.submit_change(cms_support.event())
        self.assertEqual(
            caught.exception.code, "source_client.runtime_capability_binding",
        )

    def test_wrong_payload_hash_and_tenant_status_block(self):
        change = cms_support.event()

        def hash_transform(index, result):
            if index == 2:
                return replace_json(
                    result,
                    lambda value: value.update(payload_sha256="0" * 64),
                )
            return result

        bad_hash = self.make_client(
            transport=TransformingTransport(self.transport, hash_transform),
        )
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            bad_hash.submit_change(change)
        self.assertEqual(caught.exception.code, "source_client.enqueue_binding")

        valid = self.client.status(change["event_id"], change["site_id"])
        self.assertEqual(valid["status"]["site_id"], change["site_id"])

        def site_transform(index, result):
            if index == 2:
                return replace_json(
                    result,
                    lambda value: value["status"].update(site_id="other-site"),
                )
            return result

        wrong_site = self.make_client(
            transport=TransformingTransport(self.transport, site_transform),
        )
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            wrong_site.status(change["event_id"], change["site_id"])
        self.assertEqual(caught.exception.code, "source_client.status_binding")

    def test_invalid_request_and_authentication_block_before_network(self):
        calls = len(self.transport.calls)
        invalid = cms_support.event()
        invalid["site_id"] = "not valid"
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            self.client.submit_change(invalid)
        self.assertEqual(caught.exception.code, "source_client.request_invalid")
        self.assertEqual(len(self.transport.calls), calls)

        client = CLIENT.CMSLocalizationSourceHTTPClient(
            "https://source.example",
            self.digest,
            lambda _context: {"Content-Type": "forged"},
            expected_runtime_capabilities_sha256=self.runtime_digest,
            expected_commercial_rendering_registry_sha256=(
                self.rendering_digest
            ),
            transport=self.transport,
        )
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            client.capabilities()
        self.assertEqual(caught.exception.code, "source_client.authentication")

        client = CLIENT.CMSLocalizationSourceHTTPClient(
            "https://source.example",
            self.digest,
            lambda _context: {
                f"X-Auth-{index}": "value"
                for index in range(CLIENT.MAX_AUTHENTICATION_HEADERS + 1)
            },
            expected_runtime_capabilities_sha256=self.runtime_digest,
            expected_commercial_rendering_registry_sha256=(
                self.rendering_digest
            ),
            transport=self.transport,
        )
        with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
            client.capabilities()
        self.assertEqual(caught.exception.code, "source_client.authentication")

    def test_redirect_and_server_failure_have_stable_retryability(self):
        headers = (("Content-Type", "application/json"),)

        class StaticTransport:
            def __init__(self, status):
                self.status = status

            def request(self, *_args, **_kwargs):
                return CLIENT.HTTPResult(self.status, headers, b"{}")

        for status, code, retryable in (
            (307, "source_client.redirect", False),
            (503, "source_client.http_status", True),
        ):
            with self.subTest(status=status):
                client = self.make_client(transport=StaticTransport(status))
                with self.assertRaises(CLIENT.CMSSourceClientBlocked) as caught:
                    client.capabilities()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.retryable, retryable)

    def test_authentication_context_binds_every_request(self):
        change = cms_support.event()
        self.client.submit_change(change)

        capability, submission = self.auth_contexts
        self.assertEqual(capability["path"], HTTP.CAPABILITIES_PATH)
        self.assertEqual(capability["scope"], HTTP.SCOPES[HTTP.CAPABILITIES_PATH])
        self.assertEqual(submission["path"], HTTP.CHANGE_PATH)
        self.assertEqual(submission["event_id"], change["event_id"])
        self.assertEqual(submission["request_id"], change["event_id"])
        self.assertEqual(
            submission["body_sha256"],
            self.transport.calls[-1][2][
                "X-Localization-Source-Payload-Sha256"
            ],
        )


if __name__ == "__main__":
    unittest.main()
