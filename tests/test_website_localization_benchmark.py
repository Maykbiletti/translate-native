from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import importlib.util
import json
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


PLANNER = load("blun_test_benchmark_planner", ROOT / "integrations" / "website_localization.py")
WORKER = load("blun_test_benchmark_worker", ROOT / "integrations" / "website_localization_worker.py")
BENCHMARK = load("blun_test_website_localization_benchmark", ROOT / "integrations" / "website_localization_benchmark.py")
SUITE = load("blun_test_website_localization_benchmark_suite", ROOT / "integrations" / "website_localization_benchmark_suite.py")
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
        "required_locales": ("mt-MT", "fi-FI"),
        "minimum_cases_per_locale": len(SUITE.SOURCE_CASES),
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
            self.assertEqual(item["one_sided_sign_p"], 0.00390625)
            self.assertTrue(item["suite_complete"])
            self.assertEqual(set(item["content_types"]), set(PLANNER.CONTENT_TYPES))
            self.assertEqual(item["long_form_cases"], 2)
            self.assertGreaterEqual(len(item["domains"]), 6)
            self.assertIn("marketing_calque", item["adversarial_tags"])

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

        blocked_locale = BENCHMARK.EU_BENCHMARK_TARGET_LOCALES[-1]
        replaced_keys = {
            SUITE.SOURCE_CASES[index].as_payload()["key"]
            for index in (0, 1)
        }
        weakened_results = [
            item for item in results
            if not (
                item["target_locale"] == blocked_locale
                and item["suite"]["case_key"] in replaced_keys
            )
        ]
        for index in (0, 1):
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
                    if index < 6
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
            # Six unanimous wins and two split cases made the old joint-only
            # calculation look significant: 6/6 decisive, p=0.015625.
            self.assertEqual(locale_report["candidate_wins"], 6)
            self.assertEqual(locale_report["baseline_wins"], 0)
            self.assertEqual(locale_report["one_sided_sign_p"], 0.015625)
            self.assertEqual(locale_report["status"], "BLOCK")
            axes = {item["phase"]: item for item in locale_report["axes"]}
            self.assertEqual(axes["target_native"]["status"], "PASS")
            self.assertEqual(axes["target_native"]["candidate_wins"], 8)
            self.assertEqual(axes["source_fidelity"]["status"], "BLOCK")
            self.assertEqual(axes["source_fidelity"]["candidate_wins"], 6)
            self.assertEqual(axes["source_fidelity"]["baseline_wins"], 2)
            self.assertEqual(
                axes["source_fidelity"]["one_sided_sign_p"], 0.14453125,
            )
            self.assertEqual(
                axes["source_fidelity"]["block_reasons"],
                ["not_statistically_significant"],
            )
        self.assertFalse(report["superiority_claim_allowed"])

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


if __name__ == "__main__":
    unittest.main()
