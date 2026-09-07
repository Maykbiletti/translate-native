from __future__ import annotations

import hashlib
import hmac
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = load(
    "blun_test_website_localization_runtime",
    ROOT / "integrations" / "website_localization_runtime.py",
)
CMS = RUNTIME._CMS
RELEASE = RUNTIME._RELEASE
WORKER = RUNTIME._SERVICE._RUNNER._WORKER
COORDINATOR = RUNTIME._COORDINATOR


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class Authority:
    def __init__(self, signature_type, key, key_id):
        self.signature_type = signature_type
        self.key = key
        self.key_id = key_id

    def sign(self, payload):
        return self.signature_type(
            "hmac-sha256-test", self.key_id,
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        expected = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.signature, expected)


class Provider:
    def invoke(self, request):
        if request.phase == "transcreation":
            return {
                "schema": WORKER.CANDIDATE_SCHEMA,
                "phase": request.phase,
                "locale": request.input["target"]["locale"],
                "candidate": "Kasvata yritystäsi turvallisesti.",
            }
        return {
            "schema": WORKER.REVIEW_SCHEMA,
            "phase": request.phase,
            "locale": request.input["target"]["locale"],
            "status": "PASS",
            "blocking_defects": [],
            "major_defects": [],
        }


def receipt(request):
    value = "\x1f".join((
        request.source_text, request.target_text, request.target_locale,
        request.request_id,
    ))
    return "quality:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class Evidence:
    def __init__(self):
        self.requests = []

    def obtain(self, request):
        self.requests.append(request)
        return {
            "schema": COORDINATOR.EVIDENCE_RESPONSE_SCHEMA,
            "request_id": request.request_id,
            "result_sha256": request.result_sha256,
            "quality_receipt": receipt(request),
            "human_review_receipt": None,
        }


class QualityVerifier:
    def __init__(self, evidence):
        self.evidence = evidence

    def verify(self, **values):
        return any(values["receipt"] == receipt(request)
                   for request in self.evidence.requests)


class Publisher:
    def __init__(self):
        self.requests = []

    def publish(self, request):
        self.requests.append(request)
        return {
            "schema": CMS.ACK_SCHEMA,
            "delivery_id": request.delivery_id,
            "payload_sha256": request.payload_sha256,
            "status": "accepted",
        }


class ProviderProbe:
    def check(self, **provider):
        return {
            "schema": RUNTIME._HEALTH.PROVIDER_HEALTH_SCHEMA,
            "provider": {
                "id": provider["provider_id"],
                "model_id": provider["model_id"],
                "model_version": provider["model_version"],
            },
            "status": "healthy",
        }


class WebsiteLocalizationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.connections = [sqlite3.connect(":memory:") for _ in range(5)]
        self.clock = Clock()
        self.event_authority = Authority(
            CMS.CMSMessageSignature, b"event-key", "event-key",
        )
        self.publication_authority = Authority(
            CMS.CMSMessageSignature, b"publication-key", "publication-key",
        )
        self.approval_authority = Authority(
            RELEASE.ApprovalSignature, b"approval-key", "approval-key",
        )
        self.provider = Provider()
        self.evidence = Evidence()
        self.publisher = Publisher()
        self.dependencies = {
            "provider_resolver": lambda payload: self.provider,
            "assets_resolver": self.assets,
            "evidence_provider": self.evidence,
            "quality_verifier": QualityVerifier(self.evidence),
            "event_verifier": self.event_authority,
            "approval_authority": self.approval_authority,
            "publication_authority": self.publication_authority,
            "publisher": self.publisher,
            "translation_worker_id": "translation-worker",
            "evidence_worker_id": "evidence-worker",
            "delivery_worker_id": "delivery-worker",
            "evidence_revision": "quality-evidence-1",
            "approval_ttl_seconds": 1000,
        }

    def tearDown(self):
        for connection in self.connections:
            connection.close()

    def assets(self, payload):
        return WORKER.LocalizationAssets(
            glossary_version=payload["glossary_version"],
            policy_version=payload["policy_version"],
            audience="Finnish business owners",
            tone_profile="Natural, concise, and precise Finnish",
            protected_terms=(),
        )

    def runtime(self, **overrides):
        values = {
            "queue_connection": self.connections[0],
            "release_connection": self.connections[1],
            "cms_connection": self.connections[2],
            "evidence_connection": self.connections[3],
            "supervisor_connection": self.connections[4],
            "dependencies": self.dependencies,
            "supervisor_worker_id": "runtime-worker",
            "supervisor_policy": {
                "lease_seconds": 10,
                "active_delay_seconds": 1,
                "idle_delay_seconds": 5,
                "blocked_base_seconds": 2,
                "blocked_max_seconds": 20,
                "stop_poll_seconds": 1,
            },
            "clock": self.clock,
            "token_factory": lambda: "runtime-lease",
        }
        values.update(overrides)
        return RUNTIME.WebsiteLocalizationRuntime(**values)

    @staticmethod
    def event():
        return {
            "schema": CMS.CHANGE_SCHEMA,
            "event_id": "event-1",
            "site_id": "public-site",
            "website_version": "version-1",
            "source_sequence": 1,
            "localization": {
                "source_id": "homepage.hero",
                "source_revision": "revision-1",
                "source_text": "Grow your business safely.",
                "source_locale": "en-IE",
                "content_type": "headline",
                "glossary_version": "glossary-1",
                "policy_version": "native-web-1",
                "provider_id": "customer-llm",
                "model_id": "configured-model",
                "model_version": "2026-09-07",
                "software_version": "6.43.0-dev",
                "target_locales": ["fi-FI"],
            },
        }

    def test_one_runtime_reaches_cms_and_reports_same_state_healthy(self):
        runtime = self.runtime()
        event = self.event()
        signature = self.event_authority.sign(
            CMS._canonical_json(event).encode("utf-8"),
        )
        runtime.bridge.ingest_change(
            event, signature, self.event_authority, now=self.clock(),
        )

        phases = []
        for now in (100, 101, 102):
            self.clock.value = now
            outcome = runtime.run_once(now=now)
            phases.append((outcome.tick["phase"], outcome.tick["status"]))

        self.assertEqual(phases, [
            ("translation", "succeeded"),
            ("release", "delivery_ready"),
            ("delivery", "succeeded"),
        ])
        self.assertEqual(len(self.publisher.requests), 1)
        self.clock.value = 103
        report = runtime.health(provider_probe=ProviderProbe(), now=103)
        self.assertEqual(report.status, "healthy")
        self.assertEqual(report.website_versions[0].status, "published")
        self.assertEqual(
            next(item for item in report.components
                 if item.component == "supervisor").status,
            "healthy",
        )

    def test_preflight_rejects_duplicate_connection_without_schema_writes(self):
        duplicate = self.connections[0]
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked, "runtime.connections.not_distinct"
        ):
            self.runtime(supervisor_connection=duplicate)

        after = tuple(connection.total_changes for connection in self.connections)
        self.assertEqual(before, after)

    def test_preflight_rejects_missing_capability_without_schema_writes(self):
        self.dependencies["publisher"] = object()
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked, "runtime.publisher.invalid"
        ):
            self.runtime()

        after = tuple(connection.total_changes for connection in self.connections)
        self.assertEqual(before, after)

    def test_preflight_rejects_out_of_range_retry_without_schema_writes(self):
        self.dependencies["translation_retry_max_seconds"] = 86_401
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.translation_retry_max_seconds.invalid",
        ):
            self.runtime()

        after = tuple(connection.total_changes for connection in self.connections)
        self.assertEqual(before, after)

    def test_dependencies_are_copied_and_runtime_repr_is_secret_free(self):
        runtime = self.runtime()
        self.dependencies["publisher"] = object()

        self.assertIs(runtime._dependencies["publisher"], self.publisher)
        self.assertEqual(
            repr(runtime),
            "<WebsiteLocalizationRuntime schema=blun.website-localization-runtime.v1>",
        )


if __name__ == "__main__":
    unittest.main()
