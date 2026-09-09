from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
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


BASELINE = load(
    "blun_test_website_localization_deepl_baseline",
    ROOT / "integrations" / "website_localization_deepl_baseline.py",
)
BENCHMARK = BASELINE._BENCHMARK
PLANNER = BENCHMARK._PLANNER
WORKER = BENCHMARK._WORKER
SUITE = BENCHMARK._SUITE
SUITE_MANIFEST = SUITE.manifest()


class HmacAuthority:
    def __init__(self, key=b"deepl-baseline-test-key"):
        self.key = key

    def sign(self, payload):
        return BENCHMARK.BenchmarkSignature(
            "hmac-sha256-test", "benchmark-test-key-1",
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )

    def verify(self, payload, signature):
        return hmac.compare_digest(
            signature.signature,
            hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        )


def policy(**overrides):
    values = {
        "benchmark_version": "native-vs-baseline-1",
        "suite_version": SUITE_MANIFEST["version"],
        "suite_sha256": SUITE_MANIFEST["sha256"],
        "candidate_provider_id": "customer-llm",
        "candidate_model_id": "king",
        "candidate_model_version": "2026-08-30",
        "candidate_software_version": "6.43.0-dev",
        "candidate_worker_schema": WORKER.WORKER_SCHEMA,
        "candidate_glossary_version": "glossary-3",
        "candidate_policy_version": "native-web-2",
        "attestation_algorithm": "hmac-sha256-test",
        "attestation_key_id": "benchmark-test-key-1",
        "baseline_id": "deepl-official",
        "baseline_version": "current-api-2026-09-08",
        "reviewer_id": "native-panel",
        "reviewer_version": "2026-09-08",
        "native_reference_revision": "native-reference-1",
        "native_reference_verifier_id": "review-registry",
        "native_reference_verifier_version": "2026-09-08",
        "valid_until": 1_800_000_000,
        "required_locales": ("mt-MT", "fi-FI"),
        "required_content_types": BENCHMARK.EU_BENCHMARK_CONTENT_TYPES,
        "minimum_cases_per_locale": len(SUITE.SOURCE_CASES),
        "minimum_cases_per_content_type": 8,
        "minimum_decisive_rate": 0.75,
        "minimum_candidate_win_rate": 0.60,
        "maximum_one_sided_p": 0.05,
    }
    values.update(overrides)
    return BENCHMARK.BenchmarkPolicy(**values)


def job(locale="fi-FI", case_index=0, benchmark_policy=None):
    benchmark_policy = benchmark_policy or policy()
    case = SUITE.SOURCE_CASES[case_index].as_payload()
    return PLANNER.plan_website_localization(
        source_id=case["source_id"],
        source_revision=case["source_revision"],
        source_text=case["source_text"],
        source_locale=case["source_locale"],
        content_type=case["content_type"],
        glossary_version=benchmark_policy.candidate_glossary_version,
        policy_version=benchmark_policy.candidate_policy_version,
        provider_id=benchmark_policy.candidate_provider_id,
        model_id=benchmark_policy.candidate_model_id,
        model_version=benchmark_policy.candidate_model_version,
        software_version=benchmark_policy.candidate_software_version,
        target_locales=[locale],
    ).jobs[0].as_payload()


def response(value, status=200, *, content_type="application/json"):
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return BASELINE.HTTPResult(
        status,
        (("Content-Type", content_type), ("Content-Length", str(len(body)))),
        body,
    )


def languages(*extra):
    return response([
        {
            "lang": "en", "name": "English", "usable_as_source": True,
            "usable_as_target": False, "status": "stable", "features": {},
        },
        {
            "lang": "fi", "name": "Finnish", "usable_as_source": True,
            "usable_as_target": True, "status": "stable", "features": {},
        },
        *extra,
    ])


def translation(text="Kasvata liiketoimintaasi luontevasti."):
    return response({"translations": [{
        "detected_source_language": "EN",
        "model_type_used": "quality_optimized",
        "text": text,
    }]})


class Transport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, headers, body, *, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class DeepLBaselineTest(unittest.TestCase):
    def adapter(self, transport, **kwargs):
        return BASELINE.DeepLBaselineAdapter(
            "pro", lambda: "private-api-key", transport=transport, **kwargs,
        )

    def assert_failure(self, expected, function, *, retryable):
        with self.assertRaises(BASELINE.DeepLBaselineFailed) as caught:
            function()
        self.assertEqual(caught.exception.code, expected)
        self.assertIs(caught.exception.retryable, retryable)

    def test_official_api_acquires_one_complete_finnish_document(self):
        transport = Transport(languages(), translation())
        payload = job()
        authority = HmacAuthority()
        result = self.adapter(transport).acquire(
            payload, policy(), evidence_authority=authority,
        )

        self.assertEqual(len(transport.calls), 2)
        language_call, translate_call = transport.calls
        self.assertEqual(language_call[:2], (
            "GET", "https://api.deepl.com/v3/languages?resource=translate_text",
        ))
        self.assertIsNone(language_call[3])
        self.assertEqual(translate_call[:2], (
            "POST", "https://api.deepl.com/v2/translate",
        ))
        sent = json.loads(translate_call[3])
        self.assertEqual(sent["text"], [payload["source"]["text"]])
        self.assertEqual(sent["source_lang"], "en")
        self.assertEqual(sent["target_lang"], "fi")
        self.assertEqual(sent["model_type"], "prefer_quality_optimized")
        self.assertIs(sent["preserve_formatting"], True)
        self.assertEqual(
            translate_call[2]["Authorization"],
            "DeepL-Auth-Key private-api-key",
        )
        self.assertEqual(
            result.artifact["provenance"]["method"], "official_api",
        )
        self.assertNotIn("private-api-key", repr(result))
        self.assertNotIn("private-api-key", json.dumps(result.evidence))
        validated_job = WORKER._validated_job(payload)
        self.assertEqual(
            BENCHMARK._validate_baseline(
                validated_job, result.artifact, policy(), authority,
            ),
            result.artifact,
        )

    def test_free_account_uses_only_the_official_free_origin(self):
        transport = Transport(languages(), translation())
        adapter = BASELINE.DeepLBaselineAdapter(
            "free", lambda: "key", transport=transport,
        )
        adapter.acquire(job(), policy(), evidence_authority=HmacAuthority())
        self.assertTrue(all(
            call[1].startswith("https://api-free.deepl.com/")
            for call in transport.calls
        ))

    def test_language_capabilities_are_cached_for_at_most_one_hour(self):
        now = [100.0]
        transport = Transport(
            languages(), translation("Ensimmäinen."),
            translation("Toinen."), languages(), translation("Kolmas."),
        )
        adapter = self.adapter(transport, clock=lambda: now[0])
        adapter.acquire(job(), policy(), evidence_authority=HmacAuthority())
        adapter.acquire(job(), policy(), evidence_authority=HmacAuthority())
        now[0] += 3600
        adapter.acquire(job(), policy(), evidence_authority=HmacAuthority())
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["GET", "POST", "POST", "GET", "POST"],
        )

    def test_unsupported_maltese_blocks_without_translation_call(self):
        transport = Transport(languages())
        self.assert_failure(
            "deepl.target_language_unsupported",
            lambda: self.adapter(transport).acquire(
                job("mt-MT"), policy(), evidence_authority=HmacAuthority(),
            ),
            retryable=False,
        )
        self.assertEqual([call[0] for call in transport.calls], ["GET"])

    def test_new_stable_language_support_is_accepted_dynamically(self):
        maltese = {
            "lang": "mt", "usable_as_source": True,
            "usable_as_target": True, "status": "stable", "features": {},
        }
        transport = Transport(
            languages(maltese), translation("Kabbar in-negozju tiegħek."),
        )
        result = self.adapter(transport).acquire(
            job("mt-MT"), policy(), evidence_authority=HmacAuthority(),
        )
        self.assertEqual(result.evidence["target_language"], "mt")

    def test_exact_regional_target_precedes_base_language(self):
        configured = policy(required_locales=("mt-MT", "fi-FI", "pt-PT"))
        portuguese = [
            {
                "lang": "pt", "usable_as_source": True,
                "usable_as_target": True, "status": "stable",
            },
            {
                "lang": "pt-PT", "usable_as_source": False,
                "usable_as_target": True, "status": "stable",
            },
        ]
        transport = Transport(languages(*portuguese), translation("Avance."))
        payload = job("pt-PT", benchmark_policy=configured)
        self.adapter(transport).acquire(
            payload, configured, evidence_authority=HmacAuthority(),
        )
        self.assertEqual(json.loads(transport.calls[1][3])["target_lang"], "pt-PT")

    def test_wrong_detected_source_language_blocks(self):
        invalid = response({"translations": [{
            "detected_source_language": "DE", "text": "Teksti.",
        }]})
        self.assert_failure(
            "deepl.translation_invalid",
            lambda: self.adapter(Transport(languages(), invalid)).acquire(
                job(), policy(), evidence_authority=HmacAuthority(),
            ),
            retryable=True,
        )

    def test_multiple_translations_and_extra_top_level_fields_block(self):
        for invalid in (
            {"translations": [{"text": "A"}, {"text": "B"}]},
            {"translations": [{"text": "A"}], "extra": True},
        ):
            with self.subTest(invalid=invalid):
                self.assert_failure(
                    "deepl.translation_invalid",
                    lambda invalid=invalid: self.adapter(
                        Transport(languages(), response(invalid)),
                    ).acquire(job(), policy(), evidence_authority=HmacAuthority()),
                    retryable=True,
                )

    def test_duplicate_json_keys_and_bom_block(self):
        bodies = (
            b'{"translations":[],"translations":[]}',
            b'\xef\xbb\xbf{"translations":[]}',
        )
        for body in bodies:
            with self.subTest(body=body):
                invalid = BASELINE.HTTPResult(
                    200,
                    (("Content-Type", "application/json"),
                     ("Content-Length", str(len(body)))),
                    body,
                )
                self.assert_failure(
                    "deepl.response_invalid",
                    lambda invalid=invalid: self.adapter(
                        Transport(languages(), invalid),
                    ).acquire(job(), policy(), evidence_authority=HmacAuthority()),
                    retryable=True,
                )

    def test_http_failures_have_bounded_retry_classification(self):
        cases = {
            400: ("deepl.http_error", False),
            403: ("deepl.authentication", False),
            429: ("deepl.rate_limited", True),
            456: ("deepl.quota_exceeded", False),
            500: ("deepl.unavailable", True),
            529: ("deepl.unavailable", True),
        }
        for status, (code, retryable) in cases.items():
            with self.subTest(status=status):
                self.assert_failure(
                    code,
                    lambda status=status: self.adapter(
                        Transport(BASELINE.HTTPResult(status, (), b"")),
                    ).acquire(job(), policy(), evidence_authority=HmacAuthority()),
                    retryable=retryable,
                )

    def test_malformed_or_duplicate_language_capabilities_block(self):
        malformed = response([{
            "lang": "fi", "usable_as_source": "yes",
            "usable_as_target": True, "status": "stable",
        }])
        duplicate = response([
            {
                "lang": "FI", "usable_as_source": True,
                "usable_as_target": True, "status": "stable",
            },
            {
                "lang": "fi", "usable_as_source": True,
                "usable_as_target": True, "status": "stable",
            },
        ])
        for value in (malformed, duplicate):
            with self.subTest(body=value.body):
                self.assert_failure(
                    "deepl.languages_invalid",
                    lambda value=value: self.adapter(Transport(value)).acquire(
                        job(), policy(), evidence_authority=HmacAuthority(),
                    ),
                    retryable=True,
                )

    def test_response_header_and_size_violations_block(self):
        oversized = BASELINE.HTTPResult(
            200, (("Content-Type", "application/json"),),
            b"x" * (BASELINE.MAX_RESPONSE_BYTES + 1),
        )
        wrong_type = response([], content_type="text/html")
        duplicate = BASELINE.HTTPResult(
            200,
            (("Content-Type", "application/json"),
             ("content-type", "application/json")),
            b"[]",
        )
        for value, code in (
            (oversized, "deepl.response_too_large"),
            (wrong_type, "deepl.response_headers"),
            (duplicate, "deepl.response_headers"),
        ):
            with self.subTest(code=code):
                self.assert_failure(
                    code,
                    lambda value=value: self.adapter(Transport(value)).acquire(
                        job(), policy(), evidence_authority=HmacAuthority(),
                    ),
                    retryable=True,
                )

    def test_authentication_is_validated_without_secret_disclosure(self):
        secret = "secret\nheader"
        adapter = BASELINE.DeepLBaselineAdapter(
            "pro", lambda: secret, transport=Transport(),
        )
        self.assert_failure(
            "deepl.authentication",
            lambda: adapter.acquire(
                job(), policy(), evidence_authority=HmacAuthority(),
            ),
            retryable=False,
        )
        self.assertNotIn(secret, repr(adapter))

    def test_request_larger_than_official_limit_blocks_before_post(self):
        transport = Transport(languages())
        adapter = self.adapter(transport)
        previous = BASELINE.MAX_REQUEST_BYTES
        BASELINE.MAX_REQUEST_BYTES = 10
        try:
            self.assert_failure(
                "deepl.request_too_large",
                lambda: adapter.acquire(
                    job(), policy(), evidence_authority=HmacAuthority(),
                ),
                retryable=False,
            )
        finally:
            BASELINE.MAX_REQUEST_BYTES = previous
        self.assertEqual([call[0] for call in transport.calls], ["GET"])

    def test_lawful_maltese_fixture_binds_rights_source_locale_and_target(self):
        payload = job("mt-MT")
        text = "Kabbar in-negozju tiegħek b’mod naturali."
        fixture = {
            "schema": BASELINE.FIXTURE_EVIDENCE_SCHEMA,
            "fixture_id": "deepl-maltese-case-1",
            "fixture_revision": "2026-09-08",
            "supplier_id": "customer-owned-evidence",
            "rights_basis": "licensed",
            "rights_evidence_sha256": hashlib.sha256(b"license").hexdigest(),
            "source_sha256": payload["source"]["sha256"],
            "target_locale": "mt-MT",
            "target_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        authority = HmacAuthority()
        acquired = BASELINE.create_lawful_fixture_acquisition(
            payload, text, policy(), fixture, evidence_authority=authority,
        )
        self.assertEqual(
            acquired.artifact["provenance"]["method"], "lawful_fixture",
        )
        self.assertNotIn("target_text", acquired.evidence)
        self.assertEqual(
            BENCHMARK._validate_baseline(
                WORKER._validated_job(payload), acquired.artifact,
                policy(), authority,
            ),
            acquired.artifact,
        )

    def test_lawful_fixture_tampering_blocks_fail_closed(self):
        payload = job("mt-MT")
        text = "Kabbar in-negozju tiegħek."
        fixture = {
            "schema": BASELINE.FIXTURE_EVIDENCE_SCHEMA,
            "fixture_id": "case-1",
            "fixture_revision": "revision-1",
            "supplier_id": "supplier-1",
            "rights_basis": "permission",
            "rights_evidence_sha256": hashlib.sha256(b"permission").hexdigest(),
            "source_sha256": payload["source"]["sha256"],
            "target_locale": "mt-MT",
            "target_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        changes = {
            "schema": "wrong",
            "rights_basis": "assumed",
            "source_sha256": "0" * 64,
            "target_locale": "fi-FI",
            "target_sha256": "1" * 64,
            "rights_evidence_sha256": "invalid",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(fixture)
                changed[field] = value
                self.assert_failure(
                    "fixture.evidence_invalid",
                    lambda changed=changed: BASELINE.create_lawful_fixture_acquisition(
                        payload, text, policy(), changed,
                        evidence_authority=HmacAuthority(),
                    ),
                    retryable=False,
                )

    def test_attestation_failure_blocks_without_returning_baseline(self):
        class RejectingAuthority(HmacAuthority):
            def verify(self, payload, signature):
                return False

        self.assert_failure(
            "deepl.attestation_failed",
            lambda: self.adapter(Transport(languages(), translation())).acquire(
                job(), policy(), evidence_authority=RejectingAuthority(),
            ),
            retryable=False,
        )

    def test_request_guard_runs_before_credentials_and_transport(self):
        events = []

        class GuardedTransport(Transport):
            def request(self, *args, **kwargs):
                events.append("transport")
                return super().request(*args, **kwargs)

        transport = GuardedTransport(languages(), translation())

        def credentials():
            events.append("credentials")
            return "private-api-key"

        adapter = BASELINE.DeepLBaselineAdapter(
            "pro", credentials, transport=transport,
            operation_guard=lambda: events.append("guard"),
        )
        adapter.acquire(job(), policy(), evidence_authority=HmacAuthority())
        self.assertEqual(events, [
            "guard", "credentials", "transport",
            "guard", "credentials", "transport",
        ])

    def test_failed_request_guard_blocks_before_credentials_and_transport(self):
        events = []

        def guard():
            events.append("guard")
            raise RuntimeError("lease lost")

        adapter = BASELINE.DeepLBaselineAdapter(
            "pro", lambda: events.append("credentials") or "key",
            transport=Transport(languages()), operation_guard=guard,
        )
        self.assert_failure(
            "deepl.operation_guard_failed",
            lambda: adapter.acquire(
                job(), policy(), evidence_authority=HmacAuthority(),
            ),
            retryable=True,
        )
        self.assertEqual(events, ["guard"])

    def test_durable_store_reuses_exact_acquisition_after_restart(self):
        payload = job()
        benchmark_policy = policy()
        authority = HmacAuthority()
        transport = Transport(languages(), translation())
        adapter = self.adapter(transport)
        calls = []

        with sqlite3.connect(":memory:") as connection:
            store = BASELINE.BaselineAcquisitionStore(connection)

            def acquire():
                calls.append("acquire")
                return adapter.acquire(
                    payload, benchmark_policy, evidence_authority=authority,
                )

            first = BASELINE.resolve_baseline_acquisition(
                store, payload, benchmark_policy, "deepl-pro",
                acquire, evidence_authority=authority, now=100,
            )
            restarted = BASELINE.BaselineAcquisitionStore(connection)
            second = BASELINE.resolve_baseline_acquisition(
                restarted, payload, benchmark_policy, "deepl-pro",
                acquire, evidence_authority=authority, now=200,
            )

        self.assertEqual(first, second)
        self.assertEqual(calls, ["acquire"])
        self.assertEqual([call[0] for call in transport.calls], ["GET", "POST"])

    def test_store_binding_changes_are_clean_misses(self):
        benchmark_policy = policy()
        payload = job(benchmark_policy=benchmark_policy)
        authority = HmacAuthority()
        acquisition = self.adapter(
            Transport(languages(), translation()),
        ).acquire(payload, benchmark_policy, evidence_authority=authority)
        changed_policy = policy(baseline_version="current-api-2026-09-09")
        changed_job = job(
            case_index=1, benchmark_policy=benchmark_policy,
        )
        with sqlite3.connect(":memory:") as connection:
            store = BASELINE.BaselineAcquisitionStore(connection)
            store.save(
                payload, benchmark_policy, "deepl-pro", acquisition,
                evidence_authority=authority, now=100,
            )
            self.assertIsNone(store.load(
                payload, benchmark_policy, "deepl-free",
                evidence_authority=authority,
            ))
            self.assertIsNone(store.load(
                changed_job, benchmark_policy, "deepl-pro",
                evidence_authority=authority,
            ))
            self.assertIsNone(store.load(
                payload, changed_policy, "deepl-pro",
                evidence_authority=authority,
            ))

    def test_durable_store_reuses_lawful_fixture_without_callback(self):
        payload = job("mt-MT")
        benchmark_policy = policy()
        authority = HmacAuthority()
        text = "Kabbar in-negozju tiegħek b’mod naturali."
        fixture = BASELINE.create_lawful_fixture_acquisition(
            payload, text, benchmark_policy,
            {
                "schema": BASELINE.FIXTURE_EVIDENCE_SCHEMA,
                "fixture_id": "deepl-maltese-case-1",
                "fixture_revision": "2026-09-08",
                "supplier_id": "customer-owned-evidence",
                "rights_basis": "licensed",
                "rights_evidence_sha256": hashlib.sha256(b"license").hexdigest(),
                "source_sha256": payload["source"]["sha256"],
                "target_locale": "mt-MT",
                "target_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            evidence_authority=authority,
        )
        calls = []
        with sqlite3.connect(":memory:") as connection:
            store = BASELINE.BaselineAcquisitionStore(connection)
            store.save(
                payload, benchmark_policy, "licensed-fixture-2026-09",
                fixture, evidence_authority=authority, now=100,
            )
            result = BASELINE.resolve_baseline_acquisition(
                store, payload, benchmark_policy,
                "licensed-fixture-2026-09",
                lambda: calls.append("acquire"),
                evidence_authority=authority,
            )
        self.assertEqual(result, fixture)
        self.assertEqual(calls, [])

    def test_store_tampering_blocks_before_reacquisition(self):
        payload = job()
        benchmark_policy = policy()
        authority = HmacAuthority()
        acquisition = self.adapter(
            Transport(languages(), translation()),
        ).acquire(payload, benchmark_policy, evidence_authority=authority)
        called = []
        with sqlite3.connect(":memory:") as connection:
            store = BASELINE.BaselineAcquisitionStore(connection)
            store.save(
                payload, benchmark_policy, "deepl-pro", acquisition,
                evidence_authority=authority, now=100,
            )
            connection.execute("""
                UPDATE benchmark_baseline_acquisitions
                SET evidence_json = '{}'
            """)
            connection.commit()
            self.assert_failure(
                "baseline.store.state_invalid",
                lambda: BASELINE.resolve_baseline_acquisition(
                    store, payload, benchmark_policy, "deepl-pro",
                    lambda: called.append("acquire"),
                    evidence_authority=authority,
                ),
                retryable=False,
            )
        self.assertEqual(called, [])

    def test_store_rejects_foreign_authority_and_conflicting_output(self):
        payload = job()
        benchmark_policy = policy()
        authority = HmacAuthority()
        first = self.adapter(
            Transport(languages(), translation("Ensimmäinen.")),
        ).acquire(payload, benchmark_policy, evidence_authority=authority)
        second = self.adapter(
            Transport(languages(), translation("Toinen.")),
        ).acquire(payload, benchmark_policy, evidence_authority=authority)
        with sqlite3.connect(":memory:") as connection:
            store = BASELINE.BaselineAcquisitionStore(connection)
            store.save(
                payload, benchmark_policy, "deepl-pro", first,
                evidence_authority=authority, now=100,
            )
            self.assert_failure(
                "baseline.store.conflict",
                lambda: store.save(
                    payload, benchmark_policy, "deepl-pro", second,
                    evidence_authority=authority, now=101,
                ),
                retryable=False,
            )
            self.assert_failure(
                "baseline.store.acquisition_invalid",
                lambda: store.load(
                    payload, benchmark_policy, "deepl-pro",
                    evidence_authority=HmacAuthority(b"foreign-key"),
                ),
                retryable=False,
            )

    def test_identical_concurrent_store_writes_converge(self):
        payload = job()
        benchmark_policy = policy()
        authority = HmacAuthority()
        acquisition = self.adapter(
            Transport(languages(), translation()),
        ).acquire(payload, benchmark_policy, evidence_authority=authority)
        with sqlite3.connect(":memory:") as connection:
            store = BASELINE.BaselineAcquisitionStore(connection)
            first = store.save(
                payload, benchmark_policy, "deepl-pro", acquisition,
                evidence_authority=authority, now=100,
            )
            second = store.save(
                payload, benchmark_policy, "deepl-pro", acquisition,
                evidence_authority=authority, now=101,
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM benchmark_baseline_acquisitions"
            ).fetchone()[0]
        self.assertEqual(first, second)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
