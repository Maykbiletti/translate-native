"""Synthetic HTTPS-host fixtures; not evidence of native-language quality."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import unittest

import test_website_localization_subagents as BASE


HTTP = BASE.load(
    "test_host_subagent_http",
    BASE.ROOT / "integrations" / "website_localization_subagent_http.py",
)
RESPONSE = BASE.load(
    "test_response_subagent_http_review",
    BASE.ROOT / "integrations" / "response_subagent_review.py",
)


class FixtureAuthority:
    """Test-only HMAC authority standing in for a deployment trust verifier."""

    algorithm = "hmac-sha256-fixture"
    key_id = "fixture-host-key-1"

    def __init__(self):
        self.key = b"test-only-host-attestation-key"

    def sign(self, payload):
        return {
            "schema": HTTP.ATTESTATION_SCHEMA,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "signature": hmac.new(self.key, payload, hashlib.sha256).hexdigest(),
        }

    def verify(self, payload, attestation):
        expected = self.sign(payload)
        return (attestation.get("schema") == expected["schema"]
                and attestation.get("algorithm") == expected["algorithm"]
                and attestation.get("key_id") == expected["key_id"]
                and hmac.compare_digest(attestation.get("signature", ""),
                                        expected["signature"]))


class FixtureHostTransport:
    """Authenticated remote-host double with a durable execution-key ledger."""

    def __init__(self, authority, *, status=200, mutate=None, raise_error=None):
        self.authority, self.status = authority, status
        self.mutate, self.raise_error = mutate, raise_error
        self.calls, self.ledger = [], {}

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.raise_error:
            raise self.raise_error
        if self.status != 200:
            return HTTP.HTTPResult(self.status, (), b"")
        request = json.loads(body.decode("utf-8"))
        key, control, task = request["execution_key"], request["control"], request["task"]
        if key in self.ledger:
            response_body = self.ledger[key]
        else:
            phase, locale = task["phase"], task["input"]["target"]["locale"]
            if task["schema"] == RESPONSE.SCHEMA:
                response = {
                    "schema": RESPONSE.RESPONSE_SCHEMA,
                    "phase": phase,
                    "locale": locale,
                    "status": "PASS",
                    "confidence": "high",
                    "findings": [],
                    "uncertainties": [],
                }
                response_sha256 = RESPONSE._hash(response)
            else:
                response = BASE.review(locale, phase)
                response_sha256 = BASE.SUB._hash(response)
            bound = {name: control[name] for name in {
                "schema", "execution_key", "request_sha256", "task_sha256", "phase",
                "previous_receipt_sha256", "model_id", "model_version",
                "inherit_context", "tools", "max_delegation_depth",
            } | ({"reviewer_role", "assignment_id"}
                 if task["schema"] == RESPONSE.SCHEMA else set())}
            receipt = {**bound, "response_sha256": response_sha256,
                       "agent_id": "https-reviewer:" + phase,
                       "session_id": "https-session:" + key}
            result = {"response": response, "receipt": receipt}
            signed = {
                "schema": HTTP.ATTESTATION_PAYLOAD_SCHEMA,
                "host_id": request["host_id"], "execution_key": key,
                "request_sha256": request["request_sha256"],
                "result_sha256": HTTP._sha(result), "completed": True,
            }
            envelope = {
                "schema": HTTP.RESPONSE_SCHEMA, "host_id": request["host_id"],
                "execution_key": key, "request_sha256": request["request_sha256"],
                "result": result,
                "attestation": self.authority.sign(HTTP._raw(
                    signed, code="fixture", maximum=HTTP.MAX_RESPONSE_BYTES)),
            }
            if self.mutate:
                self.mutate(envelope, request)
            response_body = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")
            self.ledger[key] = response_body
        return HTTP.HTTPResult(200, (("Content-Type", "application/json; charset=utf-8"),
                                     ("Content-Length", str(len(response_body)))), response_body)


class ConflictingConcurrentTransport(FixtureHostTransport):
    """Return two validly attested identities for one execution key."""

    def __init__(self, authority):
        super().__init__(authority)
        self.barrier = threading.Barrier(2)
        self.counter = 0
        self.counter_lock = threading.Lock()

    def post(self, url, headers, body, *, timeout):
        self.barrier.wait(2)
        base = super().post(url, headers, body, timeout=timeout)
        with self.counter_lock:
            self.counter += 1
            number = self.counter
        envelope = json.loads(base.body.decode("utf-8"))
        receipt = envelope["result"]["receipt"]
        receipt["agent_id"] = f"https-reviewer-conflict-{number}"
        receipt["session_id"] = f"https-session-conflict-{number}"
        signed = {
            "schema": HTTP.ATTESTATION_PAYLOAD_SCHEMA,
            "host_id": envelope["host_id"],
            "execution_key": envelope["execution_key"],
            "request_sha256": envelope["request_sha256"],
            "result_sha256": HTTP._sha(envelope["result"]),
            "completed": True,
        }
        envelope["attestation"] = self.authority.sign(HTTP._raw(
            signed, code="fixture", maximum=HTTP.MAX_RESPONSE_BYTES,
        ))
        encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return HTTP.HTTPResult(
            200,
            (("Content-Type", "application/json; charset=utf-8"),
             ("Content-Length", str(len(encoded)))),
            encoded,
        )


def host(transport=None, authority=None, **kwargs):
    authority = authority or FixtureAuthority()
    transport = transport or FixtureHostTransport(authority)
    return HTTP.HTTPSReviewHost(
        "https://review-host.example/v1/subagent-reviews",
        lambda: {"Authorization": "Bearer fixture-secret"}, authority,
        host_id="trusted-host-1", transport=transport, **kwargs,
    ), transport


def execute(review_host, locale="fi-FI"):
    provider = BASE.adapter(review_host)
    job = BASE.planned(provider, locale).jobs[0].as_payload()
    return BASE.WORKER.run_localization_job(job, BASE.worker_assets(), provider)


class HTTPSReviewHostTests(unittest.TestCase):
    def test_concurrent_conflicting_execution_is_atomically_rejected(self):
        authority = FixtureAuthority()
        transport = ConflictingConcurrentTransport(authority)
        review_host, _unused = host(transport=transport, authority=authority)
        reviewer = RESPONSE.ResponseSubagentReviewer(
            review_host, model_id="review-model", model_version="model-1",
            host_policy_version="isolated-host-1",
            quality_profile_version="eu-native-1",
            prompt_version="native-prompt-1", software_version="6.186.0",
            native_brief={"audience": "Website users",
                          "tone_profile": "Natural and clear",
                          "target_terms": ["BLUN"]},
        )
        outcomes = []

        def run():
            try:
                outcomes.append(reviewer.review(
                    BASE.TARGETS["fi-FI"], "fi-FI", "prose",
                    creator_id_sha256=hashlib.sha256(b"creator-main").hexdigest(),
                    creator_session_id_sha256=hashlib.sha256(
                        b"creator-session"
                    ).hexdigest(),
                ))
            except Exception as error:
                outcomes.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        blocked = [item for item in outcomes if not isinstance(item, dict)]
        self.assertEqual(len(blocked), 1)
        self.assertIsInstance(blocked[0], RESPONSE.ResponseReviewBlocked)
        self.assertEqual(
            blocked[0].code, "response_review.http.idempotency_conflict",
        )

    def test_ordinary_finnish_and_maltese_responses_use_same_authenticated_bridge(self):
        for locale, target in BASE.TARGETS.items():
            with self.subTest(locale=locale):
                review_host, transport = host()
                reviewer = RESPONSE.ResponseSubagentReviewer(
                    review_host, model_id="review-model", model_version="model-1",
                    host_policy_version="isolated-host-1",
                    quality_profile_version="eu-native-1",
                    prompt_version="native-prompt-1", software_version="6.186.0",
                    native_brief={"audience": "Website users",
                                  "tone_profile": "Natural and clear",
                                  "target_terms": ["BLUN"]},
                )
                reviewed = reviewer.review(
                    target, locale, "prose",
                    creator_id_sha256=hashlib.sha256(
                        b"creator-main"
                    ).hexdigest(),
                    creator_session_id_sha256=hashlib.sha256(
                        b"creator-session"
                    ).hexdigest(),
                )
                self.assertRegex(reviewed["evidence_sha256"], r"^[0-9a-f]{64}$")
                request = json.loads(transport.calls[0][2].decode("utf-8"))
                self.assertEqual(request["task"]["schema"], RESPONSE.SCHEMA)
                self.assertEqual(set(request["task"]["input"]), HTTP.NATIVE_INPUT_FIELDS)
                self.assertNotIn("source", request["task"]["input"])
                self.assertEqual(len(transport.calls), 1)
                wrong_task = json.loads(json.dumps(request["task"]))
                wrong_control = json.loads(json.dumps(request["control"]))
                wrong_task["phase"] = "source_fidelity"
                wrong_control["phase"] = "source_fidelity"
                wrong_control["task_sha256"] = HTTP._sha(wrong_task)
                with self.assertRaisesRegex(
                    HTTP.HTTPReviewHostFailed, "http.request_invalid"
                ):
                    HTTP._validate_task(wrong_task, wrong_control)

    def test_finnish_and_maltese_run_through_authenticated_host_bridge(self):
        for locale in BASE.TARGETS:
            with self.subTest(locale=locale):
                review_host, transport = host()
                result = execute(review_host, locale)
                self.assertEqual(result["candidate"], BASE.TARGETS[locale])
                self.assertEqual(len(transport.calls), 2)
                native = json.loads(transport.calls[0][2].decode("utf-8"))
                fidelity = json.loads(transport.calls[1][2].decode("utf-8"))
                serialized = json.dumps(native, ensure_ascii=False)
                for forbidden in ("Build your business", "SOURCE_", '"source"', '"glossary"'):
                    self.assertNotIn(forbidden, serialized)
                self.assertEqual(set(native["task"]["input"]), HTTP.NATIVE_INPUT_FIELDS)
                self.assertEqual(fidelity["task"]["input"]["source"]["text"],
                                 "Build your business with BLUN.")
                native_reply = json.loads(
                    transport.ledger[native["execution_key"]].decode("utf-8"))
                native_evidence = {
                    "schema": HTTP.EVIDENCE_SCHEMA,
                    "host_id": native_reply["host_id"],
                    "execution_key": native_reply["execution_key"],
                    "request_sha256": native_reply["request_sha256"],
                    "result_sha256": HTTP._sha(native_reply["result"]),
                    "receipt": native_reply["result"]["receipt"],
                    "attestation": native_reply["attestation"],
                }
                expected = BASE.SUB._hash({
                    "schema": "translate-native.host-review-commitment.v1",
                    "response": native_reply["result"]["response"],
                    "host_evidence": native_evidence,
                })
                actual = next(item for item in result["quality_passes"]
                              if item["phase"] == "target_native")
                self.assertEqual(actual["response_sha256"], expected)
                for _, headers, body, timeout in transport.calls:
                    request = json.loads(body.decode("utf-8"))
                    self.assertEqual(headers["Idempotency-Key"], request["execution_key"])
                    self.assertEqual(headers["X-Subagent-Request-Sha256"],
                                     request["request_sha256"])
                    self.assertEqual(headers["Authorization"], "Bearer fixture-secret")
                    self.assertLessEqual(timeout, 60)

    def test_lost_response_retry_uses_identical_execution_identity(self):
        authority, transport = FixtureAuthority(), None
        transport = FixtureHostTransport(authority)
        first, _ = host(transport, authority)
        second, _ = host(transport, authority)
        execute(first)
        execute(second)
        self.assertEqual(len(transport.calls), 4)
        for first_call, retry_call in zip(transport.calls[:2], transport.calls[2:]):
            self.assertEqual(first_call[1]["Idempotency-Key"], retry_call[1]["Idempotency-Key"])
            self.assertEqual(first_call[2], retry_call[2])
        self.assertEqual(len(transport.ledger), 2)

    def test_attestation_and_response_binding_fail_closed(self):
        mutations = {
            "signature": lambda e, _: e["attestation"].update(signature="0" * 64),
            "host": lambda e, _: e.update(host_id="other-host"),
            "request": lambda e, _: e.update(request_sha256="0" * 64),
            "execution": lambda e, _: e.update(execution_key="0" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                authority = FixtureAuthority()
                review_host, _ = host(FixtureHostTransport(authority, mutate=mutate), authority)
                with self.assertRaises(BASE.WORKER.LocalizationWorkerBlocked) as raised:
                    execute(review_host)
                self.assertFalse(raised.exception.retryable)

    def test_validly_attested_wrong_receipt_is_still_rejected_by_worker_adapter(self):
        authority = FixtureAuthority()
        def mutate(envelope, request):
            envelope["result"]["receipt"]["phase"] = "source_fidelity"
            signed = {"schema": HTTP.ATTESTATION_PAYLOAD_SCHEMA,
                      "host_id": envelope["host_id"],
                      "execution_key": envelope["execution_key"],
                      "request_sha256": envelope["request_sha256"],
                      "result_sha256": HTTP._sha(envelope["result"]), "completed": True}
            envelope["attestation"] = authority.sign(HTTP._raw(
                signed, code="fixture", maximum=HTTP.MAX_RESPONSE_BYTES))
        review_host, _ = host(FixtureHostTransport(authority, mutate=mutate), authority)
        with self.assertRaisesRegex(BASE.WORKER.LocalizationWorkerBlocked, "receipt_binding"):
            execute(review_host)

    def test_permanent_and_transient_transport_failures_preserve_retryability(self):
        cases = ((401, False, "authentication_rejected"),
                 (302, False, "redirect"), (409, False, "idempotency_conflict"),
                 (429, True, "status"), (503, True, "status"))
        for status, retryable, code in cases:
            with self.subTest(status=status):
                authority = FixtureAuthority()
                review_host, _ = host(FixtureHostTransport(authority, status=status), authority)
                with self.assertRaises(BASE.WORKER.LocalizationWorkerBlocked) as raised:
                    execute(review_host)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertIn("subagents.http." + code, str(raised.exception))

    def test_verified_execution_requires_exact_local_attested_snapshot(self):
        review_host, transport = host()
        execute(review_host)
        request = json.loads(transport.calls[0][2].decode("utf-8"))
        reply = json.loads(transport.ledger[request["execution_key"]].decode("utf-8"))
        receipt, control = reply["result"]["receipt"], request["control"]
        self.assertTrue(review_host.verify_execution(receipt, control=control))
        changed = dict(receipt, agent_id="other")
        self.assertFalse(review_host.verify_execution(changed, control=control))
        bool_changed = dict(receipt, inherit_context=0)
        self.assertFalse(review_host.verify_execution(bool_changed, control=control))
        changed_control = dict(control, host_policy_version="changed-policy")
        self.assertFalse(review_host.verify_execution(receipt, control=changed_control))

    def test_configuration_authentication_and_source_isolation_block_before_network(self):
        authority, transport = FixtureAuthority(), FixtureHostTransport(FixtureAuthority())
        with self.assertRaises(ValueError):
            HTTP.HTTPSReviewHost("http://remote.example/review", lambda: {"X-Key": "x"},
                                 authority, host_id="host", transport=transport)
        review_host = HTTP.HTTPSReviewHost(
            "https://review.example/review", lambda: {"Content-Type": "override"},
            authority, host_id="host", transport=transport)
        provider = BASE.adapter(review_host)
        with self.assertRaises(BASE.WORKER.LocalizationWorkerBlocked) as raised:
            BASE.WORKER.run_localization_job(
                BASE.planned(provider).jobs[0].as_payload(), BASE.worker_assets(), provider)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(transport.calls, [])
        capture = BASE.LedgerHost()
        execute(capture)
        native_task, native_control = capture.calls[0]
        native_task["input"]["source"] = {"text": "must remain isolated"}
        native_control["task_sha256"] = BASE.SUB._hash(native_task)
        clean_host, clean_transport = host()
        with self.assertRaisesRegex(HTTP.HTTPReviewHostFailed, "http.source_isolation"):
            clean_host.run_isolated(native_task, control=native_control)
        self.assertEqual(clean_transport.calls, [])


if __name__ == "__main__":
    unittest.main()
