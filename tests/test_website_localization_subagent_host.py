"""Synthetic host-side protocol tests; not native-language quality evidence."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

import test_website_localization_subagents as BASE


HOST = BASE.load(
    "test_website_localization_subagent_host_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_host.py",
)
HTTP = BASE.load(
    "blun_website_localization_subagent_http",
    BASE.ROOT / "integrations" / "website_localization_subagent_http.py",
)
RESPONSE = BASE.load(
    "test_response_subagent_host_review",
    BASE.ROOT / "integrations" / "response_subagent_review.py",
)


TOKEN = "host-bearer-token-with-at-least-32-characters"
SECRET = "host-attestation-secret-with-at-least-43-characters"
KEY_ID = "host-key-1"
HOST_ID = "review-host-1"


class Verifier:
    def verify(self, payload, attestation):
        expected = hmac.new(SECRET.encode("ascii"), payload, hashlib.sha256).hexdigest()
        return (
            attestation == {
                "schema": HTTP.ATTESTATION_SCHEMA,
                "algorithm": "hmac-sha256",
                "key_id": KEY_ID,
                "signature": expected,
            }
        )


class WSGITransport:
    def __init__(self, application, *, token=TOKEN):
        self.application, self.token = application, token
        self.responses = []

    def post(self, url, headers, body, *, timeout):
        parsed = urllib.parse.urlsplit(url)
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": parsed.path,
            "wsgi.url_scheme": parsed.scheme, "SERVER_NAME": parsed.hostname,
            "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
        }
        for name, value in headers.items():
            normalized = name.upper().replace("-", "_")
            if normalized == "CONTENT_TYPE":
                environ["CONTENT_TYPE"] = value
            elif normalized != "CONTENT_LENGTH":
                environ["HTTP_" + normalized] = value
        if self.token is not None:
            environ["HTTP_AUTHORIZATION"] = "Bearer " + self.token
        result = {}

        def start_response(status, response_headers):
            result["status"] = int(status.split()[0])
            result["headers"] = tuple(response_headers)

        response = b"".join(self.application(environ, start_response))
        self.responses.append(response)
        return HTTP.HTTPResult(result["status"], result["headers"], response)


class CaptureHost:
    def __init__(self):
        self.task = self.control = None

    def run_isolated(self, task, *, control):
        self.task, self.control = task, control
        raise TimeoutError

    def verify_execution(self, receipt, *, control):
        return False


def response_request(locale="fi-FI", target=None):
    target = target or BASE.TARGETS[locale]
    capture = CaptureHost()
    reviewer = RESPONSE.ResponseSubagentReviewer(
        capture, model_id="review-model", model_version="model-1",
        host_policy_version="host-policy-1",
        quality_profile_version="eu-native-1", prompt_version="native-prompt-1",
        software_version="6.187.0",
        native_brief={"audience": "Website users",
                      "tone_profile": "Natural and clear",
                      "target_terms": ["BLUN"]},
    )
    with unittest.TestCase().assertRaises(RESPONSE.ResponseReviewBlocked):
        reviewer.review(
            target, locale, "prose",
            creator_id_sha256=hashlib.sha256(b"creator").hexdigest(),
            creator_session_id_sha256=hashlib.sha256(b"creator-session").hexdigest(),
        )
    return capture.task, capture.control


def response_route(task, *, agent="reviewer-native"):
    return HOST.PinnedReviewRoute(
        route_id="ordinary-" + task["input"]["target"]["locale"],
        schema=task["schema"], phase=task["phase"],
        target_locale=task["input"]["target"]["locale"],
        content_type=task["input"]["content_type"],
        task_policy_sha256=HOST.task_policy_sha256(task),
        model_id="review-model", model_version="model-1",
        host_policy_version="host-policy-1",
        reviewer_agent_id=agent, reviewer_role="target-native-reviewer",
    )


def website_routes(locales=None):
    routes, captures = [], {}
    for locale in (locales or BASE.TARGETS):
        capture = BASE.LedgerHost()
        BASE.HostSubagentTests().execute(BASE.adapter(capture), locale)
        captures[locale] = capture
        for task, _control in capture.calls:
            role = ("target-native-reviewer" if task["phase"] == "target_native"
                    else "source-fidelity-reviewer")
            routes.append(HOST.PinnedReviewRoute(
                route_id=f"website-{locale}-{task['phase']}",
                schema=task["schema"], phase=task["phase"],
                target_locale=locale,
                content_type=task["input"]["content_type"],
                task_policy_sha256=HOST.task_policy_sha256(task),
                model_id="configured-model", model_version="model-1",
                host_policy_version="isolated-host-1",
                reviewer_agent_id="reviewer:" + task["phase"],
                reviewer_role=role,
            ))
    return routes, captures


class FixtureLauncher:
    def __init__(self):
        self.calls, self.completed = [], {}
        self.block = None
        self.wrong_identity = False
        self._lock = threading.RLock()
        self._running = {}

    def _execution(self, assignment, task):
        locale = task["input"]["target"]["locale"]
        if task["schema"] == HOST.RESPONSE_REVIEW_SCHEMA:
            response = {
                "schema": HOST.RESPONSE_NATIVE_SCHEMA,
                "phase": task["phase"], "locale": locale,
                "status": "PASS", "confidence": "high",
                "findings": [], "uncertainties": [],
            }
        else:
            if task["input"]["response_schema"]["schema"] == "translate-native.native-rewrite-review.v2":
                response = {
                    "schema": "translate-native.native-rewrite-review.v2",
                    "phase": task["phase"], "locale": locale, "status": "PASS",
                    "confidence": "high", "blocking_defects": [],
                    "major_defects": [], "uncertainties": [],
                }
                if task["phase"] == "target_native":
                    response["holistic_assessment"] = {
                        "reads_as_native_original": True,
                        "reason": "Synthetic fixture marks the complete candidate as native.",
                        "repair_scope": "none",
                    }
            else:
                response = BASE.review(locale, task["phase"], confidence="high")
            if (task["phase"] == HOST.FIDELITY_PHASE
                    and task["input"]["content_type"] == "commercial"):
                from test_commercial_localization import evidence
                response["commercial_review"] = evidence(
                    task["input"]["source"]["text"],
                    task["input"]["candidate"],
                )
        return {
            "response": response,
            "execution_key": assignment.execution_key,
            "phase": assignment.phase,
            "reviewer_role": assignment.reviewer_role,
            "agent_id": ("wrong-reviewer" if self.wrong_identity
                         else assignment.reviewer_agent_id),
            "session_id": assignment.reviewer_session_id,
            "model_id": assignment.model_id,
            "model_version": assignment.model_version,
            "inherit_context": False, "tools": [], "max_delegation_depth": 0,
            "usage": {
                "execute_request_sha256": assignment.execution_key,
                "cost_unit": assignment.cost_unit, "cost_units": 1,
                "input_bytes": len(HOST._raw(task)), "output_tokens": 1,
            },
        }

    def execute_idempotent(self, assignment, model_input, *, deadline_seconds,
                           max_output_tokens):
        with self._lock:
            existing = self.completed.get(assignment.execution_key)
            if existing is not None:
                return HOST._copy(existing)
            wait = self._running.get(assignment.execution_key)
            owner = wait is None
            if owner:
                wait = self._running[assignment.execution_key] = threading.Event()
        if not owner:
            if not wait.wait(2):
                raise RuntimeError("synthetic idempotent launch wait expired")
            with self._lock:
                return HOST._copy(self.completed[assignment.execution_key])
        self.calls.append((assignment, HOST._copy(model_input),
                           deadline_seconds, max_output_tokens))
        if self.block:
            self.block[0].set()
            self.block[1].wait(2)
        result = self._execution(assignment, model_input)
        with self._lock:
            self.completed[assignment.execution_key] = result
            self._running.pop(assignment.execution_key).set()
        return result

    def reconcile(self, assignment, model_input, **_budgets):
        result = self.completed.get(assignment.execution_key)
        return {"status": "completed" if result else "not_started",
                "execution": result}


class LostReplyLauncher(FixtureLauncher):
    def __init__(self):
        super().__init__()
        self.lost = False

    def execute_idempotent(self, assignment, model_input, **budgets):
        result = super().execute_idempotent(assignment, model_input, **budgets)
        if not self.lost:
            self.lost = True
            raise RuntimeError("synthetic lost reply")
        return result


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class PausingLedger(HOST.SQLiteReviewLedger):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started, self.release = threading.Event(), threading.Event()
        self._paused = False

    def mark_started(self, lease):
        super().mark_started(lease)
        if not self._paused:
            self._paused = True
            self.started.set()
            self.release.wait(2)


class HostEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "reviews.sqlite3"
        self.signer = HOST.HMACAttestationSigner(SECRET, KEY_ID)

    def application(self, routes, launcher=None, ledger=None):
        launcher = launcher or FixtureLauncher()
        ledger = ledger or HOST.SQLiteReviewLedger(self.db, lease_seconds=65)
        app = HOST.ReviewHostApplication(
            host_id=HOST_ID, bearer_token=TOKEN, signer=self.signer,
            policy=HOST.PinnedReviewPolicy(routes), ledger=ledger,
            launcher=launcher, allow_loopback_http=True,
        )
        return app, launcher, ledger

    @staticmethod
    def client(application):
        return HTTP.HTTPSReviewHost(
            "http://127.0.0.1/v1/subagent-reviews",
            lambda: {"Authorization": "Bearer " + TOKEN}, Verifier(),
            host_id=HOST_ID, transport=WSGITransport(application),
            allow_loopback_http=True,
        )

    def reviewer(self, application, locale="fi-FI"):
        return RESPONSE.ResponseSubagentReviewer(
            self.client(application), model_id="review-model",
            model_version="model-1", host_policy_version="host-policy-1",
            quality_profile_version="eu-native-1",
            prompt_version="native-prompt-1", software_version="6.187.0",
            native_brief={"audience": "Website users",
                          "tone_profile": "Natural and clear",
                          "target_terms": ["BLUN"]},
        )

    def test_ordinary_finnish_and_maltese_roundtrip_through_actual_adapter(self):
        tasks = [response_request(locale)[0] for locale in BASE.TARGETS]
        app, launcher, ledger = self.application([response_route(task) for task in tasks])
        reviewer = self.reviewer(app)
        for locale, target in BASE.TARGETS.items():
            with self.subTest(locale=locale):
                reviewed = reviewer.review(
                    target, locale, "prose",
                    creator_id_sha256=hashlib.sha256(b"creator").hexdigest(),
                    creator_session_id_sha256=hashlib.sha256(
                        b"creator-session"
                    ).hexdigest(),
                )
                self.assertRegex(reviewed["evidence_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(launcher.calls), 2)
        self.assertEqual(ledger.count(), 2)
        for _assignment, model_input, _deadline, _tokens in launcher.calls:
            serialized = json.dumps(model_input)
            self.assertNotIn("creator_id", serialized)
            self.assertNotIn("source", model_input["input"])

    def test_translation_runs_ordered_native_and_fidelity_reviewers(self):
        routes, _captures = website_routes()
        app, launcher, _ledger = self.application(routes)
        for locale in BASE.TARGETS:
            with self.subTest(locale=locale):
                provider = BASE.adapter(self.client(app))
                result = BASE.HostSubagentTests().execute(provider, locale)
                self.assertTrue(result["release_required"])
        phases = [call[0].phase for call in launcher.calls]
        self.assertEqual(phases, ["target_native", "source_fidelity"] * 2)
        for index in range(0, len(launcher.calls), 2):
            native, fidelity = launcher.calls[index], launcher.calls[index + 1]
            self.assertNotIn("source", native[1]["input"])
            self.assertIn("source", fidelity[1]["input"])
            for internal in ("job_id", "policy_version", "glossary_version"):
                self.assertNotIn(internal, fidelity[1]["input"])
            self.assertNotEqual(native[0].reviewer_agent_id,
                                fidelity[0].reviewer_agent_id)
            self.assertNotEqual(native[0].reviewer_session_id,
                                fidelity[0].reviewer_session_id)

    def test_commercial_worker_roundtrip_preserves_and_validates_evidence(self):
        from test_commercial_localization import SOURCE, TARGET, evidence

        capture = BASE.LedgerHost()
        original = capture.run_isolated

        def capture_commercial(task, *, control):
            reply = original(task, control=control)
            if task["phase"] == HOST.FIDELITY_PHASE:
                reply["response"]["commercial_review"] = evidence(SOURCE, TARGET)
                reply["receipt"]["response_sha256"] = BASE.SUB._hash(
                    reply["response"],
                )
                capture.ledger[control["execution_key"]] = BASE.SUB._copy(reply)
            return reply

        capture.run_isolated = capture_commercial
        fixture_provider = BASE.adapter(capture)
        fixture_provider._creator.invoke = lambda _request: BASE.candidate(
            "sv-SE", TARGET,
        )
        event = BASE.event_for(fixture_provider, "sv-SE")
        event["localization"].update(
            content_type="commercial", source_text=SOURCE,
        )
        job = BASE.PLANNER.plan_from_mapping(
            event["localization"],
        ).jobs[0].as_payload()
        BASE.WORKER.run_localization_job(job, BASE.worker_assets(), fixture_provider)

        routes = []
        for task, _control in capture.calls:
            routes.append(HOST.PinnedReviewRoute(
                route_id="commercial-sv-SE-" + task["phase"],
                schema=task["schema"], phase=task["phase"],
                target_locale="sv-SE", content_type="commercial",
                task_policy_sha256=HOST.task_policy_sha256(task),
                model_id="configured-model", model_version="model-1",
                host_policy_version="isolated-host-1",
                reviewer_agent_id="reviewer:" + task["phase"],
                reviewer_role=(
                    "target-native-reviewer"
                    if task["phase"] == HOST.NATIVE_PHASE
                    else "source-fidelity-reviewer"
                ),
            ))
        app, launcher, _ledger = self.application(routes)
        provider = BASE.adapter(self.client(app))
        provider._creator.invoke = lambda _request: BASE.candidate("sv-SE", TARGET)
        result = BASE.WORKER.run_localization_job(
            job, BASE.worker_assets(), provider,
        )
        self.assertEqual(result["commercial_review"]["status"], "verified")
        fidelity_task = launcher.calls[-1][1]["input"]
        self.assertIn("commercial_review_evidence_contract", fidelity_task)

    def test_authentication_precedes_body_read_and_ledger_access(self):
        task, _control = response_request()
        app, launcher, ledger = self.application([response_route(task)])

        class Poison:
            def read(self, _size):
                raise AssertionError("unauthenticated body was read")

        status = {}
        response = b"".join(app({
            "REQUEST_METHOD": "POST", "PATH_INFO": HOST.PATH,
            "wsgi.url_scheme": "https", "SERVER_NAME": "review.example",
            "HTTP_AUTHORIZATION": "Bearer wrong", "CONTENT_LENGTH": "5",
            "wsgi.input": Poison(),
        }, lambda value, _headers: status.setdefault("value", value)))
        self.assertEqual(status["value"], "401 Unauthorized")
        self.assertEqual(json.loads(response)["error"]["code"],
                         "review_host.authentication_rejected")
        self.assertEqual(ledger.count(), 0)
        self.assertEqual(launcher.calls, [])

    def test_nested_native_metadata_change_is_rejected_before_launch(self):
        task, control = response_request()
        app, launcher, _ledger = self.application([response_route(task)])
        changed = HOST._copy(task)
        changed["input"]["quality_profile"]["source_excerpt"] = "hidden source"
        changed_control = {**control, "task_sha256": HTTP._sha(changed)}
        with self.assertRaises(HTTP.HTTPReviewHostFailed):
            self.client(app).run_isolated(changed, control=changed_control)
        self.assertEqual(launcher.calls, [])

    def test_exact_replay_is_byte_identical_and_runs_once(self):
        task, _control = response_request()
        app, launcher, _ledger = self.application([response_route(task)])
        reviewer = self.reviewer(app)
        arguments = dict(
            target_text=BASE.TARGETS["fi-FI"], target_locale="fi-FI",
            content_type="prose",
            creator_id_sha256=hashlib.sha256(b"creator").hexdigest(),
            creator_session_id_sha256=hashlib.sha256(b"creator-session").hexdigest(),
        )
        first = reviewer.review(**arguments)
        second = reviewer.review(**arguments)
        self.assertEqual(first, second)
        self.assertEqual(len(launcher.calls), 1)

    def test_lost_reply_is_reconciled_without_a_second_launch(self):
        task, control = response_request()
        launcher = LostReplyLauncher()
        app, launcher, _ledger = self.application([response_route(task)], launcher)
        client = self.client(app)
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as lost:
            client.run_isolated(task, control=control)
        self.assertTrue(lost.exception.retryable)
        result = client.run_isolated(task, control=control)
        self.assertEqual(result["response"]["status"], "PASS")
        self.assertEqual(len(launcher.calls), 1)

    def test_concurrent_duplicate_does_not_start_twice(self):
        task, control = response_request()
        launcher = FixtureLauncher()
        launcher.block = (threading.Event(), threading.Event())
        app, launcher, _ledger = self.application([response_route(task)], launcher)
        outcomes = []

        def first():
            try:
                outcomes.append(self.client(app).run_isolated(task, control=control))
            except Exception as error:
                outcomes.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(launcher.block[0].wait(1))
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as duplicate:
            self.client(app).run_isolated(task, control=control)
        self.assertTrue(duplicate.exception.retryable)
        launcher.block[1].set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(len(launcher.calls), 1)

    def test_expired_old_worker_cannot_duplicate_the_physical_model_start(self):
        task, control = response_request()
        clock = Clock()
        ledger = PausingLedger(self.db, clock=clock, lease_seconds=5)
        launcher = FixtureLauncher()
        app, launcher, _ledger = self.application(
            [response_route(task)], launcher, ledger,
        )
        outcomes = []

        def old_worker():
            try:
                outcomes.append(self.client(app).run_isolated(task, control=control))
            except Exception as error:
                outcomes.append(error)

        thread = threading.Thread(target=old_worker)
        thread.start()
        self.assertTrue(ledger.started.wait(1))
        clock.value += 10
        recovered = self.client(app).run_isolated(task, control=control)
        ledger.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(recovered["response"]["status"], "PASS")
        self.assertEqual(len(launcher.calls), 1)
        self.assertTrue(any(isinstance(item, HTTP.HTTPReviewHostFailed)
                            for item in outcomes))

    def test_wrong_actual_identity_is_never_attested(self):
        task, control = response_request()
        launcher = FixtureLauncher()
        launcher.wrong_identity = True
        app, _launcher, ledger = self.application([response_route(task)], launcher)
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as blocked:
            self.client(app).run_isolated(task, control=control)
        self.assertFalse(blocked.exception.retryable)
        self.assertEqual(ledger.count(), 1)
        with sqlite3.connect(self.db) as connection:
            status = connection.execute(
                "SELECT status FROM review_host_executions"
            ).fetchone()[0]
        self.assertEqual(status, "unknown")

    def test_incomplete_review_and_boolean_execution_depth_are_not_attested(self):
        task, control = response_request()

        class InvalidLauncher(FixtureLauncher):
            def __init__(self, mutation):
                super().__init__()
                self.mutation = mutation

            def _execution(self, assignment, model_input):
                execution = super()._execution(assignment, model_input)
                self.mutation(execution)
                return execution

        mutations = (
            lambda execution: execution.update(response={
                "schema": HOST.RESPONSE_NATIVE_SCHEMA,
                "phase": HOST.NATIVE_PHASE,
            }),
            lambda execution: execution.update(max_delegation_depth=False),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                database = Path(self.temporary.name) / f"invalid-{index}.sqlite3"
                launcher = InvalidLauncher(mutation)
                app, _launcher, ledger = self.application(
                    [response_route(task)], launcher,
                    HOST.SQLiteReviewLedger(database, lease_seconds=65),
                )
                with self.assertRaises(HTTP.HTTPReviewHostFailed) as blocked:
                    self.client(app).run_isolated(task, control=control)
                self.assertFalse(blocked.exception.retryable)
                self.assertEqual(ledger.count(), 1)
                with sqlite3.connect(database) as connection:
                    status = connection.execute(
                        "SELECT status FROM review_host_executions"
                    ).fetchone()[0]
                self.assertEqual(status, "unknown")

    def test_fidelity_requires_the_exact_completed_native_predecessor(self):
        routes, captures = website_routes(["fi-FI"])
        app, launcher, ledger = self.application(routes)
        fidelity_task, fidelity_control = captures["fi-FI"].calls[1]
        with self.assertRaises(HTTP.HTTPReviewHostFailed):
            self.client(app).run_isolated(fidelity_task, control=fidelity_control)
        self.assertEqual(ledger.count(), 0)
        self.assertEqual(launcher.calls, [])

    def test_legacy_native_receipt_cannot_precede_rewrite_fidelity(self):
        routes, captures = website_routes(["fi-FI"])
        native_task, native_control = captures["fi-FI"].calls[0]
        legacy_fidelity, legacy_control = captures["fi-FI"].calls[1]
        rewrite_fidelity = HOST._copy(legacy_fidelity)
        rewrite_fidelity["input"]["response_schema"] = {
            "schema": HOST.NATIVE_REWRITE_RESPONSE_SCHEMA,
            "phase": HOST.FIDELITY_PHASE,
            "locale": "fi-FI", "status": "PASS or FAIL",
            "confidence": "high or low",
            "blocking_defects": [{
                "severity": "blocking", "class": "...", "excerpt": "...",
                "reason": "...", "impact": "...",
                "revision_direction": "...",
            }],
            "major_defects": [{
                "severity": "major", "class": "...", "excerpt": "...",
                "reason": "...", "impact": "...",
                "revision_direction": "...",
            }],
            "uncertainties": [{
                "class": "...", "reason": "...", "evidence_needed": "...",
            }],
        }
        routes.append(HOST.PinnedReviewRoute(
            route_id="rewrite-fi-FI-source_fidelity",
            schema=rewrite_fidelity["schema"], phase=HOST.FIDELITY_PHASE,
            target_locale="fi-FI",
            content_type=rewrite_fidelity["input"]["content_type"],
            task_policy_sha256=HOST.task_policy_sha256(rewrite_fidelity),
            model_id=legacy_control["model_id"],
            model_version=legacy_control["model_version"],
            host_policy_version=legacy_control["host_policy_version"],
            reviewer_agent_id="reviewer:rewrite-fidelity",
            reviewer_role="source-fidelity-reviewer",
        ))
        app, launcher, ledger = self.application(routes)
        client = self.client(app)
        native_result = client.run_isolated(native_task, control=native_control)
        rewrite_control = HOST._copy(legacy_control)
        rewrite_control.update(
            task_sha256=HTTP._sha(rewrite_fidelity),
            previous_receipt_sha256=HOST._sha(native_result["receipt"]),
        )
        rewrite_control["execution_key"] = HOST._sha({
            "cross_schema_replay": rewrite_fidelity,
            "previous_receipt_sha256": rewrite_control["previous_receipt_sha256"],
        })
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as blocked:
            client.run_isolated(rewrite_fidelity, control=rewrite_control)
        self.assertFalse(blocked.exception.retryable)
        self.assertEqual(ledger.count(), 1)
        self.assertEqual(len(launcher.calls), 1)

    def test_same_execution_key_with_changed_candidate_conflicts(self):
        task, control = response_request()
        app, launcher, _ledger = self.application([response_route(task)])
        client = self.client(app)
        client.run_isolated(task, control=control)
        changed = HOST._copy(task)
        changed["input"]["candidate"] += " Muutos."
        changed_control = {**control, "task_sha256": HTTP._sha(changed)}
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as conflict:
            client.run_isolated(changed, control=changed_control)
        self.assertFalse(conflict.exception.retryable)
        self.assertEqual(len(launcher.calls), 1)

    def test_host_route_cannot_self_assign_creator_as_reviewer(self):
        task, control = response_request()
        creator_agent = "creator"
        self.assertEqual(
            hashlib.sha256(creator_agent.encode()).hexdigest(),
            control["creator_id_sha256"],
        )
        app, launcher, ledger = self.application([
            response_route(task, agent=creator_agent),
        ])
        with self.assertRaises(HTTP.HTTPReviewHostFailed):
            self.client(app).run_isolated(task, control=control)
        self.assertEqual(ledger.count(), 0)
        self.assertEqual(launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
