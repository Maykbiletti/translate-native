from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from tests import test_website_localization_cms_client as cms_support
from tests import test_website_localization_cms_source_delivery as delivery_support
from tests import test_website_localization_cms_source_delivery_http as http_support


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLIENT = load(
    "blun_test_website_localization_cms_source_delivery_client",
    ROOT / "integrations" / "website_localization_cms_source_delivery_client.py",
)
RUNTIME = http_support.RUNTIME
HTTP = http_support.HTTP


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


class StaticTransport:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.result


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


class SourceDeliveryClientTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "delivery.sqlite3"
        self.remote_client = delivery_support.ScriptedClient()
        self.authenticator = http_support.Authenticator()
        self.runtime = RUNTIME.open_hosted_cms_source_delivery(
            self.database,
            self.remote_client,
            worker_id="website-source-worker",
            clock=lambda: self.now,
            lease_seconds=60,
            active_delay_seconds=10,
            idle_delay_seconds=10,
            blocked_delay_seconds=10,
            http_authenticator=self.authenticator,
        )
        self.transport = WSGITransport(self.runtime.http)
        self.contexts = []
        self.digest = HTTP._capabilities_payload()["sha256"]
        self.remote_digest = self.remote_client.expected_capabilities_sha256
        self.client = self.make_client()

    def tearDown(self):
        self.runtime._owner_pid = os.getpid()
        if self.database.exists() and not self.database.is_symlink():
            os.chmod(self.database, 0o600)
        try:
            self.runtime.close(worker_timeout_seconds=1)
        except Exception:
            pass
        self.directory.cleanup()

    def make_client(
        self, *, transport=None, digest=None, remote_digest=None, headers=None,
    ):
        def authentication(context):
            self.contexts.append(copy.deepcopy(context))
            if headers is not None:
                return headers
            return {"Authorization": "Bearer source-delivery-client-test"}

        return CLIENT.CMSSourceDeliverySidecarHTTPClient(
            "https://delivery.example",
            self.digest if digest is None else digest,
            self.remote_digest if remote_digest is None else remote_digest,
            authentication,
            transport=self.transport if transport is None else transport,
        )

    @staticmethod
    def payload_hash(payload):
        return hashlib.sha256(CLIENT._canonical(payload)).hexdigest()

    def test_capabilities_are_fresh_exact_and_pinned(self):
        response = self.client.capabilities()

        self.assertEqual(response["capabilities"]["sha256"], self.digest)
        self.assertEqual(set(response["capabilities"]["operations"]), {
            "capabilities", "change", "health", "readiness", "removal",
            "source_readiness", "source_status", "status",
        })
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.contexts[0], {
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "GET",
            "origin": "https://delivery.example",
            "path": HTTP.CAPABILITIES_PATH,
            "scope": HTTP.SCOPES[HTTP.CAPABILITIES_PATH],
            "body_sha256": hashlib.sha256(b"").hexdigest(),
        })

    def test_changes_removals_and_replays_use_exact_inner_payload_binding(self):
        change = cms_support.event()
        cancellation = cms_support.cancellation(change)
        tombstone = cms_support.tombstone(change)

        accepted = self.client.submit_change(
            change, source_max_attempts=4, delivery_max_attempts=3,
        )
        replayed = self.client.submit_change(
            change, source_max_attempts=4, delivery_max_attempts=3,
        )
        cancelled = self.client.submit_removal(cancellation)
        deleted = self.client.submit_removal(tombstone)

        self.assertEqual(accepted, replayed)
        self.assertEqual(accepted["queue"]["operation"], "change")
        self.assertEqual(cancelled["queue"]["operation"], "cancellation")
        self.assertEqual(deleted["queue"]["operation"], "tombstone")
        writes = [call for call in self.transport.calls if call[0] == "POST"]
        self.assertEqual(len(writes), 4)
        payloads = (change, change, cancellation, tombstone)
        for call, payload in zip(writes, payloads):
            _method, _url, headers, body, _timeout = call
            request_id = payload.get(
                "cancellation_id", payload.get("tombstone_id", payload["event_id"]),
            )
            self.assertEqual(headers["Idempotency-Key"], request_id)
            self.assertEqual(
                headers["X-Localization-Source-Payload-Sha256"],
                self.payload_hash(payload),
            )
            self.assertNotEqual(
                headers["X-Localization-Source-Payload-Sha256"],
                hashlib.sha256(body).hexdigest(),
            )

    def test_status_requires_and_validates_complete_known_binding(self):
        change = cms_support.event()
        accepted = self.client.submit_change(change)
        queue = accepted["queue"]

        status = self.client.status(
            queue["operation"], queue["request_id"], queue["event_id"],
            queue["site_id"], queue["payload_sha256"],
        )

        self.assertEqual(status["status"]["request_id"], change["event_id"])
        self.assertEqual(status["capabilities_sha256"], self.digest)
        source = change["localization"]["source_text"]
        self.assertNotIn(source, json.dumps(status))
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            self.client.status(
                "change", change["event_id"], "wrong-event", change["site_id"],
                queue["payload_sha256"],
            )
        self.assertEqual(caught.exception.code, "source_delivery_client.status_binding")

    def test_source_status_waits_for_acceptance_and_validates_full_lifecycle(self):
        change = cms_support.event()
        queued = self.client.submit_change(change)["queue"]

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            self.client.source_status(
                change["event_id"], change["site_id"],
                queued["payload_sha256"],
            )
        self.assertEqual(caught.exception.code, "source_delivery_client.http_status")
        self.assertFalse(caught.exception.retryable)

        self.runtime.run_once()
        response = self.client.source_status(
            change["event_id"], change["site_id"], queued["payload_sha256"],
        )

        self.assertEqual(response["source_status"]["remote_status"], "processing")
        self.assertEqual(response["source_status"]["required_locales"], [
            "fi-FI", "mt-MT",
        ])
        self.assertEqual(
            response["source_capabilities_sha256"], self.remote_digest,
        )
        context = self.contexts[-1]
        self.assertEqual(context["path"], HTTP.SOURCE_STATUS_PATH)
        self.assertEqual(context["event_id"], change["event_id"])
        self.assertEqual(context["payload_sha256"], queued["payload_sha256"])
        self.assertNotIn(
            change["localization"]["source_text"], json.dumps(response),
        )

        def substitute_event(call_number, result):
            if call_number == 2:
                return replace_json(
                    result,
                    lambda value: value["source_status"].update(
                        event_id="other-event"
                    ),
                )
            return result

        tampering_transport = TransformingTransport(
            WSGITransport(self.runtime.http), substitute_event,
        )
        tampered_client = self.make_client(transport=tampering_transport)
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            tampered_client.source_status(
                change["event_id"], change["site_id"],
                queued["payload_sha256"],
            )
        self.assertEqual(
            caught.exception.code,
            "source_delivery_client.source_status_binding",
        )

    def test_health_and_readiness_accept_only_consistent_blocked_state(self):
        self.assertEqual(self.client.health()["health"]["status"], "ok")
        self.assertEqual(self.client.readiness()["readiness"]["status"], "ready")

        self.runtime.stop_worker()
        readiness = self.client.readiness()

        self.assertEqual(readiness["readiness"]["status"], "not_ready")
        self.assertEqual(readiness["readiness"]["worker_state"], "stopped")
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            self.client.submit_change(cms_support.event())
        self.assertEqual(caught.exception.code, "source_delivery_client.http_status")
        self.assertTrue(caught.exception.retryable)

    def test_source_readiness_is_separate_and_validates_both_contracts(self):
        response = self.client.source_readiness()

        self.assertEqual(response["source_readiness"]["status"], "ready")
        self.assertEqual(
            response["source_capabilities_sha256"], self.remote_digest,
        )
        self.assertEqual(self.contexts[-1], {
            "schema": CLIENT.AUTH_CONTEXT_SCHEMA,
            "method": "GET",
            "origin": "https://delivery.example",
            "path": HTTP.SOURCE_READINESS_PATH,
            "scope": HTTP.SCOPES[HTTP.SOURCE_READINESS_PATH],
            "body_sha256": hashlib.sha256(b"").hexdigest(),
        })

        def alter_source_state(call_number, result):
            if call_number == 2:
                return replace_json(
                    result,
                    lambda value: value["source_readiness"].update(
                        worker_state="stopped"
                    ),
                )
            return result

        tampered = self.make_client(transport=TransformingTransport(
            WSGITransport(self.runtime.http), alter_source_state,
        ))
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            tampered.source_readiness()
        self.assertEqual(
            caught.exception.code,
            "source_delivery_client.source_readiness_binding",
        )

    def test_invalid_input_and_configuration_block_before_network(self):
        with self.assertRaises(ValueError):
            CLIENT.CMSSourceDeliverySidecarHTTPClient(
                "http://delivery.example", self.digest, self.remote_digest,
                lambda _context: {"Authorization": "x"}, transport=self.transport,
            )
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked):
            self.client.submit_change({"schema": "wrong"})
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked):
            self.client.submit_change(cms_support.event(), source_max_attempts=True)
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked):
            self.client.status("other", "request", "event", "site-1", "0" * 64)
        self.assertEqual(self.transport.calls, [])

    def test_sidecar_contract_drift_blocks_before_mutation(self):
        client = self.make_client(digest="0" * 64)

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            client.submit_change(cms_support.event())

        self.assertEqual(
            caught.exception.code, "source_delivery_client.capabilities_binding",
        )
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(len(self.transport.calls), 1)

    def test_remote_contract_mismatch_never_returns_false_acceptance(self):
        client = self.make_client(remote_digest="0" * 64)

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            client.submit_change(cms_support.event())

        self.assertEqual(caught.exception.code, "source_delivery_client.enqueue_binding")
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_tampered_response_bindings_are_rejected(self):
        changes = (
            lambda value: value.update(capabilities_sha256="0" * 64),
            lambda value: value["queue"].update(site_id="site-2"),
            lambda value: value["queue"].update(payload_sha256="0" * 64),
            lambda value: value["queue"].update(source_max_attempts=6),
        )
        for transform in changes:
            with self.subTest(transform=transform):
                def alter(index, result):
                    return replace_json(result, transform) if index == 2 else result

                client = self.make_client(
                    transport=TransformingTransport(self.transport, alter),
                )
                with self.assertRaises(
                    CLIENT.CMSSourceDeliveryClientBlocked
                ) as caught:
                    client.submit_change(cms_support.event())
                self.assertEqual(
                    caught.exception.code,
                    "source_delivery_client.enqueue_binding",
                )

    def test_authentication_cannot_replace_reserved_binding_headers(self):
        client = self.make_client(headers={
            "Authorization": "Bearer x",
            "Idempotency-Key": "forged",
        })

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as caught:
            client.submit_change(cms_support.event())

        self.assertEqual(caught.exception.code, "source_delivery_client.authentication")
        self.assertEqual(self.transport.calls, [])

    def test_redirect_and_network_failure_are_single_attempt_and_classified(self):
        redirect = CLIENT.HTTPResult(
            307,
            (("Content-Type", "application/json"),),
            b'{"redirect":true}',
        )
        cases = (
            (StaticTransport(result=redirect), "redirect", False),
            (StaticTransport(error=TimeoutError("private")), "network", True),
        )
        for transport, suffix, retryable in cases:
            with self.subTest(suffix=suffix):
                client = self.make_client(transport=transport)
                with self.assertRaises(
                    CLIENT.CMSSourceDeliveryClientBlocked
                ) as caught:
                    client.capabilities()
                self.assertEqual(
                    caught.exception.code, "source_delivery_client." + suffix,
                )
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("private", str(caught.exception))

    def test_cross_site_status_is_indistinguishable_and_content_free(self):
        change = cms_support.event()
        queue = self.client.submit_change(change)["queue"]
        self.authenticator.site_id = "site-2"

        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as foreign:
            self.client.status(
                queue["operation"], queue["request_id"], queue["event_id"],
                queue["site_id"], queue["payload_sha256"],
            )
        with self.assertRaises(CLIENT.CMSSourceDeliveryClientBlocked) as missing:
            self.client.status(
                "change", "missing", "missing", "site-1", "0" * 64,
            )

        self.assertEqual(foreign.exception.code, missing.exception.code)
        self.assertEqual(foreign.exception.code, "source_delivery_client.http_status")
        self.assertFalse(foreign.exception.retryable)
        self.assertNotIn(change["localization"]["source_text"], str(foreign.exception))

    def test_parallel_exact_submissions_converge_on_one_outbox_row(self):
        change = cms_support.event()
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(
                lambda _index: self.client.submit_change(change), range(16),
            ))

        self.assertTrue(all(response == responses[0] for response in responses))
        count = self.runtime._connection.execute(
            "SELECT COUNT(*) FROM cms_source_delivery_outbox"
        ).fetchone()[0]
        self.assertEqual(count, 1)
