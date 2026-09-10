from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_website_localization_http_provider import HTTP
from test_website_localization_runner import IncrementingClock, PLANNER, RUNNER, assets


def plan():
    return PLANNER.plan_website_localization(
        source_id="homepage.hero",
        source_revision="cms-https-1",
        source_text="Build your business with BLUN.",
        source_locale="en-IE",
        content_type="headline",
        glossary_version="blun-glossary-3",
        policy_version="native-web-1",
        provider_id="customer-llm",
        model_id="configured-model",
        model_version="2026-09-09",
        software_version="6.43.0-dev",
        target_locales=["fi-FI"],
    )


class ScriptedTransport:
    def __init__(self, statuses=()):
        self.statuses = list(statuses)
        self.calls = []

    def post(self, url, headers, body, *, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return HTTP.HTTPResult(status, (), b"")
        request_envelope = json.loads(body.decode("utf-8"))
        request = request_envelope["request"]
        locale = request["input"]["target"]["locale"]
        if request["phase"] == "transcreation":
            response = {
                "schema": "blun.website-localization-candidate.v1",
                "phase": "transcreation",
                "locale": locale,
                "candidate": "Kasvata yritystäsi BLUN-palvelun avulla.",
            }
        else:
            response = {
                "schema": "blun.website-localization-review.v2",
                "phase": request["phase"],
                "locale": locale,
                "status": "PASS",
                "confidence": "high",
                "blocking_defects": [],
                "major_defects": [],
            }
        response_envelope = {
            "schema": HTTP.RESPONSE_SCHEMA,
            "request_id": request_envelope["request_id"],
            "request_sha256": request_envelope["request_sha256"],
            "response": response,
        }
        raw = json.dumps(
            response_envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return HTTP.HTTPResult(
            200,
            (("Content-Type", "application/json; charset=utf-8"),),
            raw,
        )


def adapter(transport):
    return HTTP.HTTPProviderAdapter(
        "https://models.example.test/v1/localize",
        lambda: {"Authorization": "Bearer host-owned-token"},
        transport=transport,
    )


class WebsiteLocalizationHTTPProviderQueueTests(unittest.TestCase):
    def test_disk_queue_runs_all_three_bound_phases_after_restart(self):
        current = plan()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "localization.sqlite3"
            connection = sqlite3.connect(path)
            RUNNER._QUEUE.LocalizationQueue(connection).enqueue_plan(current, now=90)
            connection.close()

            connection = sqlite3.connect(path)
            queue = RUNNER._QUEUE.LocalizationQueue(connection)
            transport = ScriptedTransport()
            outcome = RUNNER.run_next_localization_job(
                queue,
                "worker-https",
                lambda _payload: adapter(transport),
                lambda _payload: assets(),
                clock=IncrementingClock(),
                lease_seconds=10,
            )
            self.assertEqual(outcome.status, "succeeded")
            self.assertEqual(outcome.target_locale, "fi-FI")
            self.assertEqual(queue.result(current.jobs[0].job_id)["candidate"],
                             "Kasvata yritystäsi BLUN-palvelun avulla.")
            connection.close()

            envelopes = [json.loads(call[2].decode("utf-8")) for call in transport.calls]
            self.assertEqual(
                [item["request"]["phase"] for item in envelopes],
                ["transcreation", "target_native", "source_fidelity"],
            )
            native_input = envelopes[1]["request"]["input"]
            self.assertNotIn("source", native_input)
            self.assertNotIn("Build your business", json.dumps(native_input, ensure_ascii=False))
            self.assertEqual(
                envelopes[2]["request"]["input"]["source"]["text"],
                "Build your business with BLUN.",
            )

            connection = sqlite3.connect(path)
            queue = RUNNER._QUEUE.LocalizationQueue(connection)
            self.assertEqual(queue.status(current.jobs[0].job_id).status, "succeeded")
            connection.close()

    def test_retryable_http_failure_is_retried_only_by_the_queue(self):
        current = plan()
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(Path(directory) / "localization.sqlite3")
            queue = RUNNER._QUEUE.LocalizationQueue(connection)
            queue.enqueue_plan(current, max_attempts=2, now=90)

            failed_transport = ScriptedTransport((503,))
            first = RUNNER.run_next_localization_job(
                queue,
                "worker-https",
                lambda _payload: adapter(failed_transport),
                lambda _payload: assets(),
                clock=IncrementingClock(),
                lease_seconds=10,
                retry_base_seconds=5,
                retry_max_seconds=60,
            )
            self.assertEqual(first.status, "retry_wait")
            self.assertEqual(first.error_code, "provider.http_status")
            self.assertEqual(len(failed_transport.calls), 1)

            successful_transport = ScriptedTransport()
            second = RUNNER.run_next_localization_job(
                queue,
                "worker-https",
                lambda _payload: adapter(successful_transport),
                lambda _payload: assets(),
                clock=IncrementingClock(start=first.next_attempt_at),
                lease_seconds=10,
                retry_base_seconds=5,
                retry_max_seconds=60,
            )
            self.assertEqual(second.status, "succeeded")
            self.assertEqual(len(successful_transport.calls), 3)
            self.assertEqual(queue.status(current.jobs[0].job_id).attempts, 2)
            connection.close()


if __name__ == "__main__":
    unittest.main()
