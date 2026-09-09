from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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
            "confidence": "high",
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
            "independent_model_review": None,
        }


class QualityVerifier:
    def __init__(self, evidence):
        self.evidence = evidence

    def verify(self, **values):
        binding = values["binding"]
        return any(
            values["receipt"] == receipt(request)
            and request.job_id == binding["job_id"]
            and request.result_sha256 == binding["result_sha256"]
            for request in self.evidence.requests
        )


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

    @staticmethod
    def benchmark_policy():
        campaign = RUNTIME._HEALTH._CAMPAIGN
        benchmark = campaign._BENCHMARK
        manifest = campaign._SUITE.manifest()
        return benchmark.BenchmarkPolicy(
            benchmark_version="native-vs-baseline-1",
            suite_version=manifest["version"],
            suite_sha256=manifest["sha256"],
            candidate_provider_id="customer-llm",
            candidate_model_id="king",
            candidate_model_version="2026-09-08",
            candidate_software_version="6.43.0-dev",
            candidate_worker_schema=WORKER.WORKER_SCHEMA,
            candidate_glossary_version="customer-glossary-1",
            candidate_policy_version="native-web-2",
            attestation_algorithm="hmac-sha256-test",
            attestation_key_id="benchmark-key",
            baseline_id="deepl-official-api",
            baseline_version="fixture-2026-09-08",
            reviewer_id="independent-native-panel",
            reviewer_version="2026-09-08",
            native_reference_revision="qualified-native-reference-1",
            native_reference_verifier_id="qualified-review-registry",
            native_reference_verifier_version="2026-09-08",
            valid_until=1_800_000_000,
            required_locales=("mt-MT", "fi-FI"),
            required_content_types=benchmark.EU_BENCHMARK_CONTENT_TYPES,
            minimum_cases_per_locale=len(manifest["cases"]),
            minimum_cases_per_content_type=8,
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
                "lease_seconds": 301,
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

    def benchmark_configuration(self, **execution_overrides):
        campaign = RUNTIME._HEALTH._CAMPAIGN
        policy = self.benchmark_policy()
        campaign_id = campaign._campaign_identity(
            campaign._BENCHMARK._validate_policy(policy),
        )[0]
        authority = Authority(
            campaign._BENCHMARK.BenchmarkSignature,
            b"benchmark-key",
            "benchmark-key",
        )
        benchmark_connections = [sqlite3.connect(":memory:") for _ in range(5)]
        self.connections.extend(benchmark_connections)
        reviewer = type("Reviewer", (), {"review": lambda self, request: None})()
        verifier = type("Verifier", (), {"verify": lambda self, **values: True})()
        execution = {
            "candidate_connection": benchmark_connections[1],
            "baseline_connection": benchmark_connections[2],
            "native_reference_connection": benchmark_connections[3],
            "review_connection": benchmark_connections[4],
            "candidate_route_id": "attached-model-primary",
            "baseline_route_id": "official-baseline",
            "native_reference_route_id": "qualified-native-vault",
            "reviewer_route_id": "independent-review-panel",
            "assets_resolver": lambda payload: None,
            "candidate_provider_resolver": lambda payload: None,
            "baseline_acquirer": lambda *values: None,
            "native_reference_loader": lambda payload: None,
            "reviewer": reviewer,
            "native_reference_verifier": verifier,
            "blinding_key": b"benchmark-blinding-key-material-1",
            "worker_id": "benchmark-worker",
            "max_attempts": 3,
            "lease_seconds": 300,
            "retry_base_seconds": 5,
            "retry_max_seconds": 3600,
        }
        execution.update(execution_overrides)
        return {
            "benchmark_connection": benchmark_connections[0],
            "benchmark_policy": policy,
            "benchmark_campaign_id": campaign_id,
            "benchmark_evidence_authority": authority,
            "benchmark_execution": execution,
        }

    def ingest(self, runtime, event=None):
        event = self.event() if event is None else event
        signature = self.event_authority.sign(
            CMS._canonical_json(event).encode("utf-8"),
        )
        runtime.bridge.ingest_change(
            event, signature, self.event_authority, now=self.clock(),
        )

    def seed_approval_then_replace_operational_stores(self):
        runtime = self.runtime()
        self.ingest(runtime)
        for now in (100, 101):
            self.clock.value = now
            self.assertEqual(runtime.run_once(now=now).status, "ran")

        old = self.connections
        self.connections = [
            sqlite3.connect(":memory:"),
            old[1],
            sqlite3.connect(":memory:"),
            sqlite3.connect(":memory:"),
            sqlite3.connect(":memory:"),
        ]
        for index in (0, 2, 3, 4):
            old[index].close()

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
        self.ingest(runtime)

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

    def test_runtime_exposes_authenticated_v2_cms_ingress_and_progress(self):
        runtime = self.runtime(cms_api_max_attempts=4)
        event = self.event()

        def request(path, value):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            signature = self.event_authority.sign(
                RUNTIME._API._canonical_json(value).encode("utf-8"),
            )
            environ = {
                "PATH_INFO": path,
                "QUERY_STRING": "",
                "REQUEST_METHOD": "POST",
                "wsgi.url_scheme": "https",
                "CONTENT_TYPE": "application/json; charset=utf-8",
                "CONTENT_LENGTH": str(len(raw)),
                "wsgi.input": io.BytesIO(raw),
                "HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM": signature.algorithm,
                "HTTP_X_LOCALIZATION_KEY_ID": signature.key_id,
                "HTTP_X_LOCALIZATION_SIGNATURE": signature.signature,
            }
            captured = {}
            body = b"".join(runtime.cms_api(
                environ,
                lambda status, headers: captured.update(
                    status=status, headers=headers,
                ),
            ))
            return captured["status"], json.loads(body)

        capabilities_request = {
            "schema": RUNTIME._API.CAPABILITIES_REQUEST_SCHEMA,
            "request_id": "runtime-capabilities-1",
            "requested_at": self.clock(),
        }
        status, capabilities = request(
            RUNTIME._API.CAPABILITIES_PATH, capabilities_request,
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(capabilities["status"], "CAPABILITIES")
        self.assertEqual(len(capabilities["capabilities"]["locales"]), 24)
        self.assertEqual(
            capabilities["capabilities"]["quality_passes"],
            ["target_native", "source_fidelity"],
        )

        status, accepted = request(RUNTIME._API.CHANGE_PATH, event)
        self.assertEqual(status, "202 Accepted")
        self.assertEqual(accepted["job_count"], 1)
        self.assertEqual(runtime.queue.plan_counts(accepted["plan_id"])["pending"], 1)
        queue_status = runtime.queue.status(
            runtime.queue.connection.execute(
                "SELECT job_id FROM localization_jobs",
            ).fetchone()[0],
        )
        self.assertEqual(queue_status.max_attempts, 4)

        progress_request = {
            "schema": RUNTIME._API.STATUS_REQUEST_SCHEMA,
            "request_id": "runtime-status-1",
            "event_id": event["event_id"],
            "site_id": event["site_id"],
            "requested_at": self.clock(),
        }
        status, progress = request(RUNTIME._API.STATUS_PATH, progress_request)
        self.assertEqual(status, "200 OK")
        self.assertEqual(progress["source_sequence"], 1)
        self.assertEqual(progress["locales"][0]["target_locale"], "fi-FI")
        self.assertNotIn("source_text", json.dumps(progress))
        self.assertIs(runtime.cms_api.bridge, runtime.bridge)

    def test_runtime_exposes_read_only_tenant_lifecycle_through_publication(self):
        runtime = self.runtime()
        event = self.event()

        def request(path, value):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            signature = self.event_authority.sign(
                RUNTIME._API._canonical_json(value).encode("utf-8"),
            )
            environ = {
                "PATH_INFO": path,
                "QUERY_STRING": "",
                "REQUEST_METHOD": "POST",
                "wsgi.url_scheme": "https",
                "CONTENT_TYPE": "application/json; charset=utf-8",
                "CONTENT_LENGTH": str(len(raw)),
                "wsgi.input": io.BytesIO(raw),
                "HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM": signature.algorithm,
                "HTTP_X_LOCALIZATION_KEY_ID": signature.key_id,
                "HTTP_X_LOCALIZATION_SIGNATURE": signature.signature,
            }
            captured = {}
            body = b"".join(runtime.cms_api(
                environ,
                lambda status, headers: captured.update(status=status),
            ))
            return captured["status"], json.loads(body)

        def lifecycle(request_id):
            value = {
                "schema": RUNTIME._API.LIFECYCLE_REQUEST_SCHEMA,
                "request_id": request_id,
                "event_id": event["event_id"],
                "site_id": event["site_id"],
                "requested_at": self.clock(),
            }
            changes_before = tuple(
                connection.total_changes for connection in self.connections
            )
            response = request(RUNTIME._API.LIFECYCLE_PATH, value)
            self.assertEqual(
                changes_before,
                tuple(connection.total_changes for connection in self.connections),
            )
            self.assertNotIn("Grow your business", json.dumps(response[1]))
            self.assertNotIn("Kasvata", json.dumps(response[1]))
            return response

        status, accepted = request(RUNTIME._API.CHANGE_PATH, event)
        self.assertEqual(status, "202 Accepted")
        self.assertEqual(lifecycle("lifecycle-processing")[1]["status"], "processing")

        self.clock.value = 101
        self.assertEqual(runtime.run_once(now=101).tick["phase"], "translation")
        awaiting = lifecycle("lifecycle-awaiting")[1]
        self.assertEqual(awaiting["status"], "awaiting_approval")
        self.assertEqual(awaiting["approved_locales"], [])

        self.clock.value = 102
        self.assertEqual(runtime.run_once(now=102).tick["phase"], "release")
        publishing = lifecycle("lifecycle-publishing")[1]
        self.assertEqual(publishing["status"], "publishing")
        self.assertEqual(publishing["approved_locales"], ["fi-FI"])
        self.assertEqual(publishing["delivery"]["status"], "pending")

        self.clock.value = 103
        self.assertEqual(runtime.run_once(now=103).tick["phase"], "delivery")
        published = lifecycle("lifecycle-published")[1]
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["delivery"]["status"], "succeeded")

    def test_tenant_lifecycle_fails_closed_for_expired_pending_approvals(self):
        self.dependencies["approval_ttl_seconds"] = 1
        runtime = self.runtime()
        self.ingest(runtime)
        self.clock.value = 101
        runtime.run_once(now=101)
        self.clock.value = 102
        runtime.run_once(now=102)
        self.clock.value = 103
        value = {
            "schema": RUNTIME._API.LIFECYCLE_REQUEST_SCHEMA,
            "request_id": "lifecycle-expired",
            "event_id": "event-1",
            "site_id": "public-site",
            "requested_at": 103,
        }
        raw = json.dumps(value).encode("utf-8")
        signature = self.event_authority.sign(
            RUNTIME._API._canonical_json(value).encode("utf-8"),
        )
        captured = {}
        body = b"".join(runtime.cms_api({
            "PATH_INFO": RUNTIME._API.LIFECYCLE_PATH,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
            "HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM": signature.algorithm,
            "HTTP_X_LOCALIZATION_KEY_ID": signature.key_id,
            "HTTP_X_LOCALIZATION_SIGNATURE": signature.signature,
        }, lambda status, headers: captured.update(status=status)))
        payload = json.loads(body)

        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(payload["status"], "publication_blocked")
        self.assertEqual(payload["blocked_locales"], [["fi-FI", "approval.expired"]])
        self.assertEqual(payload["delivery"]["status"], "pending")

    def test_tenant_lifecycle_rejects_tampered_signed_delivery_state(self):
        runtime = self.runtime()
        self.ingest(runtime)
        self.clock.value = 101
        runtime.run_once(now=101)
        self.clock.value = 102
        runtime.run_once(now=102)
        runtime.bridge.connection.execute(
            "UPDATE cms_publication_deliveries SET payload_json = '{}'",
        )
        value = {
            "schema": RUNTIME._API.LIFECYCLE_REQUEST_SCHEMA,
            "request_id": "lifecycle-tampered",
            "event_id": "event-1",
            "site_id": "public-site",
            "requested_at": 102,
        }
        raw = json.dumps(value).encode("utf-8")
        signature = self.event_authority.sign(
            RUNTIME._API._canonical_json(value).encode("utf-8"),
        )
        captured = {}
        body = b"".join(runtime.cms_api({
            "PATH_INFO": RUNTIME._API.LIFECYCLE_PATH,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "https",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
            "HTTP_X_LOCALIZATION_SIGNATURE_ALGORITHM": signature.algorithm,
            "HTTP_X_LOCALIZATION_KEY_ID": signature.key_id,
            "HTTP_X_LOCALIZATION_SIGNATURE": signature.signature,
        }, lambda status, headers: captured.update(status=status)))
        payload = json.loads(body)

        self.assertEqual(captured["status"], "503 Service Unavailable")
        self.assertEqual(payload["error"], "cms.delivery.tampered")
        self.assertNotIn("localizations", payload)

    def test_runtime_exposes_health_only_with_explicit_operator_authentication(self):
        authentication_requests = []

        def authenticate(request):
            authentication_requests.append(request)
            return {
                "schema": RUNTIME._HEALTH_HTTP.PRINCIPAL_SCHEMA,
                "reader_id": "operations-1",
                "credential_id": "health-reader-1",
                "credential_version": "2026-09-09",
                "scope": "service-health",
            }

        runtime = self.runtime(
            health_http_authenticator=authenticate,
            health_provider_probe=ProviderProbe(),
        )
        changes_before = tuple(
            connection.total_changes for connection in self.connections
        )
        captured = {}
        body = b"".join(runtime.health_http({
            "PATH_INFO": RUNTIME._HEALTH_HTTP.HEALTH_PATH,
            "QUERY_STRING": "",
            "REQUEST_METHOD": "GET",
            "wsgi.url_scheme": "https",
            "CONTENT_LENGTH": "",
            "wsgi.input": io.BytesIO(b""),
            "HTTP_AUTHORIZATION": "Bearer private-operator-token",
        }, lambda status, headers: captured.update(
            status=status, headers=dict(headers),
        )))
        payload = json.loads(body)

        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(payload["schema"], RUNTIME._HEALTH_HTTP.RESPONSE_SCHEMA)
        self.assertEqual(payload["report"]["status"], "degraded")
        self.assertEqual(payload["report"]["schema"], RUNTIME._HEALTH.SCHEMA)
        self.assertEqual(len(authentication_requests), 1)
        self.assertNotIn("private-operator-token", json.dumps(payload))
        self.assertNotIn("source_text", json.dumps(payload))
        self.assertEqual(
            changes_before,
            tuple(connection.total_changes for connection in self.connections),
        )

        unconfigured = self.runtime()
        self.assertIsNone(unconfigured.health_http)

    def test_runtime_restores_only_its_signed_local_translation_memory(self):
        self.seed_approval_then_replace_operational_stores()
        calls = []
        self.dependencies["assets_resolver"] = lambda payload: calls.append("assets")
        self.dependencies["provider_resolver"] = lambda payload: calls.append("provider")
        runtime = self.runtime()
        self.ingest(runtime)

        self.clock.value = 102
        outcome = runtime.run_once(now=102)

        self.assertEqual(outcome.tick["phase"], "release")
        self.assertEqual(outcome.tick["status"], "delivery_ready")
        self.assertEqual(calls, [])
        self.assertIs(runtime._dependencies["result_cache"].store, runtime.release_store)
        self.assertIs(
            runtime._dependencies["result_cache"].authority,
            self.approval_authority,
        )
        self.clock.value = 103
        delivered = runtime.run_once(now=103)
        self.assertEqual(delivered.tick["phase"], "delivery")
        self.assertEqual(delivered.tick["status"], "succeeded")
        self.assertEqual(calls, [])
        self.assertEqual(len(self.publisher.requests), 1)

    def test_tampered_runtime_fallback_blocks_before_external_resolvers(self):
        self.seed_approval_then_replace_operational_stores()
        self.connections[1].execute(
            "UPDATE localization_approvals SET result_json = '{}'",
        )
        self.connections[1].commit()
        calls = []
        self.dependencies["assets_resolver"] = lambda payload: calls.append("assets")
        self.dependencies["provider_resolver"] = lambda payload: calls.append("provider")
        runtime = self.runtime()
        self.ingest(runtime)

        self.clock.value = 102
        outcome = runtime.run_once(now=102)

        self.assertEqual(outcome.tick["phase"], "translation")
        self.assertEqual(outcome.tick["status"], "retry_wait")
        self.assertEqual(outcome.tick["error_code"], "runner.cache.unexpected")
        self.assertEqual(calls, [])

    def test_runtime_renews_outer_lease_for_each_child_operation_kind(self):
        self.dependencies.update({
            "translation_lease_seconds": 101,
            "evidence_lease_seconds": 102,
            "delivery_lease_seconds": 103,
        })
        policy = {
            "lease_seconds": 104,
            "active_delay_seconds": 1,
            "idle_delay_seconds": 5,
            "blocked_base_seconds": 2,
            "blocked_max_seconds": 20,
            "stop_poll_seconds": 1,
        }
        runtime = self.runtime(supervisor_policy=policy)
        event = self.event()
        signature = self.event_authority.sign(
            CMS._canonical_json(event).encode("utf-8"),
        )
        runtime.bridge.ingest_change(
            event, signature, self.event_authority, now=self.clock(),
        )
        guarded_leases = []
        original_guard = runtime.supervisor.renew_active_lease

        def observing_guard(seconds):
            guarded_leases.append(seconds)
            return original_guard(seconds)

        runtime.supervisor.renew_active_lease = observing_guard
        for now in (100, 101, 102):
            self.clock.value = now
            self.assertEqual(runtime.run_once(now=now).status, "ran")

        self.assertTrue({101.0, 102.0, 103.0}.issubset(guarded_leases))

    def test_preflight_rejects_duplicate_connection_without_schema_writes(self):
        duplicate = self.connections[0]
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked, "runtime.connections.not_distinct"
        ):
            self.runtime(supervisor_connection=duplicate)

        after = tuple(connection.total_changes for connection in self.connections)
        self.assertEqual(before, after)

    def test_runtime_rejects_invalid_cms_ingress_policy_without_schema_writes(self):
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.cms_api.max_attempts.invalid",
        ):
            self.runtime(cms_api_max_attempts=0)

        self.assertEqual(
            before,
            tuple(connection.total_changes for connection in self.connections),
        )

    def test_runtime_rejects_invalid_health_http_configuration_without_schema_writes(self):
        scenarios = (
            (
                {"health_http_authenticator": object()},
                "runtime.health_http.authenticator.invalid",
            ),
            (
                {"health_provider_probe": ProviderProbe()},
                "runtime.health_http.incomplete",
            ),
            (
                {
                    "health_http_authenticator": lambda request: None,
                    "health_provider_probe": object(),
                },
                "runtime.health_http.provider_probe.invalid",
            ),
        )
        for options, code in scenarios:
            with self.subTest(code=code):
                before = tuple(
                    connection.total_changes for connection in self.connections
                )
                with self.assertRaisesRegex(
                    RUNTIME.LocalizationRuntimeBlocked, code,
                ):
                    self.runtime(**options)
                self.assertEqual(
                    before,
                    tuple(
                        connection.total_changes for connection in self.connections
                    ),
                )

    def test_runtime_integrates_bound_benchmark_campaign_health(self):
        campaign = RUNTIME._HEALTH._CAMPAIGN
        benchmark_connection = sqlite3.connect(":memory:")
        self.connections.append(benchmark_connection)
        benchmark_policy = self.benchmark_policy()
        store = campaign.BenchmarkCampaignStore(benchmark_connection)
        campaign_id = store.create(benchmark_policy, now=100)
        authority = Authority(
            campaign._BENCHMARK.BenchmarkSignature,
            b"benchmark-key",
            "benchmark-key",
        )
        self.clock.value = 105

        runtime = self.runtime(
            benchmark_connection=benchmark_connection,
            benchmark_policy=benchmark_policy,
            benchmark_campaign_id=campaign_id,
            benchmark_evidence_authority=authority,
            benchmark_stale_after_seconds=30,
        )
        report = runtime.health(now=105)

        self.assertIs(runtime.benchmark_store.connection, benchmark_connection)
        component = next(
            item for item in report.components
            if item.component == "benchmark_campaign"
        )
        self.assertEqual(component.status, "healthy")
        work_count = 2 * len(campaign._SUITE.manifest()["cases"])
        self.assertEqual(dict(component.counts)["work_count"], work_count)
        self.assertEqual(dict(component.counts)["pending"], work_count)

    def test_runtime_rejects_partial_benchmark_before_schema_writes(self):
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.benchmark.incomplete",
        ):
            self.runtime(benchmark_policy=self.benchmark_policy())

        after = tuple(connection.total_changes for connection in self.connections)
        self.assertEqual(before, after)

    def test_runtime_rejects_stale_benchmark_binding_before_schema_writes(self):
        campaign = RUNTIME._HEALTH._CAMPAIGN
        benchmark_connection = sqlite3.connect(":memory:")
        self.connections.append(benchmark_connection)
        authority = Authority(
            campaign._BENCHMARK.BenchmarkSignature,
            b"benchmark-key",
            "benchmark-key",
        )
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.benchmark.binding.invalid",
        ):
            self.runtime(
                benchmark_connection=benchmark_connection,
                benchmark_policy=self.benchmark_policy(),
                benchmark_campaign_id="benchmark-campaign-" + "0" * 64,
                benchmark_evidence_authority=authority,
            )

        after = tuple(connection.total_changes for connection in self.connections)
        self.assertEqual(before, after)
        self.assertIsNone(benchmark_connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        ).fetchone())

    def test_runtime_runs_one_benchmark_case_only_after_customer_work_is_idle(self):
        values = self.benchmark_configuration()
        fake = mock.Mock()
        fake.campaign_id = values["benchmark_campaign_id"]
        fake.review_store = RUNTIME._HEALTH._BENCHMARK_REVIEW.BenchmarkReviewEvidenceStore(
            values["benchmark_execution"]["review_connection"]
        )
        fake.reviewer_route_id = values["benchmark_execution"]["reviewer_route_id"]
        fake.run_once.side_effect = lambda **kwargs: (
            kwargs["operation_guard"](kwargs["lease_seconds"]),
            SimpleNamespace(
                status="succeeded",
                work_id="benchmark-work-" + "a" * 64,
                target_locale="mt-MT",
                attempt=1,
                error_code=None,
            ),
        )[1]
        with mock.patch.object(
            RUNTIME._BENCHMARK_RUNTIME,
            "WebsiteLocalizationBenchmarkRuntime",
            return_value=fake,
        ):
            runtime = self.runtime(**values)

        outcome = runtime.run_once(now=100)

        self.assertEqual(outcome.tick["phase"], "benchmark")
        self.assertEqual(outcome.tick["status"], "succeeded")
        self.assertEqual(outcome.tick["target_locale"], "mt-MT")
        self.assertEqual(outcome.tick["attempt"], 1)
        self.assertEqual(fake.run_once.call_count, 1)
        self.assertEqual(
            fake.run_once.call_args.kwargs["lease_seconds"],
            300.0,
        )

    def test_runtime_reports_crash_gap_finalization_as_benchmark_work(self):
        values = self.benchmark_configuration()
        fake = mock.Mock()
        fake.campaign_id = values["benchmark_campaign_id"]
        fake.review_store = RUNTIME._HEALTH._BENCHMARK_REVIEW.BenchmarkReviewEvidenceStore(
            values["benchmark_execution"]["review_connection"]
        )
        fake.reviewer_route_id = values["benchmark_execution"]["reviewer_route_id"]
        fake.run_once.return_value = SimpleNamespace(
            status="succeeded",
            work_id=None,
            target_locale=None,
            attempt=None,
            error_code=None,
        )
        with mock.patch.object(
            RUNTIME._BENCHMARK_RUNTIME,
            "WebsiteLocalizationBenchmarkRuntime",
            return_value=fake,
        ):
            runtime = self.runtime(**values)

        outcome = runtime.run_once(now=100)

        self.assertEqual(outcome.tick["phase"], "benchmark")
        self.assertEqual(outcome.tick["status"], "succeeded")
        self.assertIsNone(outcome.tick["job_id"])
        self.assertIsNone(outcome.tick["target_locale"])
        self.assertIsNone(outcome.tick["attempt"])
        status = runtime.supervisor.status(now=100)
        self.assertEqual(status.last_phase, "benchmark")
        self.assertEqual(status.last_status, "succeeded")

    def test_runtime_constructs_exact_durable_benchmark_execution_root(self):
        values = self.benchmark_configuration()

        runtime = self.runtime(**values)

        self.assertIsInstance(
            runtime.benchmark_runtime,
            RUNTIME._BENCHMARK_RUNTIME.WebsiteLocalizationBenchmarkRuntime,
        )
        self.assertEqual(
            runtime.benchmark_runtime.campaign_id,
            values["benchmark_campaign_id"],
        )
        self.assertIs(
            runtime.benchmark_runtime.campaign_store.connection,
            values["benchmark_connection"],
        )
        status = runtime.benchmark_runtime.status()
        work_count = 2 * len(
            RUNTIME._HEALTH._CAMPAIGN._SUITE.manifest()["cases"]
        )
        self.assertEqual(status["work_count"], work_count)
        self.assertEqual(status["counts"]["pending"], work_count)
        report = runtime.health(now=100)
        review_health = next(
            component for component in report.components
            if component.component == "benchmark_reviews"
        )
        self.assertEqual(review_health.status, "healthy")
        self.assertEqual(dict(review_health.counts)["scoped"], 0)
        self.assertEqual(dict(review_health.counts)["required"], 0)
        reference_health = next(
            component for component in report.components
            if component.component == "benchmark_native_references"
        )
        self.assertEqual(reference_health.status, "healthy")
        self.assertEqual(
            dict(reference_health.counts)["work_count"], work_count,
        )

        lease = runtime.claim_native_reference_work_order("native-editor-1")
        reference_status = runtime.native_reference_queue_status()
        reference_health = runtime.native_reference_queue_health(now=100)
        self.assertEqual(lease.as_payload()["work_id"], lease.claim.work_id)
        restored = runtime.native_reference_lease_from_payload(
            lease.as_payload(),
            editor_id="native-editor-1",
            target_locale=lease.claim.target_locale,
        )
        self.assertEqual(restored.as_payload(), lease.as_payload())
        self.assertIsNone(runtime.native_reference_http_request_replay(
            editor_id="native-editor-1",
            target_locale=lease.claim.target_locale,
            operation="renew",
            request_id="runtime-renew-missing-0001",
            request_sha256="1" * 64,
        ))
        self.assertEqual(reference_status["counts"]["leased"], 1)
        self.assertEqual(reference_health.status, "healthy")
        self.clock.value = 101
        renewed = runtime.renew_native_reference_work_order(
            lease, lease_seconds=3600,
        )
        self.assertGreater(
            renewed.claim.lease_expires_at,
            lease.claim.lease_expires_at,
        )

    def test_runtime_native_reference_queue_boundary_fails_closed(self):
        runtime = self.runtime()
        for operation in (
            lambda: runtime.claim_native_reference_work_order("native-editor-1"),
            lambda: runtime.native_reference_lease_from_payload(
                {}, editor_id="native-editor-1", target_locale="mt-MT",
            ),
            lambda: runtime.native_reference_http_request_replay(
                editor_id="native-editor-1",
                target_locale="mt-MT",
                operation="renew",
                request_id="runtime-renew-missing-0001",
                request_sha256="1" * 64,
            ),
            runtime.native_reference_queue_status,
            lambda: runtime.native_reference_queue_health(now=100),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(
                    RUNTIME.LocalizationRuntimeBlocked,
                ) as caught:
                    operation()
                self.assertEqual(
                    caught.exception.code,
                    "runtime.benchmark.unavailable",
                )

    def test_runtime_exposes_only_a_stored_reverified_benchmark_report(self):
        values = self.benchmark_configuration()
        values.pop("benchmark_execution")
        expected = {"schema": "verified-report", "status": "BLOCK"}
        expected_status = {
            "campaign_id": values["benchmark_campaign_id"],
            "complete": False,
        }
        with mock.patch.object(
            RUNTIME._HEALTH._CAMPAIGN.BenchmarkCampaignStore,
            "load_report",
            return_value=expected,
        ) as load_report, mock.patch.object(
            RUNTIME._HEALTH._CAMPAIGN.BenchmarkCampaignStore,
            "status",
            return_value=expected_status,
        ) as campaign_status:
            runtime = self.runtime(**values)
            observed = runtime.load_benchmark_report(now=123)
            observed_status = runtime.benchmark_campaign_status()

        self.assertIs(observed, expected)
        self.assertIs(observed_status, expected_status)
        load_report.assert_called_once_with(
            values["benchmark_policy"],
            values["benchmark_campaign_id"],
            values["benchmark_evidence_authority"],
            now=123,
        )
        campaign_status.assert_called_once_with(
            values["benchmark_policy"],
            values["benchmark_campaign_id"],
        )

    def test_runtime_benchmark_report_boundary_is_content_free(self):
        runtime = self.runtime()
        for operation in (
            lambda: runtime.load_benchmark_report(now=100),
            runtime.benchmark_campaign_status,
        ):
            with self.assertRaises(RUNTIME.LocalizationRuntimeBlocked) as caught:
                operation()
            self.assertEqual(caught.exception.code, "runtime.benchmark.unavailable")

        values = self.benchmark_configuration()
        values.pop("benchmark_execution")
        with mock.patch.object(
            RUNTIME._HEALTH._CAMPAIGN.BenchmarkCampaignStore,
            "load_report",
            side_effect=RuntimeError("private report failure"),
        ):
            runtime = self.runtime(**values)
            with self.assertRaises(RUNTIME.LocalizationRuntimeBlocked) as caught:
                runtime.load_benchmark_report(now=100)
        self.assertEqual(
            caught.exception.code,
            "runtime.benchmark.report.invalid",
        )
        self.assertNotIn("private report failure", str(caught.exception))

        with mock.patch.object(
            RUNTIME._HEALTH._CAMPAIGN.BenchmarkCampaignStore,
            "status",
            side_effect=RuntimeError("private status failure"),
        ):
            runtime = self.runtime(**values)
            with self.assertRaises(RUNTIME.LocalizationRuntimeBlocked) as caught:
                runtime.benchmark_campaign_status()
        self.assertEqual(caught.exception.code, "runtime.benchmark.status.invalid")
        self.assertNotIn("private status failure", str(caught.exception))

    def test_runtime_prioritizes_all_customer_phases_over_benchmark(self):
        values = self.benchmark_configuration()
        fake = mock.Mock()
        fake.campaign_id = values["benchmark_campaign_id"]
        fake.review_store = RUNTIME._HEALTH._BENCHMARK_REVIEW.BenchmarkReviewEvidenceStore(
            values["benchmark_execution"]["review_connection"]
        )
        fake.reviewer_route_id = values["benchmark_execution"]["reviewer_route_id"]
        fake.run_once.return_value = SimpleNamespace(
            status="succeeded",
            work_id="benchmark-work-" + "b" * 64,
            target_locale="fi-FI",
            attempt=1,
            error_code=None,
        )
        with mock.patch.object(
            RUNTIME._BENCHMARK_RUNTIME,
            "WebsiteLocalizationBenchmarkRuntime",
            return_value=fake,
        ):
            runtime = self.runtime(**values)
        self.ingest(runtime)

        phases = []
        for now in (100, 101, 102):
            self.clock.value = now
            phases.append(runtime.run_once(now=now).tick["phase"])
            fake.run_once.assert_not_called()
        self.clock.value = 103
        benchmark = runtime.run_once(now=103)

        self.assertEqual(phases, ["translation", "release", "delivery"])
        self.assertEqual(benchmark.tick["phase"], "benchmark")
        fake.run_once.assert_called_once()

    def test_runtime_rejects_incomplete_benchmark_execution_without_schema_writes(self):
        values = self.benchmark_configuration()
        values["benchmark_execution"].pop("reviewer")
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.benchmark.execution.invalid",
        ):
            self.runtime(**values)

        self.assertEqual(
            before,
            tuple(connection.total_changes for connection in self.connections),
        )
        self.assertTrue(all(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchone() is None
            for connection in self.connections
        ))

    def test_runtime_benchmark_lease_must_fit_outer_lease_before_schema_writes(self):
        values = self.benchmark_configuration(lease_seconds=301)
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.lease_hierarchy.invalid",
        ):
            self.runtime(**values)

        self.assertEqual(
            before,
            tuple(connection.total_changes for connection in self.connections),
        )
        self.assertTrue(all(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchone() is None
            for connection in self.connections
        ))

    def test_runtime_rejects_invalid_benchmark_clock_without_schema_writes(self):
        values = self.benchmark_configuration()
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.clock.invalid",
        ):
            self.runtime(clock=lambda: True, **values)

        self.assertEqual(
            before,
            tuple(connection.total_changes for connection in self.connections),
        )
        self.assertTrue(all(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchone() is None
            for connection in self.connections
        ))

    def test_runtime_rejects_expired_benchmark_before_schema_writes(self):
        values = self.benchmark_configuration()
        values["benchmark_policy"] = replace(
            values["benchmark_policy"], valid_until=99,
        )
        campaign = RUNTIME._HEALTH._CAMPAIGN
        values["benchmark_campaign_id"] = campaign._campaign_identity(
            values["benchmark_policy"],
        )[0]
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.benchmark.validity_expired",
        ):
            self.runtime(**values)

        self.assertEqual(
            before,
            tuple(connection.total_changes for connection in self.connections),
        )
        self.assertTrue(all(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchone() is None
            for connection in self.connections
        ))

    def test_runtime_rejects_benchmark_store_reuse_without_schema_writes(self):
        values = self.benchmark_configuration(
            candidate_connection=self.connections[0],
        )
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.connections.not_distinct",
        ):
            self.runtime(**values)

        self.assertEqual(
            before,
            tuple(connection.total_changes for connection in self.connections),
        )
        self.assertTrue(all(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchone() is None
            for connection in self.connections
        ))

    def test_preflight_rejects_missing_capability_without_schema_writes(self):
        self.dependencies["publisher"] = object()
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked, "runtime.publisher.invalid"
        ):
            self.runtime()

        after = tuple(connection.total_changes for connection in self.connections)
        self.assertEqual(before, after)

    def test_preflight_rejects_external_result_cache_without_schema_writes(self):
        self.dependencies["result_cache"] = type(
            "ExternalCache", (), {"resolve": lambda *args, **kwargs: None},
        )()
        before = tuple(connection.total_changes for connection in self.connections)

        with self.assertRaisesRegex(
            RUNTIME.LocalizationRuntimeBlocked,
            "runtime.result_cache.external_forbidden",
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

    def test_preflight_requires_supervisor_lease_to_outlive_every_operation(self):
        original_connections = self.connections
        try:
            for name in (
                "translation_lease_seconds",
                "evidence_lease_seconds",
                "delivery_lease_seconds",
            ):
                with self.subTest(name=name):
                    connections = [sqlite3.connect(":memory:") for _ in range(5)]
                    self.connections = connections
                    self.dependencies[name] = 301
                    try:
                        before = tuple(
                            connection.total_changes for connection in connections
                        )

                        with self.assertRaisesRegex(
                            RUNTIME.LocalizationRuntimeBlocked,
                            "runtime.lease_hierarchy.invalid",
                        ):
                            self.runtime()

                        after = tuple(
                            connection.total_changes for connection in connections
                        )
                        self.assertEqual(before, after)
                        self.assertTrue(all(
                            connection.execute(
                                "SELECT name FROM sqlite_master WHERE type = 'table'"
                            ).fetchone() is None
                            for connection in connections
                        ))
                    finally:
                        self.dependencies.pop(name, None)
                        for connection in connections:
                            connection.close()
        finally:
            self.connections = original_connections

    def test_default_operation_leases_fit_valid_supervisor_boundary(self):
        runtime = self.runtime()

        self.assertEqual(runtime.supervisor.policy.lease_seconds, 301.0)

    def test_runtime_default_supervisor_policy_outlives_default_operations(self):
        runtime = self.runtime(supervisor_policy=None)

        self.assertEqual(runtime.supervisor.policy.lease_seconds, 360.0)

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
