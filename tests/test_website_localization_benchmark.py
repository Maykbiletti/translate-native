from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import hmac
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLANNER = load("blun_test_benchmark_planner", ROOT / "integrations" / "website_localization.py")
WORKER = load("blun_test_benchmark_worker", ROOT / "integrations" / "website_localization_worker.py")
BENCHMARK = load("blun_test_website_localization_benchmark", ROOT / "integrations" / "website_localization_benchmark.py")
SUITE = load("blun_test_website_localization_benchmark_suite", ROOT / "integrations" / "website_localization_benchmark_suite.py")
CAMPAIGN = load("blun_test_website_localization_benchmark_campaign", ROOT / "integrations" / "website_localization_benchmark_campaign.py")
FOREIGN_CAMPAIGN = load("blun_test_foreign_benchmark_campaign", ROOT / "integrations" / "website_localization_benchmark_campaign.py")
BASELINE_ADAPTER = load("blun_test_cross_module_deepl_baseline", ROOT / "integrations" / "website_localization_deepl_baseline.py")
BENCHMARK_RUNTIME = load(
    "blun_test_website_localization_benchmark_runtime",
    ROOT / "integrations" / "website_localization_benchmark_runtime.py",
)
SUITE_MANIFEST = SUITE.manifest()


TARGETS = {
    "mt-MT": {
        "candidate": "Kabbar in-negozju tiegħek ma’ BLUN.",
        "baseline": "Ibni n-negozju tiegħek ma’ BLUN.",
        "reference": "Kabbar in-negozju tiegħek b’mod naturali f’Malta.",
        "audience": "Sidien ta’ negozji żgħar f’Malta",
    },
    "fi-FI": {
        "candidate": "Kasvata yritystäsi BLUNin avulla.",
        "baseline": "Rakenna yrityksesi BLUNin kanssa.",
        "reference": "Kasvata yritystäsi luontevasti Suomessa.",
        "audience": "Suomalaiset pienyrittäjät",
    },
}


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
        "candidate_glossary_version": "blun-glossary-3",
        "candidate_policy_version": "native-web-2",
        "attestation_algorithm": "hmac-sha256-test",
        "attestation_key_id": "benchmark-test-key-1",
        "baseline_id": "deepl-official-api",
        "baseline_version": "fixture-2026-08-30",
        "reviewer_id": "independent-native-panel",
        "reviewer_version": "2026-08-30",
        "native_reference_revision": "qualified-native-reference-1",
        "native_reference_verifier_id": "qualified-review-registry",
        "native_reference_verifier_version": "2026-08-30",
        "valid_until": 1_800_000_000,
        "required_locales": ("mt-MT", "fi-FI"),
        "required_content_types": ("commercial",),
        "minimum_cases_per_locale": len(SUITE.SOURCE_CASES),
        "minimum_cases_per_content_type": 8,
        "minimum_decisive_rate": 0.75,
        "minimum_candidate_win_rate": 0.60,
        "maximum_one_sided_p": 0.05,
    }
    values.update(overrides)
    return BENCHMARK.BenchmarkPolicy(**values)


def job(locale="mt-MT", suffix="1"):
    final = str(suffix).rsplit("-", 1)[-1]
    case_index = int(final) if final.isdigit() else 0
    case = SUITE.SOURCE_CASES[case_index % len(SUITE.SOURCE_CASES)].as_payload()
    return PLANNER.plan_website_localization(
        source_id=case["source_id"],
        source_revision=case["source_revision"],
        source_text=case["source_text"],
        source_locale=case["source_locale"],
        content_type=case["content_type"],
        glossary_version="blun-glossary-3",
        policy_version="native-web-2",
        provider_id="customer-llm",
        model_id="king",
        model_version="2026-08-30",
        software_version="6.43.0-dev",
        target_locales=[locale],
    ).jobs[0].as_payload()


def fixture_copy(locale):
    if locale in TARGETS:
        return TARGETS[locale]
    profile = next(
        item for item in PLANNER.EU_OFFICIAL_LOCALES if item.locale == locale
    )
    return {
        "candidate": f"{profile.native_name} candidate contract fixture.",
        "baseline": f"{profile.native_name} baseline contract fixture.",
        "reference": f"{profile.native_name} reference contract fixture.",
        "audience": f"{profile.native_name} contract-test audience",
    }


def assets(locale="mt-MT"):
    target = "negozju" if locale == "mt-MT" else (
        "yritys" if locale == "fi-FI" else fixture_copy(locale)["candidate"]
    )
    return WORKER.LocalizationAssets(
        glossary_version="blun-glossary-3",
        policy_version="native-web-2",
        audience=fixture_copy(locale)["audience"],
        tone_profile="Natural, confident, warm, concise, and never inflated",
        glossary=(WORKER.GlossaryTerm("business", target),),
        protected_terms=("BLUN",),
    )


def candidate_result(payload, text=None):
    text = _target_fixture(payload, "candidate") if text is None else text
    phase = lambda name: {
        "phase": name,
        "request_sha256": hashlib.sha256((name + "-request").encode()).hexdigest(),
        "response_sha256": hashlib.sha256((name + "-response").encode()).hexdigest(),
        "status": "PASS",
    }
    return {
        "schema": WORKER.RESULT_SCHEMA,
        "worker_schema": WORKER.WORKER_SCHEMA,
        "job_id": payload["job_id"],
        "source_sha256": payload["source"]["sha256"],
        "target_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_locale": payload["source"]["locale"],
        "target_locale": payload["target"]["locale"],
        "content_type": payload["content_type"],
        "glossary_version": payload["glossary_version"],
        "policy_version": payload["policy_version"],
        "provider": payload["provider"],
        "software_version": payload["software_version"],
        "candidate": text,
        "quality_passes": [phase(name) for name in ("transcreation", "target_native", "source_fidelity")],
        "integrity": {"status": "PASS", "guard": "translate-native-structure-and-token-gate"},
        "review_confidence": {"target_native": "high", "source_fidelity": "high"},
        "quality_profile": {
            "locale": payload["target"]["locale"],
            "version": payload["target"]["quality_profile_version"],
            "sha256": payload["target"]["quality_profile_sha256"],
        },
        "human_review_required": payload["content_type"] == "legal",
        "independent_review_required": False,
        "release_required": True,
    }


def baseline(
    payload, text=None, *, method="lawful_fixture", evidence_id=None,
    authority=None, benchmark_policy=None,
):
    text = _target_fixture(payload, "baseline") if text is None else text
    evidence_id = evidence_id or "fixture-" + payload["job_id"]
    provenance = {
        "schema": BENCHMARK.BASELINE_PROVENANCE_SCHEMA,
        "method": method,
        "evidence_id": evidence_id,
        "evidence_sha256": hashlib.sha256(
            (evidence_id + payload["source"]["sha256"] + text).encode()
        ).hexdigest(),
    }
    return BENCHMARK.create_baseline_artifact(
        payload, text, benchmark_policy or policy(), provenance,
        evidence_authority=authority or HmacBenchmarkAuthority(),
    )


def _target_fixture(payload, variant):
    locale = payload["target"]["locale"]
    phrase = fixture_copy(locale)[variant]
    source_id = payload["source"]["id"]
    if source_id.endswith("travel-marketing"):
        return (
            f'<section><h2>{phrase}</h2><p>{phrase} {{{{season}}}}.</p>'
            '<a href="https://example.test/routes">Route</a></section>'
        )
    if source_id.endswith("payments-ui"):
        return json.dumps(
            {"error": phrase, "retry": phrase}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
    repetitions = max(
        1,
        (len(payload["source"]["text"]) + len(phrase) - 1) // len(phrase),
    )
    return " ".join((phrase,) * repetitions)


def review_response(request, preference, defects=None):
    defects = {} if defects is None else defects
    variants = {}
    for label in ("A", "B"):
        variants[label] = {
            "blocking_defects": defects.get(label, {}).get("blocking", []),
            "major_defects": defects.get(label, {}).get("major", []),
        }
    return {
        "schema": BENCHMARK.REVIEW_SCHEMA,
        "phase": request.phase,
        "target_locale": request.target_locale,
        "blind_id": request.input["blind_id"],
        "preference": preference,
        "variants": variants,
    }


class PreferenceReviewer:
    def __init__(self, preferred_text, *, prefer="preferred", defects=None):
        self.preferred_text = preferred_text
        self.prefer = prefer
        self.defects = defects
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        by_text = {item["text"]: item["label"] for item in request.input["variants"]}
        label = by_text[self.preferred_text]
        if self.prefer == "other":
            label = "B" if label == "A" else "A"
        elif self.prefer == "tie":
            label = "tie"
        return review_response(request, label, self.defects)


class HmacBenchmarkAuthority:
    def __init__(
        self, key=b"isolated-benchmark-attestation-key-material",
        *, algorithm="hmac-sha256-test", key_id="benchmark-test-key-1",
    ):
        self.key = key
        self.algorithm = algorithm
        self.key_id = key_id

    def sign(self, payload):
        digest = hmac.new(self.key, payload, hashlib.sha256).digest()
        return BENCHMARK.BenchmarkSignature(
            algorithm=self.algorithm,
            key_id=self.key_id,
            signature=base64.b64encode(digest).decode("ascii"),
        )

    def verify(self, payload, signature):
        digest = hmac.new(self.key, payload, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("ascii")
        return (
            signature.algorithm == self.algorithm
            and signature.key_id == self.key_id
            and hmac.compare_digest(signature.signature, expected)
        )


class HmacNativeReferenceVerifier:
    def __init__(self, key=b"isolated-qualified-native-reference-key"):
        self.key = key

    def receipt(self, request):
        payload = BENCHMARK._canonical_json(request).encode("utf-8")
        return base64.b64encode(
            hmac.new(self.key, payload, hashlib.sha256).digest()
        ).decode("ascii")

    def verify(self, request, receipt):
        return hmac.compare_digest(self.receipt(request), receipt)


class CampaignAuthority(HmacBenchmarkAuthority):
    def sign(self, payload):
        digest = hmac.new(self.key, payload, hashlib.sha256).digest()
        return CAMPAIGN._BENCHMARK.BenchmarkSignature(
            algorithm=self.algorithm,
            key_id=self.key_id,
            signature=base64.b64encode(digest).decode("ascii"),
        )


class CountingCampaignAuthority(CampaignAuthority):
    def __init__(self):
        super().__init__()
        self.sign_calls = 0
        self.verify_calls = 0

    def sign(self, payload):
        self.sign_calls += 1
        return super().sign(payload)

    def verify(self, payload, signature):
        self.verify_calls += 1
        return super().verify(payload, signature)


class CampaignNativeReferenceVerifier(HmacNativeReferenceVerifier):
    def receipt(self, request):
        payload = CAMPAIGN._BENCHMARK._canonical_json(request).encode("utf-8")
        return base64.b64encode(
            hmac.new(self.key, payload, hashlib.sha256).digest()
        ).decode("ascii")


class CampaignCandidateReviewer:
    def __init__(self):
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        marker = fixture_copy(request.target_locale)["candidate"]
        preferred = next(
            item["label"] for item in request.input["variants"]
            if marker in item["text"]
        )
        return review_response(request, preferred)


class RuntimeCandidateProvider:
    def __init__(self):
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        locale = request.input["target"]["locale"]
        if request.phase == "transcreation":
            source = request.input["source"]["text"]
            phrase = fixture_copy(locale)["candidate"]
            repetitions = max(1, (len(source) + len(phrase) - 1) // len(phrase))
            return {
                "schema": BENCHMARK_RUNTIME._CANDIDATE._WORKER.CANDIDATE_SCHEMA,
                "phase": "transcreation",
                "locale": locale,
                "candidate": " ".join((phrase,) * repetitions),
            }
        return {
            "schema": BENCHMARK_RUNTIME._CANDIDATE._WORKER.REVIEW_SCHEMA,
            "phase": request.phase,
            "locale": locale,
            "status": "PASS",
            "confidence": "high",
            "blocking_defects": [],
            "major_defects": [],
        }


class RetryOnceCampaignReviewer(CampaignCandidateReviewer):
    def __init__(self):
        super().__init__()
        self.failed = False

    def review(self, request):
        if not self.failed:
            self.failed = True
            self.requests.append(request)
            raise TimeoutError("private reviewer failure")
        return super().review(request)


class RetryFidelityOnceCampaignReviewer(CampaignCandidateReviewer):
    def __init__(self):
        super().__init__()
        self.failed = False

    def review(self, request):
        if request.phase == "source_fidelity" and not self.failed:
            self.failed = True
            self.requests.append(request)
            raise TimeoutError("private fidelity reviewer failure")
        return super().review(request)


def native_reference(
    payload, benchmark_policy, verifier, authority, text=None,
    *, reviewer_id="qualified-native-reviewer-17",
    reviewer_version="credential-2026-08-30",
):
    text = _target_fixture(payload, "reference") if text is None else text
    request = BENCHMARK.native_reference_verification_request(
        payload, text, benchmark_policy,
        reviewer_id=reviewer_id, reviewer_version=reviewer_version,
    )
    return BENCHMARK.create_native_reference_artifact(
        payload, text, benchmark_policy,
        reviewer_id=reviewer_id,
        reviewer_version=reviewer_version,
        qualification_receipt=verifier.receipt(request),
        native_reference_verifier=verifier,
        evidence_authority=authority,
    )


def campaign_policy(**overrides):
    return CAMPAIGN._BENCHMARK.BenchmarkPolicy(
        **{**CAMPAIGN.asdict(policy()), **overrides},
    )


def campaign_inputs(payload, benchmark_policy, verifier, authority):
    benchmark = CAMPAIGN._BENCHMARK
    candidate = candidate_result(payload)
    baseline_text = _target_fixture(payload, "baseline")
    evidence_id = "fixture-" + payload["job_id"]
    baseline_artifact = benchmark.create_baseline_artifact(
        payload,
        baseline_text,
        benchmark_policy,
        {
            "schema": benchmark.BASELINE_PROVENANCE_SCHEMA,
            "method": "lawful_fixture",
            "evidence_id": evidence_id,
            "evidence_sha256": hashlib.sha256(
                (evidence_id + payload["source"]["sha256"] + baseline_text).encode()
            ).hexdigest(),
        },
        evidence_authority=authority,
    )
    reference_text = _target_fixture(payload, "reference")
    request = benchmark.native_reference_verification_request(
        payload,
        reference_text,
        benchmark_policy,
        reviewer_id="qualified-native-reviewer-17",
        reviewer_version="credential-2026-08-30",
    )
    native_artifact = benchmark.create_native_reference_artifact(
        payload,
        reference_text,
        benchmark_policy,
        reviewer_id="qualified-native-reviewer-17",
        reviewer_version="credential-2026-08-30",
        qualification_receipt=verifier.receipt(request),
        native_reference_verifier=verifier,
        evidence_authority=authority,
    )
    return CAMPAIGN.BenchmarkCaseInputs(
        candidate_result=candidate,
        baseline_artifact=baseline_artifact,
        assets=assets(payload["target"]["locale"]),
        native_reference_artifact=native_artifact,
    )


class WebsiteLocalizationBenchmarkTests(unittest.TestCase):
    key = b"benchmark-host-secret-key-material-32"

    def setUp(self):
        self.authority = HmacBenchmarkAuthority()
        self.native_reference_verifier = HmacNativeReferenceVerifier()

    def run_benchmark(self, *args, **kwargs):
        kwargs["evidence_authority"] = self.authority
        kwargs.setdefault("native_reference_verifier", self.native_reference_verifier)
        kwargs.setdefault(
            "native_reference_artifact",
            native_reference(
                args[0], args[4], self.native_reference_verifier, self.authority,
            ),
        )
        return BENCHMARK.run_blind_benchmark_case(*args, **kwargs)

    def summarize(self, benchmark_policy, results):
        return BENCHMARK.summarize_benchmark(
            benchmark_policy, results, evidence_authority=self.authority,
        )

    def run_case(
        self, locale="mt-MT", suffix="1", *, prefer="preferred",
        baseline_text=None, benchmark_policy=None,
    ):
        benchmark_policy = benchmark_policy or policy()
        payload = job(locale, suffix)
        result = candidate_result(payload)
        reviewer = PreferenceReviewer(result["candidate"], prefer=prefer)
        outcome = self.run_benchmark(
            payload,
            result,
            baseline(
                payload, baseline_text, benchmark_policy=benchmark_policy,
            ),
            assets(locale),
            benchmark_policy,
            reviewer,
            blinding_key=self.key,
        )
        return outcome, reviewer

    def test_runs_two_ordered_origin_blind_reviews(self):
        outcome, reviewer = self.run_case()
        source_text = job()["source"]["text"]
        self.assertEqual([item.phase for item in reviewer.requests], ["target_native", "source_fidelity"])
        native = json.dumps(reviewer.requests[0].as_payload(), ensure_ascii=False)
        fidelity = json.dumps(reviewer.requests[1].as_payload(), ensure_ascii=False)
        self.assertNotIn(source_text, native)
        self.assertNotIn("customer-llm", native + fidelity)
        self.assertNotIn("deepl", (native + fidelity).lower())
        self.assertNotIn("benchmark-test-key", native + fidelity)
        self.assertNotIn("hmac-sha256-test", native + fidelity)
        self.assertNotIn("lawful_fixture", native + fidelity)
        self.assertNotIn("official_api", native + fidelity)
        self.assertNotIn("evidence_id", native + fidelity)
        self.assertNotIn("qualified-native-reviewer", native + fidelity)
        self.assertNotIn(TARGETS["mt-MT"]["reference"], native + fidelity)
        self.assertNotIn('"origin"', native + fidelity)
        self.assertNotIn('"case_key"', native)
        self.assertNotIn('"adversarial_tags"', native)
        self.assertIn('"case_key"', fidelity)
        self.assertIn('"adversarial_tags"', fidelity)
        self.assertIn(source_text, fidelity)
        self.assertEqual(
            reviewer.requests[0].input["benchmark_suite"]["sha256"],
            SUITE_MANIFEST["sha256"],
        )
        self.assertEqual(outcome["winner"], "candidate")

    def test_qualified_native_reference_is_verified_bound_and_text_free(self):
        payload = job()
        benchmark_policy = policy()
        artifact = native_reference(
            payload, benchmark_policy, self.native_reference_verifier,
            self.authority,
        )
        self.assertEqual(artifact["schema"], BENCHMARK.NATIVE_REFERENCE_SCHEMA)
        self.assertEqual(
            artifact["request"]["qualification"]["method"],
            "qualified_native_human",
        )
        self.assertEqual(artifact["request"]["source"], {
            "locale": payload["source"]["locale"],
            "text": payload["source"]["text"],
            "sha256": payload["source"]["sha256"],
        })
        self.assertEqual(artifact["request"]["localization_policy"], {
            "glossary_version": "blun-glossary-3",
            "policy_version": "native-web-2",
        })
        reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
        outcome = self.run_benchmark(
            payload, candidate_result(payload), baseline(payload), assets(),
            benchmark_policy, reviewer, blinding_key=self.key,
            native_reference_artifact=artifact,
        )
        self.assertEqual(outcome["native_reference"]["revision"], "qualified-native-reference-1")
        serialized = json.dumps(outcome, ensure_ascii=False)
        self.assertNotIn(TARGETS["mt-MT"]["reference"], serialized)
        self.assertNotIn("qualified-native-reviewer-17", serialized)
        self.assertRegex(
            outcome["native_reference"]["evidence_sha256"], r"^[0-9a-f]{64}$",
        )

    def test_missing_replayed_or_unverified_native_reference_blocks_before_review(self):
        payload = job("mt-MT", "1")
        benchmark_policy = policy()
        artifact = native_reference(
            payload, benchmark_policy, self.native_reference_verifier,
            self.authority,
        )
        cases = [
            (None, self.native_reference_verifier, "benchmark.native_reference.invalid"),
            (
                artifact,
                HmacNativeReferenceVerifier(key=b"different-qualified-review-key"),
                "benchmark.native_reference.rejected",
            ),
        ]
        for reference_artifact, verifier, code in cases:
            with self.subTest(code=code):
                reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    self.run_benchmark(
                        payload, candidate_result(payload), baseline(payload),
                        assets(), benchmark_policy, reviewer,
                        blinding_key=self.key,
                        native_reference_artifact=reference_artifact,
                        native_reference_verifier=verifier,
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(reviewer.requests, [])

        other_payload = job("mt-MT", "2")
        reviewer = PreferenceReviewer(candidate_result(other_payload)["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.run_benchmark(
                other_payload, candidate_result(other_payload), baseline(other_payload),
                assets(), benchmark_policy, reviewer, blinding_key=self.key,
                native_reference_artifact=artifact,
            )
        self.assertEqual(caught.exception.code, "benchmark.native_reference.binding_mismatch")
        self.assertEqual(reviewer.requests, [])

    def test_native_reference_verifier_cannot_mutate_its_request(self):
        payload = job()
        benchmark_policy = policy()
        request = BENCHMARK.native_reference_verification_request(
            payload, TARGETS["mt-MT"]["reference"], benchmark_policy,
            reviewer_id="qualified-native-reviewer-17",
            reviewer_version="credential-2026-08-30",
        )

        class MutatingVerifier:
            def verify(self, received, receipt):
                received["target_text"] = "changed"
                return True

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.create_native_reference_artifact(
                payload, TARGETS["mt-MT"]["reference"], benchmark_policy,
                reviewer_id="qualified-native-reviewer-17",
                reviewer_version="credential-2026-08-30",
                qualification_receipt=self.native_reference_verifier.receipt(request),
                native_reference_verifier=MutatingVerifier(),
                evidence_authority=self.authority,
            )
        self.assertEqual(
            caught.exception.code,
            "benchmark.native_reference.verifier_mutated_request",
        )

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.native_reference_verification_request(
                payload, TARGETS["mt-MT"]["reference"], benchmark_policy,
                reviewer_id=benchmark_policy.reviewer_id,
                reviewer_version="credential-2026-08-30",
            )
        self.assertEqual(
            caught.exception.code,
            "benchmark.native_reference.independence_invalid",
        )

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(
                policy(native_reference_verifier_id="customer-llm"), [],
            )
        self.assertEqual(caught.exception.code, "benchmark.policy.invalid")

    def test_blinding_is_reproducible_and_keyed(self):
        payload = job()
        result = candidate_result(payload)
        base = baseline(payload)
        first_reviewer = PreferenceReviewer(result["candidate"])
        first = self.run_benchmark(
            payload, result, base, assets(), policy(), first_reviewer, blinding_key=self.key,
        )
        second_reviewer = PreferenceReviewer(result["candidate"])
        second = self.run_benchmark(
            payload, result, base, assets(), policy(), second_reviewer, blinding_key=self.key,
        )
        self.assertEqual(first, second)
        self.assertEqual(first_reviewer.requests[0].input["variants"], second_reviewer.requests[0].input["variants"])
        commitments = {first["blind_commitment_sha256"]}
        for index in range(1, 8):
            reviewer = PreferenceReviewer(result["candidate"])
            changed = self.run_benchmark(
                payload, result, base, assets(), policy(), reviewer,
                blinding_key=(f"different-key-{index:02d}".encode() * 3)[:32],
            )
            commitments.add(changed["blind_commitment_sha256"])
        self.assertGreater(len(commitments), 1)

    def test_maltese_and_finnish_contexts_preserve_native_profiles(self):
        for locale in ("mt-MT", "fi-FI"):
            with self.subTest(locale=locale):
                outcome, reviewer = self.run_case(locale)
                request = reviewer.requests[0]
                self.assertEqual(request.target_locale, locale)
                self.assertEqual(request.input["target"]["native_name"], "Malti" if locale == "mt-MT" else "suomi")
                self.assertEqual(
                    request.input["quality_profile"],
                    PLANNER.quality_profile_for(locale),
                )
                marker = "għ" if locale == "mt-MT" else "ä"
                self.assertIn(marker, TARGETS[locale]["candidate"])
                self.assertEqual(outcome["winner"], "candidate")

    def test_wrong_baseline_binding_blocks_before_review(self):
        payload = job()
        base = baseline(payload)
        for field, value in (
            ("source_sha256", "0" * 64),
            ("target_locale", "fi-FI"),
            ("baseline_version", "stale"),
            ("target_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                changed = {**base, field: value}
                reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    self.run_benchmark(
                        payload, candidate_result(payload), changed, assets(), policy(), reviewer,
                        blinding_key=self.key,
                    )
                self.assertEqual(caught.exception.code, "benchmark.attestation.payload_mismatch")
                self.assertEqual(reviewer.requests, [])

    def test_baseline_requires_attested_lawful_provenance_before_review(self):
        payload = job()
        for method in ("official_api", "lawful_fixture"):
            with self.subTest(method=method):
                artifact = baseline(payload, method=method, authority=self.authority)
                reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
                outcome = self.run_benchmark(
                    payload, candidate_result(payload), artifact, assets(),
                    policy(), reviewer, blinding_key=self.key,
                )
                self.assertEqual(outcome["winner"], "candidate")
                self.assertRegex(
                    outcome["baseline"]["provenance_sha256"], r"^[0-9a-f]{64}$",
                )
                self.assertRegex(
                    outcome["baseline"]["evidence_sha256"], r"^[0-9a-f]{64}$",
                )

        artifact = baseline(payload, authority=self.authority)
        cases = []
        unsigned = copy.deepcopy(artifact)
        unsigned.pop("attestation")
        cases.append((unsigned, "benchmark.baseline.invalid"))
        changed = copy.deepcopy(artifact)
        changed["provenance"]["method"] = "scraped"
        cases.append((changed, "benchmark.attestation.payload_mismatch"))
        changed = copy.deepcopy(artifact)
        changed["provenance"]["evidence_sha256"] = "0" * 64
        cases.append((changed, "benchmark.attestation.payload_mismatch"))
        changed = copy.deepcopy(artifact)
        changed["attestation"]["signature"] = "0" * 64
        cases.append((changed, "benchmark.attestation.rejected"))
        for changed, code in cases:
            with self.subTest(code=code):
                reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    self.run_benchmark(
                        payload, candidate_result(payload), changed, assets(),
                        policy(), reviewer, blinding_key=self.key,
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(reviewer.requests, [])

        for method in ("scraped", "undocumented_endpoint", ""):
            with self.subTest(method=method):
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    baseline(payload, method=method, authority=self.authority)
                self.assertEqual(
                    caught.exception.code,
                    "benchmark.baseline.provenance_invalid",
                )

    def test_wrong_candidate_binding_blocks_before_review(self):
        payload = job()
        result = candidate_result(payload)
        result["target_sha256"] = "0" * 64
        reviewer = PreferenceReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.run_benchmark(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.candidate.binding_mismatch")
        self.assertEqual(reviewer.requests, [])

    def test_candidate_policy_mismatch_blocks_before_review(self):
        payload = job()
        result = candidate_result(payload)
        mismatches = {
            "candidate_provider_id": "other-provider",
            "candidate_model_id": "other-model",
            "candidate_model_version": "other-version",
            "candidate_software_version": "other-software",
            "candidate_worker_schema": "other-worker-schema",
            "candidate_glossary_version": "other-glossary",
            "candidate_policy_version": "other-policy",
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                reviewer = PreferenceReviewer(result["candidate"])
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    self.run_benchmark(
                        payload, result, baseline(payload), assets(),
                        policy(**{field: value}), reviewer, blinding_key=self.key,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "benchmark.candidate.policy_mismatch",
                )
                self.assertEqual(reviewer.requests, [])

    def test_substituted_candidate_quality_profile_blocks_before_review(self):
        payload = job()
        result = candidate_result(payload)
        result["quality_profile"]["version"] = "stale-profile"
        reviewer = PreferenceReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.run_benchmark(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.candidate.binding_mismatch")
        self.assertEqual(reviewer.requests, [])

    def test_unverified_candidate_phases_and_invalid_policy_block(self):
        payload = job()
        result = candidate_result(payload)
        result["quality_passes"][1]["status"] = "FAIL"
        reviewer = PreferenceReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.run_benchmark(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.candidate.invalid")
        self.assertEqual(reviewer.requests, [])

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(policy(minimum_cases_per_locale="six"), [])
        self.assertEqual(caught.exception.code, "benchmark.policy.invalid")

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(policy(suite_sha256="0" * 64), [])
        self.assertEqual(caught.exception.code, "benchmark.suite.version_mismatch")

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(policy(required_locales=("mt-MT",)), [])
        self.assertEqual(caught.exception.code, "benchmark.policy.invalid")

        for overrides in (
            {"required_content_types": ("unknown",)},
            {"required_content_types": ("commercial", 7)},
            {"required_content_types": ("commercial", "commercial")},
            {"minimum_cases_per_content_type": 9},
            {"valid_until": True},
            {"valid_until": 0},
            {"valid_until": 9_007_199_254_740_992},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(
                BENCHMARK.BenchmarkBlocked,
            ) as caught:
                self.summarize(policy(**overrides), [])
            self.assertEqual(caught.exception.code, "benchmark.policy.invalid")

    def test_preferred_variant_cannot_have_major_or_blocking_defect(self):
        payload = job()
        result = candidate_result(payload)

        class DefectiveReviewer(PreferenceReviewer):
            def review(self, request):
                self.requests.append(request)
                label = next(item["label"] for item in request.input["variants"] if item["text"] == self.preferred_text)
                finding = {"class": "nativeness", "excerpt": "test", "reason": "major defect"}
                return review_response(request, label, {label: {"major": [finding]}})

        reviewer = DefectiveReviewer(result["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.run_benchmark(
                payload, result, baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.review.invalid")

    def test_split_or_tied_judgment_is_not_a_win(self):
        payload = job()
        result = candidate_result(payload)

        class SplitReviewer(PreferenceReviewer):
            def review(self, request):
                self.requests.append(request)
                by_text = {item["text"]: item["label"] for item in request.input["variants"]}
                label = by_text[self.preferred_text]
                if request.phase == "source_fidelity":
                    label = "B" if label == "A" else "A"
                return review_response(request, label)

        outcome = self.run_benchmark(
            payload, result, baseline(payload), assets(), policy(), SplitReviewer(result["candidate"]),
            blinding_key=self.key,
        )
        self.assertEqual(outcome["winner"], "inconclusive")

        tied, _ = self.run_case(prefer="tie")
        self.assertEqual(tied["winner"], "inconclusive")

    def test_local_integrity_failure_cannot_be_a_candidate_win(self):
        payload = job("mt-MT", "2")
        broken = '<section><h2>Kabbar</h2><p>Kabbar.</p><a href="https://evil.example">Route</a></section>'
        result = candidate_result(payload, broken)
        base = baseline(payload)
        reviewer = PreferenceReviewer(broken)
        outcome = self.run_benchmark(
            payload, result, base, assets(), policy(), reviewer, blinding_key=self.key,
        )
        self.assertEqual(outcome["integrity"]["candidate"]["status"], "FAIL")
        self.assertEqual(outcome["winner"], "inconclusive")

    def test_case_result_retains_no_text_or_reviewer_prose(self):
        outcome, _ = self.run_case()
        serialized = json.dumps(outcome, ensure_ascii=False)
        self.assertNotIn("Kabbar", serialized)
        self.assertNotIn("Ibni", serialized)
        self.assertNotIn(job()["source"]["text"], serialized)
        self.assertNotIn("excerpt", serialized)
        self.assertNotIn("reason", serialized)

    def test_successful_early_lanes_do_not_authorize_an_eu_wide_claim(self):
        results = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES)):
                outcome, _ = self.run_case(locale, f"{locale}-{index}")
                results.append(outcome)
        report = self.summarize(policy(), results)
        self.assertFalse(report["superiority_claim_allowed"])
        self.assertEqual(report["status"], "BLOCK")
        self.assertEqual(report["configured_lanes_status"], "PASS")
        self.assertEqual(
            report["claim_block_reasons"],
            ["eu_target_locale_coverage_incomplete"],
        )
        self.assertFalse(report["claim_scope"]["complete"])
        self.assertEqual(report["claim_scope"]["source_languages"], ["en"])
        self.assertEqual(
            report["claim_scope"]["source_language_locales"], ["en-IE"],
        )
        self.assertEqual(
            set(report["claim_scope"]["missing_target_locales"]),
            set(BENCHMARK.EU_BENCHMARK_TARGET_LOCALES) - {"mt-MT", "fi-FI"},
        )
        self.assertEqual(report["candidate"], {
            "provider": {
                "id": "customer-llm",
                "model_id": "king",
                "model_version": "2026-08-30",
            },
            "software_version": "6.43.0-dev",
            "glossary_version": "blun-glossary-3",
            "policy_version": "native-web-2",
            "worker_schema": WORKER.WORKER_SCHEMA,
        })
        self.assertEqual(report["baseline"], {
            "id": "deepl-official-api",
            "version": "fixture-2026-08-30",
            "provenance_methods": ["lawful_fixture"],
        })
        self.assertRegex(report["baseline_evidence_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["native_references"]["revision"], "qualified-native-reference-1")
        self.assertEqual(report["native_references"]["verifier"], {
            "id": "qualified-review-registry",
            "version": "2026-08-30",
        })
        self.assertRegex(
            report["native_references"]["evidence_sha256"], r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            report["quality_profiles"],
            [
                {
                    "locale": locale,
                    "version": PLANNER.quality_profile_for(locale)["version"],
                    "sha256": PLANNER.quality_profile_for(locale)["sha256"],
                }
                for locale in ("mt-MT", "fi-FI")
            ],
        )
        for item in report["locales"]:
            self.assertEqual(item["candidate_wins"], len(SUITE.SOURCE_CASES))
            self.assertEqual(
                item["one_sided_sign_p"],
                BENCHMARK._one_sided_sign_p(
                    len(SUITE.SOURCE_CASES), len(SUITE.SOURCE_CASES),
                ),
            )
            self.assertTrue(item["suite_complete"])
            self.assertEqual(set(item["content_types"]), set(PLANNER.CONTENT_TYPES))
            self.assertGreaterEqual(item["long_form_cases"], 6)
            self.assertGreaterEqual(len(item["domains"]), 6)
            self.assertIn("marketing_calque", item["adversarial_tags"])
            self.assertEqual(len(item["content_type_lanes"]), 1)
            lane = item["content_type_lanes"][0]
            self.assertEqual(lane["content_type"], "commercial")
            self.assertEqual(lane["case_count"], 8)
            self.assertEqual(lane["status"], "PASS")
            self.assertTrue(all(
                axis["status"] == "PASS" for axis in lane["axes"]
            ))

    def test_only_complete_successful_eu_target_scope_allows_claim(self):
        # Scripted fixtures prove report gating, not linguistic quality.
        benchmark_policy = policy(
            required_locales=BENCHMARK.EU_BENCHMARK_TARGET_LOCALES,
        )
        results = []
        for locale in BENCHMARK.EU_BENCHMARK_TARGET_LOCALES:
            for index in range(len(SUITE.SOURCE_CASES)):
                outcome, _ = self.run_case(
                    locale,
                    f"full-{locale}-{index}",
                    benchmark_policy=benchmark_policy,
                )
                results.append(outcome)
        report = self.summarize(benchmark_policy, results)
        self.assertTrue(report["superiority_claim_allowed"])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["configured_lanes_status"], "PASS")
        self.assertEqual(report["claim_block_reasons"], [])
        self.assertEqual(report["decision_policy"], {
            "minimum_cases_per_locale": len(SUITE.SOURCE_CASES),
            "required_content_types": ["commercial"],
            "minimum_cases_per_content_type": 8,
            "minimum_decisive_rate": 0.75,
            "minimum_candidate_win_rate": 0.60,
            "maximum_one_sided_p": 0.05,
            "required_axes": ["target_native", "source_fidelity"],
        })
        self.assertEqual(report["claim_scope"], {
            "schema": BENCHMARK.CLAIM_SCOPE_SCHEMA,
            "source_languages": ["en"],
            "source_language_locales": ["en-IE"],
            "required_target_locales": list(
                BENCHMARK.EU_BENCHMARK_TARGET_LOCALES
            ),
            "evaluated_target_locales": list(
                BENCHMARK.EU_BENCHMARK_TARGET_LOCALES
            ),
            "missing_target_locales": [],
            "unexpected_target_locales": [],
            "complete": True,
        })
        self.assertEqual(len(report["locales"]), 23)
        self.assertTrue(all(
            item["status"] == "PASS" for item in report["locales"]
        ))
        for locale_report in report["locales"]:
            self.assertEqual(
                [axis["phase"] for axis in locale_report["axes"]],
                ["target_native", "source_fidelity"],
            )
            self.assertTrue(all(
                axis["status"] == "PASS"
                and axis["block_reasons"] == []
                for axis in locale_report["axes"]
            ))
            self.assertEqual(
                [lane["content_type"] for lane in locale_report["content_type_lanes"]],
                ["commercial"],
            )
            self.assertTrue(all(
                lane["status"] == "PASS"
                and all(axis["status"] == "PASS" for axis in lane["axes"])
                for lane in locale_report["content_type_lanes"]
            ))

        blocked_locale = BENCHMARK.EU_BENCHMARK_TARGET_LOCALES[-1]
        replaced_keys = {
            SUITE.SOURCE_CASES[index].as_payload()["key"]
            for index in range(4)
        }
        weakened_results = [
            item for item in results
            if not (
                item["target_locale"] == blocked_locale
                and item["suite"]["case_key"] in replaced_keys
            )
        ]
        for index in range(4):
            outcome, _ = self.run_case(
                blocked_locale,
                f"blocked-{blocked_locale}-{index}",
                prefer="other",
                benchmark_policy=benchmark_policy,
            )
            weakened_results.append(outcome)
        blocked_report = self.summarize(
            benchmark_policy, weakened_results,
        )
        self.assertTrue(blocked_report["claim_scope"]["complete"])
        self.assertEqual(blocked_report["configured_lanes_status"], "BLOCK")
        self.assertFalse(blocked_report["superiority_claim_allowed"])
        self.assertEqual(
            blocked_report["claim_block_reasons"],
            ["configured_locale_evaluation_failed"],
        )
        blocked_by_locale = {
            item["locale"]: item for item in blocked_report["locales"]
        }
        self.assertEqual(blocked_by_locale[blocked_locale]["status"], "BLOCK")

    def test_non_suite_job_blocks_before_review(self):
        payload = PLANNER.plan_website_localization(
            source_id="benchmark.unregistered", source_revision=SUITE.VERSION,
            source_text="A valid source that was never registered in the suite.",
            source_locale="en-US", content_type="headline",
            glossary_version="blun-glossary-3", policy_version="native-web-2",
            provider_id="customer-llm", model_id="king", model_version="2026-08-30",
            software_version="6.43.0-dev", target_locales=["mt-MT"],
        ).jobs[0].as_payload()
        reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.run_benchmark(
                payload, candidate_result(payload), baseline(payload), assets(), policy(), reviewer,
                blinding_key=self.key,
            )
        self.assertEqual(caught.exception.code, "benchmark.suite.case_mismatch")
        self.assertEqual(reviewer.requests, [])

    def test_strong_maltese_average_cannot_hide_finnish_losses(self):
        results = []
        for index in range(len(SUITE.SOURCE_CASES)):
            outcome, _ = self.run_case("mt-MT", f"mt-{index}")
            results.append(outcome)
        for index in range(len(SUITE.SOURCE_CASES)):
            outcome, _ = self.run_case("fi-FI", f"fi-{index}", prefer="other")
            results.append(outcome)
        report = self.summarize(policy(), results)
        self.assertFalse(report["superiority_claim_allowed"])
        by_locale = {item["locale"]: item for item in report["locales"]}
        self.assertEqual(by_locale["mt-MT"]["status"], "PASS")
        self.assertEqual(by_locale["fi-FI"]["status"], "BLOCK")

    def test_joint_wins_cannot_hide_a_statistically_weak_fidelity_axis(self):
        class DivergentAxisReviewer(PreferenceReviewer):
            def review(self, request):
                self.requests.append(request)
                by_text = {
                    item["text"]: item["label"]
                    for item in request.input["variants"]
                }
                candidate = by_text[self.preferred_text]
                preference = candidate
                if request.phase == "source_fidelity":
                    preference = "B" if candidate == "A" else "A"
                return review_response(request, preference)

        results = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES)):
                payload = job(locale, f"axis-{locale}-{index}")
                result = candidate_result(payload)
                reviewer = (
                    PreferenceReviewer(result["candidate"])
                    if index < 11
                    else DivergentAxisReviewer(result["candidate"])
                )
                results.append(self.run_benchmark(
                    payload, result, baseline(payload), assets(locale),
                    policy(), reviewer, blinding_key=self.key,
                ))

        report = self.summarize(policy(), results)
        by_locale = {item["locale"]: item for item in report["locales"]}
        for locale in ("mt-MT", "fi-FI"):
            locale_report = by_locale[locale]
            # Eleven unanimous wins and four split cases make the joint-only
            # calculation significant while source fidelity remains weak.
            self.assertEqual(locale_report["candidate_wins"], 11)
            self.assertEqual(locale_report["baseline_wins"], 0)
            self.assertEqual(locale_report["one_sided_sign_p"], 0.00048828125)
            self.assertEqual(locale_report["status"], "BLOCK")
            axes = {item["phase"]: item for item in locale_report["axes"]}
            self.assertEqual(axes["target_native"]["status"], "PASS")
            self.assertEqual(axes["target_native"]["candidate_wins"], 15)
            self.assertEqual(axes["source_fidelity"]["status"], "BLOCK")
            self.assertEqual(axes["source_fidelity"]["candidate_wins"], 11)
            self.assertEqual(axes["source_fidelity"]["baseline_wins"], 4)
            self.assertEqual(
                axes["source_fidelity"]["one_sided_sign_p"], 0.059234619140625,
            )
            self.assertEqual(
                axes["source_fidelity"]["block_reasons"],
                ["not_statistically_significant"],
            )
        self.assertFalse(report["superiority_claim_allowed"])

    def test_aggregate_and_joint_wins_cannot_hide_weak_commercial_fidelity(self):
        class CommercialFidelityLossReviewer(PreferenceReviewer):
            def review(self, request):
                self.requests.append(request)
                by_text = {
                    item["text"]: item["label"]
                    for item in request.input["variants"]
                }
                candidate = by_text[self.preferred_text]
                preference = candidate
                if request.phase == "source_fidelity":
                    preference = "B" if candidate == "A" else "A"
                return review_response(request, preference)

        results = []
        for locale in ("mt-MT", "fi-FI"):
            commercial_seen = 0
            for index, source_case in enumerate(SUITE.SOURCE_CASES):
                payload = job(locale, f"commercial-lane-{locale}-{index}")
                result = candidate_result(payload)
                reviewer = PreferenceReviewer(result["candidate"])
                if source_case.content_type == "commercial":
                    commercial_seen += 1
                    if commercial_seen > 6:
                        reviewer = CommercialFidelityLossReviewer(
                            result["candidate"]
                        )
                results.append(self.run_benchmark(
                    payload, result, baseline(payload), assets(locale),
                    policy(), reviewer, blinding_key=self.key,
                ))

        report = self.summarize(policy(), results)
        self.assertEqual(report["configured_lanes_status"], "BLOCK")
        self.assertFalse(report["superiority_claim_allowed"])
        for locale_report in report["locales"]:
            self.assertTrue(locale_report["suite_complete"])
            self.assertEqual(locale_report["candidate_wins"], 13)
            self.assertEqual(locale_report["baseline_wins"], 0)
            self.assertEqual(locale_report["inconclusive"], 2)
            self.assertEqual(
                locale_report["one_sided_sign_p"], 0.0001220703125,
            )
            self.assertTrue(all(
                axis["status"] == "PASS" for axis in locale_report["axes"]
            ))
            self.assertEqual(locale_report["status"], "BLOCK")
            lane = locale_report["content_type_lanes"][0]
            self.assertEqual(lane["content_type"], "commercial")
            self.assertEqual(lane["case_count"], 8)
            self.assertEqual(lane["candidate_wins"], 6)
            self.assertEqual(lane["baseline_wins"], 0)
            self.assertEqual(lane["inconclusive"], 2)
            self.assertEqual(lane["one_sided_sign_p"], 0.015625)
            self.assertEqual(lane["status"], "BLOCK")
            lane_axes = {axis["phase"]: axis for axis in lane["axes"]}
            self.assertEqual(lane_axes["target_native"]["status"], "PASS")
            self.assertEqual(lane_axes["target_native"]["candidate_wins"], 8)
            self.assertEqual(lane_axes["source_fidelity"]["status"], "BLOCK")
            self.assertEqual(lane_axes["source_fidelity"]["candidate_wins"], 6)
            self.assertEqual(lane_axes["source_fidelity"]["baseline_wins"], 2)
            self.assertEqual(
                lane_axes["source_fidelity"]["block_reasons"],
                ["not_statistically_significant"],
            )

    def test_small_or_inconclusive_sample_never_claims_superiority(self):
        results = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES) - 1):
                outcome, _ = self.run_case(locale, f"small-{locale}-{index}")
                results.append(outcome)
        report = self.summarize(policy(), results)
        self.assertFalse(report["superiority_claim_allowed"])

        tied = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES)):
                outcome, _ = self.run_case(locale, f"tie-{locale}-{index}", prefer="tie")
                tied.append(outcome)
        report = self.summarize(policy(), tied)
        self.assertFalse(report["superiority_claim_allowed"])

    def test_duplicate_cases_and_mixed_versions_block(self):
        result, _ = self.run_case()
        with self.assertRaises(BENCHMARK.BenchmarkBlocked):
            self.summarize(policy(), [result, result])
        disguised_duplicate = copy.deepcopy(result)
        disguised_duplicate["case_id"] = "benchmark-case-" + "0" * 64
        with self.assertRaises(BENCHMARK.BenchmarkBlocked):
            self.summarize(policy(), [result, disguised_duplicate])
        changed = copy.deepcopy(result)
        changed["benchmark_version"] = "other"
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(policy(), [changed])
        self.assertEqual(caught.exception.code, "benchmark.attestation.payload_mismatch")

    def test_complete_suite_is_required_even_with_a_lower_case_threshold(self):
        results = []
        for locale in ("mt-MT", "fi-FI"):
            for index in range(len(SUITE.SOURCE_CASES) - 1):
                outcome, _ = self.run_case(locale, f"partial-{locale}-{index}")
                results.append(outcome)
        report = self.summarize(
            policy(minimum_cases_per_locale=len(SUITE.SOURCE_CASES) - 1),
            results,
        )
        self.assertFalse(report["superiority_claim_allowed"])
        self.assertTrue(all(not item["suite_complete"] for item in report["locales"]))

    def test_report_rejects_tampered_winner_and_nested_shapes(self):
        result, _ = self.run_case()
        changed = copy.deepcopy(result)
        changed["winner"] = "baseline"
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(policy(), [changed])
        self.assertEqual(caught.exception.code, "benchmark.attestation.payload_mismatch")

        for field, value in (
            ("job_id", "blun-l10n-" + "0" * 64),
            ("candidate", {**result["candidate"], "software_version": "stale"}),
            ("quality_profile", {**result["quality_profile"], "version": "stale"}),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(result)
                changed[field] = value
                with self.assertRaises(BENCHMARK.BenchmarkBlocked):
                    self.summarize(policy(), [changed])

        changed = copy.deepcopy(result)
        changed["integrity"]["candidate"] = "PASS"
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(policy(), [changed])
        self.assertEqual(caught.exception.code, "benchmark.attestation.payload_mismatch")

    def test_case_attestation_blocks_forgery_and_wrong_authority(self):
        result, _ = self.run_case()
        self.assertEqual(result["attestation"]["schema"], BENCHMARK.ATTESTATION_SCHEMA)
        self.assertEqual(result["attestation"]["key_id"], "benchmark-test-key-1")

        unsigned = copy.deepcopy(result)
        unsigned.pop("attestation")
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.summarize(policy(), [unsigned])
        self.assertEqual(caught.exception.code, "benchmark.attestation.invalid")

        for field, value, code in (
            ("signature", "0" * 64, "benchmark.attestation.rejected"),
            ("key_id", "other-key", "benchmark.attestation.binding_mismatch"),
            ("payload_sha256", "0" * 64, "benchmark.attestation.payload_mismatch"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(result)
                changed["attestation"][field] = value
                with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
                    self.summarize(policy(), [changed])
                self.assertEqual(caught.exception.code, code)

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.summarize_benchmark(
                policy(), [result],
                evidence_authority=HmacBenchmarkAuthority(key=b"wrong-key-material"),
            )
        self.assertEqual(caught.exception.code, "benchmark.attestation.rejected")

        class RejectingAuthority(HmacBenchmarkAuthority):
            reject_new_signature = False

            def sign(self, payload):
                signature = super().sign(payload)
                self.reject_new_signature = True
                return signature

            def verify(self, payload, signature):
                if self.reject_new_signature:
                    return False
                return super().verify(payload, signature)

        payload = job()
        reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.run_blind_benchmark_case(
                payload, candidate_result(payload), baseline(payload), assets(),
                policy(), reviewer, blinding_key=self.key,
                native_reference_artifact=native_reference(
                    payload, policy(), self.native_reference_verifier,
                    self.authority,
                ),
                native_reference_verifier=self.native_reference_verifier,
                evidence_authority=RejectingAuthority(),
            )
        self.assertEqual(caught.exception.code, "benchmark.attestation.rejected")
        self.assertEqual(len(reviewer.requests), 2)

    def test_report_attestation_binds_exact_case_evidence(self):
        result, _ = self.run_case()
        report = self.summarize(policy(), [result])
        verified = BENCHMARK.verify_benchmark_report(
            policy(), report, [result], evidence_authority=self.authority,
        )
        self.assertEqual(verified, report)
        self.assertEqual(report["attestation"]["schema"], BENCHMARK.ATTESTATION_SCHEMA)
        self.assertRegex(report["case_evidence_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["valid_until"], policy().valid_until)

        changed_report = copy.deepcopy(report)
        changed_report["superiority_claim_allowed"] = True
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.verify_benchmark_report(
                policy(), changed_report, [result], evidence_authority=self.authority,
            )
        self.assertEqual(caught.exception.code, "benchmark.attestation.payload_mismatch")

        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.verify_benchmark_report(
                policy(), report, [], evidence_authority=self.authority,
            )
        self.assertEqual(caught.exception.code, "benchmark.report.binding_mismatch")

        extended = policy(valid_until=policy().valid_until + 1)
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            BENCHMARK.verify_benchmark_report(
                extended, report, [result], evidence_authority=self.authority,
            )
        self.assertIn(caught.exception.code, {
            "benchmark.attestation.payload_mismatch",
            "benchmark.results.version_mismatch",
        })

    def test_progress_callback_runs_before_each_external_review(self):
        payload = job()
        phases = []
        reviewer = PreferenceReviewer(candidate_result(payload)["candidate"])
        self.run_benchmark(
            payload, candidate_result(payload), baseline(payload), assets(),
            policy(), reviewer, blinding_key=self.key,
            progress_callback=phases.append,
        )
        self.assertEqual(phases, ["target_native", "source_fidelity"])
        with self.assertRaises(BENCHMARK.BenchmarkBlocked) as caught:
            self.run_benchmark(
                payload, candidate_result(payload), baseline(payload), assets(),
                policy(), reviewer, blinding_key=self.key,
                progress_callback="not-callable",
            )
        self.assertEqual(caught.exception.code, "benchmark.progress.invalid")

    def test_campaign_plans_exact_complete_eu_matrix_and_replays_idempotently(self):
        benchmark_policy = campaign_policy(
            required_locales=CAMPAIGN._BENCHMARK.EU_BENCHMARK_TARGET_LOCALES,
        )
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)
            self.assertEqual(store.create(benchmark_policy, now=101), campaign_id)
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.create(benchmark_policy, max_attempts=4, now=102)
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.attempts_mismatch",
            )
            status = store.status(benchmark_policy, campaign_id)
            self.assertEqual(status["work_count"], 23 * len(SUITE.SOURCE_CASES))
            self.assertEqual(status["counts"]["pending"], 345)
            self.assertFalse(status["complete"])
            rows = connection.execute("""
                SELECT target_locale, suite_case_key
                FROM benchmark_campaign_work
            """).fetchall()
            self.assertEqual(len(set(map(tuple, rows))), 345)
            self.assertNotIn("en-IE", {row["target_locale"] for row in rows})

    def test_campaign_schema_v1_migrates_to_durable_reports_transactionally(self):
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("""
                CREATE TABLE benchmark_campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    policy_sha256 TEXT NOT NULL,
                    suite_sha256 TEXT NOT NULL,
                    work_count INTEGER NOT NULL CHECK (work_count > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            connection.execute(f"""
                CREATE TABLE benchmark_campaign_work (
                    work_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    target_locale TEXT NOT NULL,
                    suite_case_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN {CAMPAIGN.STATUSES}),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (
                        max_attempts BETWEEN 1 AND {CAMPAIGN.MAX_ATTEMPTS}
                    ),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    last_error_detail_hash TEXT,
                    result_json TEXT,
                    result_sha256 TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (campaign_id, target_locale, suite_case_key),
                    FOREIGN KEY (campaign_id)
                        REFERENCES benchmark_campaigns (campaign_id)
                )
            """)
            connection.execute("""
                CREATE INDEX benchmark_campaign_ready
                ON benchmark_campaign_work
                (campaign_id, status, next_attempt_at, target_locale, suite_case_key)
            """)
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

            store = CAMPAIGN.BenchmarkCampaignStore(connection)

            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                CAMPAIGN.SCHEMA_VERSION,
            )

    def test_campaign_validity_blocks_before_external_work_and_in_health(self):
        benchmark_policy = campaign_policy(valid_until=101)
        calls = []
        clock_values = iter((100, 102, 102))
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)
            outcome = CAMPAIGN.run_next_benchmark_case(
                store,
                benchmark_policy,
                campaign_id,
                "worker",
                lambda payload: calls.append(payload),
                CampaignCandidateReviewer(),
                blinding_key=self.key,
                native_reference_verifier=CampaignNativeReferenceVerifier(),
                evidence_authority=CampaignAuthority(),
                clock=lambda: next(clock_values),
            )

            self.assertEqual(calls, [])
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(
                outcome.error_code,
                "benchmark.campaign.validity_expired",
            )
            health = store.health(
                benchmark_policy, campaign_id, CampaignAuthority(), now=102,
            )
            self.assertEqual(health.status, "blocked")
            self.assertIn(
                "benchmark.campaign.validity_expired", health.reasons,
            )
            self.assertFalse(health.report_ready)
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.claim(
                    benchmark_policy, campaign_id, "worker", now=102,
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.validity_expired",
            )

    def test_case_expiry_before_completion_is_terminal_and_discards_result(self):
        benchmark_policy = campaign_policy(valid_until=101)
        authority = CampaignAuthority()
        verifier = CampaignNativeReferenceVerifier()
        reviewer = CampaignCandidateReviewer()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)

            outcome = CAMPAIGN.run_next_benchmark_case(
                store,
                benchmark_policy,
                campaign_id,
                "worker",
                lambda payload: campaign_inputs(
                    payload, benchmark_policy, verifier, authority,
                ),
                reviewer,
                blinding_key=self.key,
                native_reference_verifier=verifier,
                evidence_authority=authority,
                clock=lambda: 102 if len(reviewer.requests) == 2 else 100,
            )

            self.assertEqual(outcome.status, "failed")
            self.assertEqual(
                outcome.error_code,
                "benchmark.campaign.validity_expired",
            )
            row = connection.execute(
                """SELECT status, result_json, result_sha256
                   FROM benchmark_campaign_work WHERE work_id = ?""",
                (outcome.work_id,),
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertIsNone(row["result_json"])
            self.assertIsNone(row["result_sha256"])
            self.assertEqual(
                tuple(row["name"] for row in connection.execute(
                    "PRAGMA table_info(benchmark_campaign_reports)"
                )),
                CAMPAIGN.REPORT_COLUMNS,
            )
            self.assertEqual(
                tuple(row["name"] for row in connection.execute(
                    "PRAGMA table_info(benchmark_campaign_report_state)"
                )),
                CAMPAIGN.REPORT_STATE_COLUMNS,
            )
            store._verify_schema()

    def test_campaign_schema_v2_migrates_report_attempt_state(self):
        benchmark_policy = campaign_policy()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(
                benchmark_policy, max_attempts=4, now=100,
            )
            connection.execute("DROP TABLE benchmark_campaign_report_state")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()

            migrated = CAMPAIGN.BenchmarkCampaignStore(connection)

            status = migrated.status(benchmark_policy, campaign_id)
            self.assertEqual(status["report_finalization"], {
                "status": "pending",
                "attempt": 0,
                "max_attempts": 4,
                "next_attempt_at": 100.0,
                "error_code": None,
            })
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                CAMPAIGN.SCHEMA_VERSION,
            )

    def test_report_finalization_crash_lease_recovers_and_rejects_stale_token(self):
        benchmark_policy = campaign_policy()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(
                benchmark_policy, max_attempts=2, now=100,
            )
            encoded = "{}"
            connection.execute("""
                UPDATE benchmark_campaign_work
                SET status = 'succeeded', attempts = 1,
                    result_json = ?, result_sha256 = ?, updated_at = 100
                WHERE campaign_id = ?
            """, (encoded, CAMPAIGN._hash_text(encoded), campaign_id))
            connection.commit()
            stale = store.claim_report_finalization(
                benchmark_policy,
                campaign_id,
                "report-worker-a",
                now=100,
                lease_seconds=5,
            )
            recovered = store.claim_report_finalization(
                benchmark_policy,
                campaign_id,
                "report-worker-b",
                now=106,
                lease_seconds=5,
            )
            self.assertEqual(recovered.attempt, 2)
            self.assertNotEqual(recovered.lease_token, stale.lease_token)
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.transition_report_failure(
                    benchmark_policy,
                    stale,
                    "benchmark.campaign.report_unexpected",
                    retryable=True,
                    now=106,
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.report_lease_lost",
            )
            self.assertIsNone(store.claim_report_finalization(
                benchmark_policy,
                campaign_id,
                "report-worker-c",
                now=112,
                lease_seconds=5,
            ))
            state = store.status(
                benchmark_policy, campaign_id,
            )["report_finalization"]
            self.assertEqual(state["status"], "failed")
            self.assertEqual(
                state["error_code"],
                "benchmark.campaign.report_lease_expired",
            )
            connection.execute("""
                UPDATE benchmark_campaign_report_state
                SET max_attempts = 20 WHERE campaign_id = ?
            """, (campaign_id,))
            connection.commit()
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.status(benchmark_policy, campaign_id)
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.report_state_invalid",
            )

    def test_campaign_policy_change_creates_new_identity_and_tampering_blocks(self):
        first = campaign_policy()
        second = campaign_policy(candidate_model_version="2026-09-08")
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            first_id = store.create(first, now=100)
            second_id = store.create(second, now=101)
            self.assertNotEqual(first_id, second_id)
            connection.execute("""
                UPDATE benchmark_campaign_work SET suite_case_key = 'exchanged-case'
                WHERE work_id = (
                    SELECT work_id FROM benchmark_campaign_work
                    WHERE campaign_id = ? LIMIT 1
                )
            """, (first_id,))
            connection.commit()
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.status(first, first_id)
            self.assertEqual(caught.exception.code, "benchmark.campaign.state_invalid")

    def test_campaign_recovers_expired_lease_and_rejects_stale_completion(self):
        benchmark_policy = campaign_policy()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)
            stale = store.claim(
                benchmark_policy, campaign_id, "worker-a",
                now=100, lease_seconds=5,
            )
            recovered = store.claim(
                benchmark_policy, campaign_id, "worker-b",
                now=106, lease_seconds=5,
            )
            self.assertEqual(recovered.work_id, stale.work_id)
            self.assertEqual(recovered.attempt, 2)
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.transition_failure(
                    benchmark_policy, stale, "reviewer.timeout",
                    retryable=True, now=106,
                )
            self.assertEqual(caught.exception.code, "benchmark.campaign.lease_lost")

    def test_campaign_health_is_read_only_and_detects_expired_or_stalled_work(self):
        benchmark_policy = campaign_policy()
        authority = CampaignAuthority()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)

            recent = store.health(
                benchmark_policy, campaign_id, authority,
                now=105, stale_after_seconds=10,
            )
            self.assertEqual(recent.status, "healthy")
            self.assertFalse(recent.report_ready)
            self.assertEqual(dict(recent.counts)["pending"], 30)

            stale = store.health(
                benchmark_policy, campaign_id, authority,
                now=111, stale_after_seconds=10,
            )
            self.assertEqual(stale.status, "degraded")
            self.assertEqual(stale.reasons, ("benchmark.campaign.stalled",))

            claim = store.claim(
                benchmark_policy, campaign_id, "worker",
                now=112, lease_seconds=5,
            )
            before = connection.total_changes
            expired = store.health(
                benchmark_policy, campaign_id, authority,
                now=117, stale_after_seconds=10,
            )
            self.assertIn("benchmark.campaign.lease_expired", expired.reasons)
            self.assertEqual(connection.total_changes, before)
            row = connection.execute(
                "SELECT status FROM benchmark_campaign_work WHERE work_id = ?",
                (claim.work_id,),
            ).fetchone()
            self.assertEqual(row["status"], "leased")

    def test_campaign_dependency_failure_is_bounded_and_content_free(self):
        benchmark_policy = campaign_policy()
        secret = "private provider response with customer text"
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(
                benchmark_policy, max_attempts=1, now=100,
            )

            def unavailable(_):
                raise CAMPAIGN.BenchmarkCampaignDependencyFailed(
                    "provider_unavailable", retryable=True,
                )

            outcome = CAMPAIGN.run_next_benchmark_case(
                store, benchmark_policy, campaign_id, "worker", unavailable,
                CampaignCandidateReviewer(), blinding_key=self.key,
                native_reference_verifier=CampaignNativeReferenceVerifier(),
                evidence_authority=CampaignAuthority(), clock=lambda: 100,
            )
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(
                outcome.error_code,
                "benchmark.campaign.dependency.provider_unavailable",
            )
            serialized = json.dumps(store.status(benchmark_policy, campaign_id))
            self.assertNotIn(secret, serialized)
            self.assertEqual(
                store.status(benchmark_policy, campaign_id)["counts"]["failed"],
                1,
            )
            next_claim = store.claim(
                benchmark_policy, campaign_id, "other", now=1000,
            )
            self.assertIsNotNone(next_claim)
            self.assertNotEqual(next_claim.work_id, outcome.work_id)
            health = store.health(
                benchmark_policy, campaign_id, CampaignAuthority(),
                now=1000,
            )
            self.assertEqual(health.status, "blocked")
            self.assertIn("benchmark.campaign.failed", health.reasons)
            self.assertNotIn(secret, json.dumps(health.as_payload()))

    def test_campaign_accepts_content_free_baseline_adapter_failures(self):
        benchmark_policy = campaign_policy()

        class ExternalBaselineFailure(RuntimeError):
            benchmark_campaign_dependency_failure = True

            def __init__(self):
                self.code = "deepl.rate_limited"
                self.retryable = True

        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)

            def rate_limited(_):
                raise ExternalBaselineFailure()

            outcome = CAMPAIGN.run_next_benchmark_case(
                store, benchmark_policy, campaign_id, "worker", rate_limited,
                CampaignCandidateReviewer(), blinding_key=self.key,
                native_reference_verifier=CampaignNativeReferenceVerifier(),
                evidence_authority=CampaignAuthority(), clock=lambda: 100,
            )
            self.assertEqual(outcome.status, "retry_wait")
            self.assertEqual(
                outcome.error_code,
                "benchmark.campaign.dependency.deepl.rate_limited",
            )

    def _benchmark_runtime_fixture(self, *, reviewer=None, max_attempts=3):
        connections = [sqlite3.connect(":memory:") for _ in range(5)]
        for connection in connections:
            self.addCleanup(connection.close)
        benchmark_policy = campaign_policy()
        authority = CampaignAuthority()
        verifier = CampaignNativeReferenceVerifier()
        provider = RuntimeCandidateProvider()
        reviewer = reviewer or CampaignCandidateReviewer()
        calls = {"candidate_provider": 0, "baseline": 0, "reference": 0}
        current_time = [100.0]

        def acquire_baseline(payload, selected_policy, selected_authority, guard):
            calls["baseline"] += 1
            guard()
            target = _target_fixture(payload, "baseline")
            return BENCHMARK_RUNTIME._BASELINE.create_lawful_fixture_acquisition(
                payload,
                target,
                selected_policy,
                {
                    "schema": BENCHMARK_RUNTIME._BASELINE.FIXTURE_EVIDENCE_SCHEMA,
                    "fixture_id": "licensed-baseline-fixture",
                    "fixture_revision": "2026-09-08",
                    "supplier_id": "licensed-supplier",
                    "rights_basis": "licensed",
                    "rights_evidence_sha256": "a" * 64,
                    "source_sha256": payload["source"]["sha256"],
                    "target_locale": payload["target"]["locale"],
                    "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
                },
                evidence_authority=selected_authority,
            )

        def load_reference(payload):
            calls["reference"] += 1
            return native_reference(
                payload, benchmark_policy, verifier, authority,
            )

        def resolve_candidate_provider(_):
            calls["candidate_provider"] += 1
            return provider

        runtime = BENCHMARK_RUNTIME.WebsiteLocalizationBenchmarkRuntime(
            campaign_connection=connections[0],
            candidate_connection=connections[1],
            baseline_connection=connections[2],
            native_reference_connection=connections[3],
            review_connection=connections[4],
            policy=benchmark_policy,
            candidate_route_id="attached-model-primary",
            baseline_route_id="licensed-baseline",
            native_reference_route_id="qualified-native-vault",
            reviewer_route_id="independent-review-panel",
            assets_resolver=lambda payload: assets(payload["target"]["locale"]),
            candidate_provider_resolver=resolve_candidate_provider,
            baseline_acquirer=acquire_baseline,
            native_reference_loader=load_reference,
            reviewer=reviewer,
            native_reference_verifier=verifier,
            evidence_authority=authority,
            blinding_key=self.key,
            worker_id="benchmark-worker-1",
            max_attempts=max_attempts,
            clock=lambda: current_time[0],
        )
        return runtime, connections, provider, calls, current_time

    def test_benchmark_runtime_preflight_writes_no_schema_on_invalid_configuration(self):
        connections = [sqlite3.connect(":memory:") for _ in range(4)]
        for connection in connections:
            self.addCleanup(connection.close)
        with self.assertRaises(BENCHMARK_RUNTIME.BenchmarkRuntimeFailed) as caught:
            BENCHMARK_RUNTIME.WebsiteLocalizationBenchmarkRuntime(
                campaign_connection=connections[0],
                candidate_connection=connections[0],
                baseline_connection=connections[1],
                native_reference_connection=connections[2],
                review_connection=connections[3],
                policy=campaign_policy(),
                candidate_route_id="candidate",
                baseline_route_id="baseline",
                native_reference_route_id="reference",
                reviewer_route_id="reviewer",
                assets_resolver=lambda _: None,
                candidate_provider_resolver=lambda _: None,
                baseline_acquirer=lambda *_: None,
                native_reference_loader=lambda _: None,
                reviewer=CampaignCandidateReviewer(),
                native_reference_verifier=CampaignNativeReferenceVerifier(),
                evidence_authority=CampaignAuthority(),
                blinding_key=self.key,
                worker_id="worker",
            )
        self.assertEqual(caught.exception.code, "benchmark.runtime.connection_reused")
        for connection in connections:
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall(),
                [],
            )
        late_connections = [sqlite3.connect(":memory:") for _ in range(5)]
        for connection in late_connections:
            self.addCleanup(connection.close)
        with self.assertRaises(BENCHMARK_RUNTIME.BenchmarkRuntimeFailed) as caught:
            BENCHMARK_RUNTIME.WebsiteLocalizationBenchmarkRuntime(
                campaign_connection=late_connections[0],
                candidate_connection=late_connections[1],
                baseline_connection=late_connections[2],
                native_reference_connection=late_connections[3],
                review_connection=late_connections[4],
                policy=campaign_policy(),
                candidate_route_id="candidate",
                baseline_route_id="baseline",
                native_reference_route_id="reference",
                reviewer_route_id="reviewer",
                assets_resolver=lambda _: None,
                candidate_provider_resolver=lambda _: None,
                baseline_acquirer=lambda *_: None,
                native_reference_loader=lambda _: None,
                reviewer=CampaignCandidateReviewer(),
                native_reference_verifier=CampaignNativeReferenceVerifier(),
                evidence_authority=CampaignAuthority(),
                blinding_key=self.key,
                worker_id="worker",
                clock=lambda: True,
            )
        self.assertEqual(caught.exception.code, "benchmark.runtime.clock_invalid")
        for connection in late_connections:
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall(),
                [],
            )

        expired_connections = [sqlite3.connect(":memory:") for _ in range(5)]
        for connection in expired_connections:
            self.addCleanup(connection.close)
        with self.assertRaises(BENCHMARK_RUNTIME.BenchmarkRuntimeFailed) as caught:
            BENCHMARK_RUNTIME.WebsiteLocalizationBenchmarkRuntime(
                campaign_connection=expired_connections[0],
                candidate_connection=expired_connections[1],
                baseline_connection=expired_connections[2],
                native_reference_connection=expired_connections[3],
                review_connection=expired_connections[4],
                policy=campaign_policy(valid_until=99),
                candidate_route_id="candidate",
                baseline_route_id="baseline",
                native_reference_route_id="reference",
                reviewer_route_id="reviewer",
                assets_resolver=lambda _: None,
                candidate_provider_resolver=lambda _: None,
                baseline_acquirer=lambda *_: None,
                native_reference_loader=lambda _: None,
                reviewer=CampaignCandidateReviewer(),
                native_reference_verifier=CampaignNativeReferenceVerifier(),
                evidence_authority=CampaignAuthority(),
                blinding_key=self.key,
                worker_id="worker",
                clock=lambda: 100,
            )
        self.assertEqual(
            caught.exception.code,
            "benchmark.runtime.validity_expired",
        )
        for connection in expired_connections:
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall(),
                [],
            )

    def test_benchmark_runtime_retries_with_exact_durable_inputs(self):
        reviewer = RetryOnceCampaignReviewer()
        runtime, connections, provider, calls, current_time = (
            self._benchmark_runtime_fixture(reviewer=reviewer)
        )
        guard_calls = []
        first = runtime.run_once(
            operation_guard=guard_calls.append,
            retry_base_seconds=5,
        )
        self.assertEqual(first.status, "retry_wait")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(
            calls,
            {"candidate_provider": 1, "baseline": 1, "reference": 1},
        )
        self.assertGreater(len(guard_calls), 10)

        current_time[0] = 106
        second = runtime.run_once(
            operation_guard=guard_calls.append,
            retry_base_seconds=5,
        )
        self.assertEqual(second.work_id, first.work_id)
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(
            calls,
            {"candidate_provider": 1, "baseline": 1, "reference": 1},
        )
        self.assertEqual(len(reviewer.requests), 3)
        status_text = json.dumps(runtime.status(), ensure_ascii=False)
        payload = CAMPAIGN._job_payload(
            runtime.policy, second.target_locale, second.suite_case_key,
        )
        for prohibited in (
            payload["source"]["text"],
            _target_fixture(payload, "candidate"),
            _target_fixture(payload, "baseline"),
            _target_fixture(payload, "reference"),
        ):
            self.assertNotIn(prohibited, status_text)
        for connection, (table, expected_count) in zip(connections[1:], (
            ("benchmark_candidate_acquisitions", 1),
            ("benchmark_baseline_acquisitions", 1),
            ("benchmark_native_references", 1),
            ("benchmark_review_evidence", 2),
        )):
            self.assertEqual(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                expected_count,
            )

    def test_benchmark_runtime_resumes_after_persisted_first_review(self):
        reviewer = RetryFidelityOnceCampaignReviewer()
        runtime, connections, provider, calls, current_time = (
            self._benchmark_runtime_fixture(reviewer=reviewer)
        )

        first = runtime.run_once(retry_base_seconds=5)

        self.assertEqual(first.status, "retry_wait")
        self.assertEqual(
            [item.phase for item in reviewer.requests],
            ["target_native", "source_fidelity"],
        )
        self.assertEqual(
            connections[4].execute(
                "SELECT COUNT(*) FROM benchmark_review_evidence"
            ).fetchone()[0],
            1,
        )
        current_time[0] = 106

        second = runtime.run_once(retry_base_seconds=5)

        self.assertEqual(second.work_id, first.work_id)
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(
            [item.phase for item in reviewer.requests],
            ["target_native", "source_fidelity", "source_fidelity"],
        )
        self.assertEqual(
            connections[4].execute(
                "SELECT COUNT(*) FROM benchmark_review_evidence"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(
            calls,
            {"candidate_provider": 1, "baseline": 1, "reference": 1},
        )

    def test_benchmark_runtime_review_tamper_is_terminal_before_reviewer(self):
        reviewer = RetryFidelityOnceCampaignReviewer()
        runtime, connections, provider, calls, current_time = (
            self._benchmark_runtime_fixture(reviewer=reviewer, max_attempts=3)
        )
        first = runtime.run_once(retry_base_seconds=5)
        self.assertEqual(first.status, "retry_wait")
        connections[4].execute("""
            UPDATE benchmark_review_evidence SET artifact_json = '{}'
        """)
        connections[4].commit()
        current_time[0] = 106

        second = runtime.run_once(retry_base_seconds=5)

        self.assertEqual(second.work_id, first.work_id)
        self.assertEqual(second.status, "failed")
        self.assertEqual(
            second.error_code,
            "reviewer.review.store.state_invalid",
        )
        self.assertEqual(len(reviewer.requests), 2)
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(
            calls,
            {"candidate_provider": 1, "baseline": 1, "reference": 1},
        )
        self.assertNotIn(
            "private fidelity reviewer failure",
            json.dumps(runtime.status()),
        )

    def test_benchmark_runtime_tamper_blocks_before_second_review_or_provider_call(self):
        reviewer = RetryOnceCampaignReviewer()
        runtime, connections, provider, calls, current_time = (
            self._benchmark_runtime_fixture(reviewer=reviewer, max_attempts=2)
        )
        first = runtime.run_once(retry_base_seconds=5)
        self.assertEqual(first.status, "retry_wait")
        connections[1].execute("""
            UPDATE benchmark_candidate_acquisitions
            SET artifact_json = '{}'
        """)
        connections[1].commit()
        current_time[0] = 106
        second = runtime.run_once(retry_base_seconds=5)
        self.assertEqual(second.work_id, first.work_id)
        self.assertEqual(second.status, "failed")
        self.assertEqual(
            second.error_code,
            "benchmark.campaign.dependency.candidate.store.state_invalid",
        )
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(
            calls,
            {"candidate_provider": 1, "baseline": 1, "reference": 1},
        )
        self.assertEqual(len(reviewer.requests), 1)
        self.assertNotIn("private reviewer failure", json.dumps(runtime.status()))

    def test_benchmark_runtime_finalizes_after_last_case_and_after_crash_gap(self):
        for completed_case in (False, True):
            with self.subTest(completed_case=completed_case):
                runtime, _, _, _, _ = self._benchmark_runtime_fixture()
                outcome = (
                    CAMPAIGN.BenchmarkCampaignOutcome(
                        work_id="benchmark-work-" + "a" * 64,
                        target_locale="mt-MT",
                        suite_case_key="homepage-headline",
                        status="succeeded",
                        attempt=1,
                        max_attempts=3,
                        next_attempt_at=100,
                        error_code=None,
                        error_detail_hash=None,
                        result_sha256="b" * 64,
                    )
                    if completed_case else None
                )
                guard_calls = []
                report_outcome = (
                    BENCHMARK_RUNTIME._CAMPAIGN.BenchmarkReportFinalizationOutcome(
                        campaign_id=runtime.campaign_id,
                        status="succeeded",
                        attempt=1,
                        max_attempts=3,
                        next_attempt_at=100,
                        error_code=None,
                        error_detail_hash=None,
                    )
                )
                with (
                    mock.patch.object(
                        BENCHMARK_RUNTIME._CAMPAIGN,
                        "run_next_benchmark_case",
                        return_value=outcome,
                    ),
                    mock.patch.object(
                        runtime.campaign_store,
                        "report_finalization_required",
                        return_value=True,
                    ) as required,
                    mock.patch.object(
                        BENCHMARK_RUNTIME._CAMPAIGN,
                        "run_benchmark_report_finalization",
                        return_value=report_outcome,
                    ) as finalize,
                ):
                    observed = runtime.run_once(
                        operation_guard=guard_calls.append,
                        lease_seconds=240,
                    )

                if completed_case:
                    self.assertIs(observed, outcome)
                else:
                    self.assertIs(observed, report_outcome)
                    self.assertEqual(observed.campaign_id, runtime.campaign_id)
                    self.assertEqual(observed.status, "succeeded")
                    self.assertEqual(observed.attempt, 1)
                    self.assertIsNone(observed.error_code)
                required.assert_called_once_with(
                    runtime.policy, runtime.campaign_id,
                )
                finalize.assert_called_once()
                arguments = finalize.call_args
                self.assertEqual(arguments.args[:5], (
                    runtime.campaign_store,
                    runtime.policy,
                    runtime.campaign_id,
                    runtime.worker_id,
                    runtime.evidence_authority,
                ))
                self.assertIs(arguments.kwargs["clock"], runtime.clock)
                self.assertEqual(
                    arguments.kwargs["operation_guard"], guard_calls.append,
                )
                self.assertEqual(arguments.kwargs["lease_seconds"], 240.0)

        runtime, _, _, _, _ = self._benchmark_runtime_fixture()
        with (
            mock.patch.object(
                BENCHMARK_RUNTIME._CAMPAIGN,
                "run_next_benchmark_case",
                return_value=None,
            ),
            mock.patch.object(
                runtime.campaign_store,
                "report_finalization_required",
                return_value=False,
            ),
            mock.patch.object(
                BENCHMARK_RUNTIME._CAMPAIGN,
                "run_benchmark_report_finalization",
            ) as finalize,
        ):
            self.assertIsNone(runtime.run_once())
        finalize.assert_not_called()

    def test_benchmark_runtime_loads_only_the_existing_verified_report(self):
        runtime, _, _, _, current_time = self._benchmark_runtime_fixture()
        current_time[0] = 123
        expected = {"schema": "verified-report", "status": "BLOCK"}
        with mock.patch.object(
            runtime.campaign_store,
            "load_report",
            return_value=expected,
        ) as load_report:
            observed = runtime.load_report()

        self.assertIs(observed, expected)
        load_report.assert_called_once_with(
            runtime.policy,
            runtime.campaign_id,
            runtime.evidence_authority,
            now=123,
        )

    def test_cross_loaded_deepl_adapter_store_and_inputs_complete_campaign_case(self):
        benchmark_policy = campaign_policy()
        authority = CampaignAuthority()
        verifier = CampaignNativeReferenceVerifier()
        reviewer = CampaignCandidateReviewer()

        def http_result(value):
            body = json.dumps(
                value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return BASELINE_ADAPTER.HTTPResult(
                200,
                (("Content-Type", "application/json"),
                 ("Content-Length", str(len(body)))),
                body,
            )

        class Transport:
            def __init__(self):
                self.calls = []
                self.results = [
                    http_result([
                        {
                            "lang": "en", "usable_as_source": True,
                            "usable_as_target": False, "status": "stable",
                        },
                        {
                            "lang": "fi", "usable_as_source": True,
                            "usable_as_target": True, "status": "stable",
                        },
                    ]),
                    http_result({"translations": [{
                        "detected_source_language": "EN",
                        "model_type_used": "quality_optimized",
                        "text": TARGETS["fi-FI"]["baseline"],
                    }]}),
                ]

            def request(self, method, url, headers, body, *, timeout):
                self.calls.append((method, url))
                return self.results.pop(0)

        transport = Transport()
        adapter = BASELINE_ADAPTER.DeepLBaselineAdapter(
            "pro", lambda: "private-api-key", transport=transport,
        )
        with (
            sqlite3.connect(":memory:") as campaign_connection,
            sqlite3.connect(":memory:") as baseline_connection,
        ):
            campaign_store = CAMPAIGN.BenchmarkCampaignStore(
                campaign_connection,
            )
            acquisition_store = BASELINE_ADAPTER.BaselineAcquisitionStore(
                baseline_connection,
            )
            campaign_id = campaign_store.create(benchmark_policy, now=100)

            def resolve(payload):
                acquisition = BASELINE_ADAPTER.resolve_baseline_acquisition(
                    acquisition_store,
                    payload,
                    benchmark_policy,
                    "deepl-pro",
                    lambda: adapter.acquire(
                        payload, benchmark_policy,
                        evidence_authority=authority,
                    ),
                    evidence_authority=authority,
                    now=100,
                )
                reference_text = _target_fixture(payload, "reference")
                request = CAMPAIGN._BENCHMARK.native_reference_verification_request(
                    payload,
                    reference_text,
                    benchmark_policy,
                    reviewer_id="qualified-native-reviewer-17",
                    reviewer_version="credential-2026-08-30",
                )
                reference = CAMPAIGN._BENCHMARK.create_native_reference_artifact(
                    payload,
                    reference_text,
                    benchmark_policy,
                    reviewer_id="qualified-native-reviewer-17",
                    reviewer_version="credential-2026-08-30",
                    qualification_receipt=verifier.receipt(request),
                    native_reference_verifier=verifier,
                    evidence_authority=authority,
                )
                return FOREIGN_CAMPAIGN.BenchmarkCaseInputs(
                    candidate_result=candidate_result(payload),
                    baseline_artifact=acquisition.artifact,
                    assets=assets(payload["target"]["locale"]),
                    native_reference_artifact=reference,
                )

            outcome = CAMPAIGN.run_next_benchmark_case(
                campaign_store,
                benchmark_policy,
                campaign_id,
                "worker",
                resolve,
                reviewer,
                blinding_key=self.key,
                native_reference_verifier=verifier,
                evidence_authority=authority,
                clock=lambda: 100,
            )
            self.assertEqual(outcome.status, "succeeded")
            cached = acquisition_store.load(
                CAMPAIGN._job_payload(
                    benchmark_policy, outcome.target_locale,
                    outcome.suite_case_key,
                ),
                benchmark_policy,
                "deepl-pro",
                evidence_authority=authority,
            )
            self.assertEqual(cached.artifact["target_locale"], "fi-FI")
        self.assertEqual(
            transport.calls,
            [
                ("GET", "https://api.deepl.com/v3/languages?resource=translate_text"),
                ("POST", "https://api.deepl.com/v2/translate"),
            ],
        )

    def test_cross_module_normalization_rejects_mutable_lookalikes(self):
        benchmark_policy = campaign_policy()

        class BenchmarkPolicy:
            pass

        lookalike = BenchmarkPolicy()
        for field, value in CAMPAIGN.asdict(benchmark_policy).items():
            setattr(lookalike, field, value)
        with self.assertRaises(
            BASELINE_ADAPTER._BENCHMARK.BenchmarkBlocked,
        ) as caught:
            BASELINE_ADAPTER._BENCHMARK._validate_policy(lookalike)
        self.assertEqual(caught.exception.code, "benchmark.policy.invalid")

        class BenchmarkSignature:
            def __init__(self):
                self.algorithm = "hmac-sha256-test"
                self.key_id = "benchmark-test-key-1"
                self.signature = "valid-looking-token"

        with self.assertRaises(
            BASELINE_ADAPTER._BENCHMARK.BenchmarkBlocked,
        ) as caught:
            BASELINE_ADAPTER._BENCHMARK._benchmark_signature(
                BenchmarkSignature(),
            )
        self.assertEqual(caught.exception.code, "benchmark.attestation.invalid")

        class BenchmarkCaseInputs:
            candidate_result = {}
            baseline_artifact = {}
            assets = None
            native_reference_artifact = {}

        reviewer = CampaignCandidateReviewer()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(
                benchmark_policy, max_attempts=1, now=100,
            )
            outcome = CAMPAIGN.run_next_benchmark_case(
                store,
                benchmark_policy,
                campaign_id,
                "worker",
                lambda _: BenchmarkCaseInputs(),
                reviewer,
                blinding_key=self.key,
                native_reference_verifier=CampaignNativeReferenceVerifier(),
                evidence_authority=CampaignAuthority(),
                clock=lambda: 100,
            )
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(
            outcome.error_code,
            "benchmark.campaign.dependency.inputs_invalid",
        )
        self.assertEqual(reviewer.requests, [])

        fields = [
            (field.name, object)
            for field in dataclasses.fields(CAMPAIGN.BenchmarkCaseInputs)
        ]
        ExtraInputs = dataclasses.make_dataclass(
            "BenchmarkCaseInputs", fields + [("extra", object)], frozen=True,
        )
        MissingInputs = dataclasses.make_dataclass(
            "BenchmarkCaseInputs", fields[:-1], frozen=True,
        )
        values = ({
            "candidate_result": {},
            "baseline_artifact": {},
            "assets": None,
            "native_reference_artifact": {},
        })
        for malformed in (
            ExtraInputs(**values, extra=None),
            MissingInputs(**{
                key: value for key, value in values.items()
                if key != "native_reference_artifact"
            }),
        ):
            with self.subTest(shape=type(malformed).__name__):
                with self.assertRaises(
                    CAMPAIGN.BenchmarkCampaignDependencyFailed,
                ) as caught:
                    CAMPAIGN._benchmark_case_inputs(malformed)
                self.assertEqual(caught.exception.code, "inputs_invalid")

    def test_campaign_runs_one_case_per_tick_and_blocks_incomplete_report(self):
        benchmark_policy = campaign_policy()
        authority = CampaignAuthority()
        verifier = CampaignNativeReferenceVerifier()
        reviewer = CampaignCandidateReviewer()
        guard_calls = []
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)
            outcome = CAMPAIGN.run_next_benchmark_case(
                store, benchmark_policy, campaign_id, "worker",
                lambda payload: campaign_inputs(
                    payload, benchmark_policy, verifier, authority,
                ),
                reviewer,
                blinding_key=self.key,
                native_reference_verifier=verifier,
                evidence_authority=authority,
                operation_guard=guard_calls.append,
                clock=lambda: 100,
            )
            self.assertEqual(outcome.status, "succeeded")
            self.assertRegex(outcome.result_sha256, r"^[0-9a-f]{64}$")
            status = store.status(benchmark_policy, campaign_id)
            self.assertEqual(status["counts"]["succeeded"], 1)
            self.assertEqual(status["counts"]["pending"], 29)
            self.assertEqual(len(reviewer.requests), 2)
            self.assertGreaterEqual(len(guard_calls), 4)
            stored = connection.execute("""
                SELECT result_json FROM benchmark_campaign_work
                WHERE status = 'succeeded'
            """).fetchone()[0]
            self.assertNotIn(fixture_copy(outcome.target_locale)["candidate"], stored)
            self.assertNotIn(fixture_copy(outcome.target_locale)["baseline"], stored)
            health = store.health(
                benchmark_policy, campaign_id, authority, now=100,
            )
            self.assertEqual(health.status, "healthy")
            self.assertFalse(health.report_ready)
            connection.execute("""
                UPDATE benchmark_campaign_work SET result_json = '{}'
                WHERE status = 'succeeded'
            """)
            connection.commit()
            blocked_health = store.health(
                benchmark_policy, campaign_id, authority, now=100,
            )
            self.assertEqual(blocked_health.status, "blocked")
            self.assertEqual(
                blocked_health.reasons,
                ("benchmark.campaign.state_invalid",),
            )
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.summarize(benchmark_policy, campaign_id, authority)
            self.assertEqual(caught.exception.code, "benchmark.campaign.incomplete")

    def test_complete_early_campaign_produces_attested_partial_scope_report(self):
        benchmark_policy = campaign_policy()
        authority = CountingCampaignAuthority()
        verifier = CampaignNativeReferenceVerifier()
        reviewer = CampaignCandidateReviewer()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)
            outcomes = []
            while True:
                outcome = CAMPAIGN.run_next_benchmark_case(
                    store, benchmark_policy, campaign_id, "worker",
                    lambda payload: campaign_inputs(
                        payload, benchmark_policy, verifier, authority,
                    ),
                    reviewer,
                    blinding_key=self.key,
                    native_reference_verifier=verifier,
                    evidence_authority=authority,
                    clock=lambda: 100,
                )
                if outcome is None:
                    break
                outcomes.append(outcome)
            self.assertEqual(len(outcomes), 30)
            self.assertTrue(store.status(benchmark_policy, campaign_id)["complete"])
            sign_calls = authority.sign_calls
            before = connection.total_changes
            pending_report = store.health(
                benchmark_policy, campaign_id, authority, now=100,
            )
            self.assertEqual(pending_report.status, "degraded")
            self.assertEqual(
                pending_report.reasons,
                ("benchmark.campaign.report_missing",),
            )
            self.assertFalse(pending_report.report_ready)
            self.assertEqual(authority.sign_calls, sign_calls)
            self.assertEqual(connection.total_changes, before)
            self.assertTrue(store.report_finalization_required(
                benchmark_policy, campaign_id,
            ))
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.load_report(
                    benchmark_policy, campaign_id, authority, now=100,
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.report_missing",
            )
            self.assertEqual(authority.sign_calls, sign_calls)
            self.assertEqual(connection.total_changes, before)

            lost_guard_calls = []

            def lose_guard_after_signing():
                lost_guard_calls.append(len(lost_guard_calls) + 1)
                if len(lost_guard_calls) == 2:
                    raise RuntimeError("outer lease lost")

            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.summarize(
                    benchmark_policy,
                    campaign_id,
                    authority,
                    now=101,
                    operation_guard=lose_guard_after_signing,
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.operation_guard_failed",
            )
            self.assertEqual(lost_guard_calls, [1, 2])
            self.assertEqual(authority.sign_calls, sign_calls + 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM benchmark_campaign_reports"
            ).fetchone()[0], 0)

            report = store.summarize(
                benchmark_policy, campaign_id, authority, now=102,
            )
            self.assertEqual(authority.sign_calls, sign_calls + 2)
            first_report_json = connection.execute("""
                SELECT report_json FROM benchmark_campaign_reports
                WHERE campaign_id = ?
            """, (campaign_id,)).fetchone()[0]
            self.assertFalse(store.report_finalization_required(
                benchmark_policy, campaign_id,
            ))
            repeated = store.summarize(
                benchmark_policy, campaign_id, authority, now=103,
            )
            self.assertEqual(repeated, report)
            self.assertEqual(authority.sign_calls, sign_calls + 2)
            self.assertEqual(
                connection.execute("""
                    SELECT report_json FROM benchmark_campaign_reports
                    WHERE campaign_id = ?
                """, (campaign_id,)).fetchone()[0],
                first_report_json,
            )
            before_load = connection.total_changes
            verify_calls = authority.verify_calls
            loaded = store.load_report(
                benchmark_policy, campaign_id, authority, now=103,
            )
            self.assertEqual(loaded, report)
            self.assertEqual(authority.sign_calls, sign_calls + 2)
            self.assertGreater(authority.verify_calls, verify_calls)
            self.assertEqual(connection.total_changes, before_load)
            self.assertEqual(
                connection.execute("""
                    SELECT report_json FROM benchmark_campaign_reports
                    WHERE campaign_id = ?
                """, (campaign_id,)).fetchone()[0],
                first_report_json,
            )
            self.assertEqual(report["configured_lanes_status"], "PASS")
            self.assertEqual(report["status"], "BLOCK")
            self.assertFalse(report["superiority_claim_allowed"])
            health = store.health(
                benchmark_policy, campaign_id, authority, now=103,
            )
            self.assertEqual(health.status, "healthy")
            self.assertTrue(health.report_ready)
            self.assertEqual(authority.sign_calls, sign_calls + 2)
            self.assertEqual(
                report["claim_block_reasons"],
                ["eu_target_locale_coverage_incomplete"],
            )

            expired_at = benchmark_policy.valid_until + 1
            before_expired_load = connection.total_changes
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.load_report(
                    benchmark_policy, campaign_id, authority, now=expired_at,
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.validity_expired",
            )
            self.assertEqual(connection.total_changes, before_expired_load)
            expired_health = store.health(
                benchmark_policy, campaign_id, authority, now=expired_at,
            )
            self.assertEqual(expired_health.status, "blocked")
            self.assertEqual(
                expired_health.reasons,
                ("benchmark.campaign.validity_expired",),
            )
            self.assertFalse(expired_health.report_ready)

            connection.execute("""
                UPDATE benchmark_campaign_reports SET report_sha256 = ?
                WHERE campaign_id = ?
            """, ("0" * 64, campaign_id))
            connection.commit()
            self.assertFalse(store.report_finalization_required(
                benchmark_policy, campaign_id,
            ))
            blocked = store.health(
                benchmark_policy, campaign_id, authority, now=104,
            )
            self.assertEqual(blocked.status, "blocked")
            self.assertEqual(
                blocked.reasons,
                ("benchmark.campaign.report_invalid",),
            )
            self.assertFalse(blocked.report_ready)
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.summarize(
                    benchmark_policy, campaign_id, authority, now=104,
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.report_invalid",
            )
            self.assertEqual(authority.sign_calls, sign_calls + 2)
            with self.assertRaises(CAMPAIGN.BenchmarkCampaignBlocked) as caught:
                store.load_report(
                    benchmark_policy, campaign_id, authority, now=104,
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark.campaign.report_invalid",
            )
            self.assertEqual(authority.sign_calls, sign_calls + 2)

    def test_report_finalization_retries_durably_and_stops_at_attempt_limit(self):
        benchmark_policy = campaign_policy()
        authority = CountingCampaignAuthority()
        verifier = CampaignNativeReferenceVerifier()
        reviewer = CampaignCandidateReviewer()
        current_time = [100.0]
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(
                benchmark_policy, max_attempts=2, now=current_time[0],
            )
            while CAMPAIGN.run_next_benchmark_case(
                store,
                benchmark_policy,
                campaign_id,
                "case-worker",
                lambda payload: campaign_inputs(
                    payload, benchmark_policy, verifier, authority,
                ),
                reviewer,
                blinding_key=self.key,
                native_reference_verifier=verifier,
                evidence_authority=authority,
                clock=lambda: current_time[0],
            ) is not None:
                pass

            with mock.patch.object(
                store,
                "summarize",
                side_effect=RuntimeError("private signing failure"),
            ) as summarize:
                first = CAMPAIGN.run_benchmark_report_finalization(
                    store,
                    benchmark_policy,
                    campaign_id,
                    "report-worker",
                    authority,
                    clock=lambda: current_time[0],
                    retry_base_seconds=5,
                    retry_max_seconds=20,
                )
                self.assertEqual(first.status, "retry_wait")
                self.assertEqual(first.attempt, 1)
                self.assertEqual(first.next_attempt_at, 105)
                self.assertEqual(
                    first.error_code,
                    "benchmark.campaign.report_unexpected",
                )

                current_time[0] = 104
                self.assertIsNone(CAMPAIGN.run_benchmark_report_finalization(
                    store,
                    benchmark_policy,
                    campaign_id,
                    "report-worker",
                    authority,
                    clock=lambda: current_time[0],
                    retry_base_seconds=5,
                    retry_max_seconds=20,
                ))

                current_time[0] = 105
                second = CAMPAIGN.run_benchmark_report_finalization(
                    store,
                    benchmark_policy,
                    campaign_id,
                    "report-worker",
                    authority,
                    clock=lambda: current_time[0],
                    retry_base_seconds=5,
                    retry_max_seconds=20,
                )
                self.assertEqual(second.status, "failed")
                self.assertEqual(second.attempt, 2)
                self.assertEqual(summarize.call_count, 2)

                current_time[0] = 200
                self.assertIsNone(CAMPAIGN.run_benchmark_report_finalization(
                    store,
                    benchmark_policy,
                    campaign_id,
                    "report-worker",
                    authority,
                    clock=lambda: current_time[0],
                    retry_base_seconds=5,
                    retry_max_seconds=20,
                ))
                self.assertEqual(summarize.call_count, 2)

            state_json = json.dumps(dict(connection.execute("""
                SELECT * FROM benchmark_campaign_report_state
                WHERE campaign_id = ?
            """, (campaign_id,)).fetchone()))
            self.assertNotIn("private signing failure", state_json)
            health = store.health(
                benchmark_policy, campaign_id, authority, now=200,
            )
            self.assertEqual(health.status, "blocked")
            self.assertIn("benchmark.campaign.report_failed", health.reasons)
            self.assertIn(
                "benchmark.campaign.report_error."
                "benchmark.campaign.report_unexpected",
                health.reasons,
            )

    def test_report_expiry_during_finalization_blocks_before_signing(self):
        benchmark_policy = campaign_policy(valid_until=101)
        authority = CountingCampaignAuthority()
        verifier = CampaignNativeReferenceVerifier()
        reviewer = CampaignCandidateReviewer()
        with sqlite3.connect(":memory:") as connection:
            store = CAMPAIGN.BenchmarkCampaignStore(connection)
            campaign_id = store.create(benchmark_policy, now=100)
            while CAMPAIGN.run_next_benchmark_case(
                store,
                benchmark_policy,
                campaign_id,
                "case-worker",
                lambda payload: campaign_inputs(
                    payload, benchmark_policy, verifier, authority,
                ),
                reviewer,
                blinding_key=self.key,
                native_reference_verifier=verifier,
                evidence_authority=authority,
                clock=lambda: 100,
            ) is not None:
                pass
            sign_calls = authority.sign_calls
            clock_values = iter((100, 100, 102, 102))

            outcome = CAMPAIGN.run_benchmark_report_finalization(
                store,
                benchmark_policy,
                campaign_id,
                "report-worker",
                authority,
                clock=lambda: next(clock_values),
            )

            self.assertEqual(outcome.status, "failed")
            self.assertEqual(
                outcome.error_code,
                "benchmark.campaign.validity_expired",
            )
            self.assertEqual(authority.sign_calls, sign_calls)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM benchmark_campaign_reports"
            ).fetchone()[0], 0)
            health = store.health(
                benchmark_policy, campaign_id, authority, now=102,
            )
            self.assertEqual(health.status, "blocked")
            self.assertIn("benchmark.campaign.report_failed", health.reasons)
            self.assertIn(
                "benchmark.campaign.validity_expired", health.reasons,
            )
            self.assertFalse(health.report_ready)


if __name__ == "__main__":
    unittest.main()
