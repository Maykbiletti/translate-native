"""Synthetic protected-host runtime tests; not native-quality evidence."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_website_localization_subagent_host as ENDPOINT
import test_website_localization_subagents as BASE


RUNTIME = BASE.load(
    "test_website_localization_subagent_host_runtime_impl",
    BASE.ROOT / "integrations" / "website_localization_subagent_host_runtime.py",
)
HTTP = ENDPOINT.HTTP

ACTIVE_LAUNCHER = None
FACTORY_SETTINGS = []


def launcher_factory(settings):
    FACTORY_SETTINGS.append(settings)
    return ACTIVE_LAUNCHER


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        global ACTIVE_LAUNCHER
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        if os.name != "nt":
            self.root.chmod(0o700)
        self.module_path = self.root / "synthetic_review_launcher.py"
        self.module_path.write_text(
            "import importlib\n"
            f"def build(settings):\n    return importlib.import_module({__name__!r}).launcher_factory(settings)\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.module_path.chmod(0o600)
        self.factory_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        self.token_file = self.write("host.token", ENDPOINT.TOKEN)
        self.secret_file = self.write("host.secret", ENDPOINT.SECRET)
        self.launcher_file = self.write_json("launcher.json", {
            "schema": RUNTIME.LAUNCHER_SCHEMA,
            "launcher_id": "synthetic-launcher",
            "launcher_version": "fixture-1",
            "settings": {"endpoint": "synthetic://isolated-reviewers"},
        })
        self.ledger = self.root / "review-host.sqlite3"
        ACTIVE_LAUNCHER = ENDPOINT.FixtureLauncher()
        ACTIVE_LAUNCHER.launcher_id = "synthetic-launcher"
        ACTIVE_LAUNCHER.launcher_version = "fixture-1"
        FACTORY_SETTINGS.clear()

    def write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="ascii")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    @staticmethod
    def route_dict(route):
        return {
            "route_id": route.route_id, "schema": route.schema,
            "phase": route.phase, "target_locale": route.target_locale,
            "content_type": route.content_type,
            "task_policy_sha256": route.task_policy_sha256,
            "model_id": route.model_id, "model_version": route.model_version,
            "host_policy_version": route.host_policy_version,
            "reviewer_agent_id": route.reviewer_agent_id,
            "reviewer_role": route.reviewer_role,
            "max_timeout_seconds": route.max_timeout_seconds,
            "max_output_tokens": route.max_output_tokens,
            "max_input_bytes": route.max_input_bytes,
            "cost_unit": route.cost_unit,
            "max_cost_units": route.max_cost_units,
        }

    def configuration(self, routes, **changes):
        value = {
            "schema": RUNTIME.CONFIG_SCHEMA,
            "host_id": ENDPOINT.HOST_ID,
            "allow_loopback_http": True,
            "authentication": {
                "scheme": "bearer", "token_file": str(self.token_file),
            },
            "attestation": {
                "algorithm": "hmac-sha256", "key_id": ENDPOINT.KEY_ID,
                "secret_file": str(self.secret_file),
            },
            "ledger": {"path": str(self.ledger), "lease_seconds": 65},
            "launcher": {
                "factory_file": str(self.module_path),
                "factory_callable": "build",
                "factory_sha256": self.factory_sha256,
                "launcher_id": "synthetic-launcher",
                "launcher_version": "fixture-1",
                "config_file": str(self.launcher_file),
            },
            "routes": [self.route_dict(route) for route in routes],
        }
        value.update(changes)
        return self.write_json("host.json", value)

    @staticmethod
    def client(runtime):
        return HTTP.HTTPSReviewHost(
            "http://127.0.0.1/v1/subagent-reviews",
            lambda: {"Authorization": "Bearer " + ENDPOINT.TOKEN},
            ENDPOINT.Verifier(), host_id=ENDPOINT.HOST_ID,
            transport=ENDPOINT.WSGITransport(runtime.application),
            allow_loopback_http=True,
        )

    def test_finnish_response_roundtrip_and_restart_replay(self):
        global ACTIVE_LAUNCHER
        task, control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        first_launcher = ACTIVE_LAUNCHER
        try:
            first = self.client(runtime).run_isolated(task, control=control)
        finally:
            runtime.close()
        self.assertEqual(first["response"]["locale"], "fi-FI")
        self.assertEqual(len(first_launcher.calls), 1)
        self.assertEqual(FACTORY_SETTINGS, [
            {"endpoint": "synthetic://isolated-reviewers"},
        ])

        ACTIVE_LAUNCHER = ENDPOINT.FixtureLauncher()
        ACTIVE_LAUNCHER.launcher_id = "synthetic-launcher"
        ACTIVE_LAUNCHER.launcher_version = "fixture-1"
        runtime = RUNTIME.open_review_host_runtime(config)
        try:
            replay = self.client(runtime).run_isolated(task, control=control)
        finally:
            runtime.close()
        self.assertEqual(replay, first)
        self.assertEqual(ACTIVE_LAUNCHER.calls, [])

    def test_maltese_translation_is_source_blind_then_source_aware(self):
        routes, _captures = ENDPOINT.website_routes(["mt-MT"])
        config = self.configuration(routes)
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        try:
            result = BASE.HostSubagentTests().execute(
                BASE.adapter(self.client(runtime)), "mt-MT",
            )
        finally:
            runtime.close()
        self.assertTrue(result["release_required"])
        self.assertEqual(
            [call[0].phase for call in ACTIVE_LAUNCHER.calls],
            ["target_native", "source_fidelity"],
        )
        native = ACTIVE_LAUNCHER.calls[0][1]
        fidelity = ACTIVE_LAUNCHER.calls[1][1]
        self.assertNotIn("source", native["input"])
        self.assertNotIn("Build your business", json.dumps(native))
        self.assertEqual(
            fidelity["input"]["source"]["text"],
            "Build your business with BLUN.",
        )

    def test_deployment_drift_blocks_existing_journal(self):
        task, _control = ENDPOINT.response_request("fi-FI")
        route = ENDPOINT.response_route(task)
        config = self.configuration([route])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        runtime.close()
        changed = self.route_dict(route)
        changed["reviewer_agent_id"] = "different-native-reviewer"
        value = json.loads(config.read_text(encoding="utf-8"))
        value["routes"] = [changed]
        self.write_json("host.json", value)
        with self.assertRaisesRegex(
                RUNTIME.ReviewHostRuntimeError, "deployment binding changed"):
            RUNTIME.open_review_host_runtime(config)

    def test_missing_or_unsafe_state_never_exposes_application(self):
        task, _control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        with self.assertRaisesRegex(RUNTIME.ReviewHostRuntimeError, "ledger is missing"):
            RUNTIME.open_review_host_runtime(config)
        if os.name != "nt":
            self.launcher_file.chmod(0o644)
            with self.assertRaisesRegex(
                    RUNTIME.ReviewHostRuntimeError, "launcher configuration"):
                RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
            self.assertFalse(self.ledger.exists())

    def test_normal_start_never_adopts_empty_or_foreign_ledger(self):
        task, _control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        self.ledger.touch(mode=0o600)
        before = self.ledger.read_bytes()
        with self.assertRaises(RUNTIME.ReviewHostRuntimeError):
            RUNTIME.open_review_host_runtime(config)
        self.assertEqual(self.ledger.read_bytes(), before)

        self.ledger.unlink()
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("CREATE TABLE foreign_state(value TEXT)")
        if os.name != "nt":
            self.ledger.chmod(0o600)
        with self.assertRaisesRegex(
                RUNTIME.ReviewHostRuntimeError, "not an initialized deployment"):
            RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        with sqlite3.connect(self.ledger) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall(), [("foreign_state",)],
            )

    def test_factory_digest_identity_and_cross_phase_reviewers_are_pinned(self):
        task, _control = ENDPOINT.response_request("fi-FI")
        route = ENDPOINT.response_route(task)
        config = self.configuration([route])
        value = json.loads(config.read_text(encoding="utf-8"))
        value["launcher"]["factory_sha256"] = "0" * 64
        self.write_json("host.json", value)
        with self.assertRaisesRegex(RUNTIME.ReviewHostRuntimeError, "digest"):
            RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        self.assertFalse(self.ledger.exists())

        routes, _captures = ENDPOINT.website_routes(["mt-MT"])
        route_values = [self.route_dict(item) for item in routes]
        route_values[1]["reviewer_agent_id"] = route_values[0]["reviewer_agent_id"]
        value["launcher"]["factory_sha256"] = self.factory_sha256
        value["routes"] = route_values
        self.write_json("host.json", value)
        with self.assertRaisesRegex(RUNTIME.ReviewHostRuntimeError, "must be distinct"):
            RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        self.assertFalse(self.ledger.exists())

    def test_verified_factory_source_ignores_stale_bytecode(self):
        task, control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        original = self.module_path.stat()
        evil = self.module_path.read_text(encoding="utf-8").replace(
            "launcher_factory(settings)", "launcher_factory({'x': 1})",
        )
        self.module_path.write_text(evil, encoding="utf-8")
        os.utime(self.module_path, ns=(original.st_atime_ns, original.st_mtime_ns))
        py_compile.compile(str(self.module_path), doraise=True)
        self.module_path.write_text(
            "import importlib\n"
            f"def build(settings):\n    return importlib.import_module({__name__!r}).launcher_factory(settings)\n",
            encoding="utf-8",
        )
        os.utime(self.module_path, ns=(original.st_atime_ns, original.st_mtime_ns))
        if os.name != "nt":
            self.module_path.chmod(0o600)
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        try:
            self.client(runtime).run_isolated(task, control=control)
        finally:
            runtime.close()
        self.assertEqual(FACTORY_SETTINGS, [
            {"endpoint": "synthetic://isolated-reviewers"},
        ])
        self.assertEqual(len(ACTIVE_LAUNCHER.calls), 1)

    def test_factory_supports_dataclasses_without_unverified_import(self):
        task, _control = ENDPOINT.response_request("fi-FI")
        self.module_path.write_text(
            "from __future__ import annotations\n"
            "import importlib\n"
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class Marker:\n    value: str\n"
            f"def build(settings):\n    Marker('ok')\n    return importlib.import_module({__name__!r}).launcher_factory(settings)\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            self.module_path.chmod(0o600)
        self.factory_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        config = self.configuration([ENDPOINT.response_route(task)])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        runtime.close()
        self.assertEqual(FACTORY_SETTINGS, [
            {"endpoint": "synthetic://isolated-reviewers"},
        ])

    def test_literal_sqlite_uri_characters_address_exact_ledger(self):
        self.ledger = self.root / "review-host.sqlite3?tenant=one#state"
        task, control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        runtime.close()
        runtime = RUNTIME.open_review_host_runtime(config)
        try:
            result = self.client(runtime).run_isolated(task, control=control)
        finally:
            runtime.close()
        self.assertEqual(result["response"]["status"], "PASS")

    def test_startup_ledger_replacement_blocks_without_schema_repair(self):
        task, _control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        runtime.close()
        original = RUNTIME._bind_deployment

        def replace_after_binding(path, binding, *, initialize):
            original(path, binding, initialize=initialize)
            replacement = self.root / "startup-replacement.sqlite3"
            replacement.write_bytes(b"")
            if os.name != "nt":
                replacement.chmod(0o600)
            os.replace(replacement, path)

        with mock.patch.object(
                RUNTIME, "_bind_deployment", side_effect=replace_after_binding):
            with self.assertRaisesRegex(
                    RUNTIME.ReviewHostRuntimeError, "changed during startup"):
                RUNTIME.open_review_host_runtime(config)
        self.assertEqual(self.ledger.read_bytes(), b"")

    def test_closed_runtime_fails_without_launcher_access(self):
        task, control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        runtime.close()
        client = self.client(runtime)
        with self.assertRaises(HTTP.HTTPReviewHostFailed) as raised:
            client.run_isolated(task, control=control)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(ACTIVE_LAUNCHER.calls, [])

    def test_close_drains_admitted_request_before_closing_launcher(self):
        global ACTIVE_LAUNCHER

        class ClosingLauncher(ENDPOINT.FixtureLauncher):
            def __init__(self):
                super().__init__()
                self.events = []

            def execute_idempotent(self, *args, **kwargs):
                self.events.append("execute")
                return super().execute_idempotent(*args, **kwargs)

            def close(self):
                self.events.append("close")

        ACTIVE_LAUNCHER = ClosingLauncher()
        ACTIVE_LAUNCHER.launcher_id = "synthetic-launcher"
        ACTIVE_LAUNCHER.launcher_version = "fixture-1"
        entered, release = threading.Event(), threading.Event()
        ACTIVE_LAUNCHER.block = (entered, release)
        task, control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        result = []
        request = threading.Thread(
            target=lambda: result.append(
                self.client(runtime).run_isolated(task, control=control)
            )
        )
        request.start()
        self.assertTrue(entered.wait(1))
        closer = threading.Thread(target=runtime.close)
        closer.start()
        time.sleep(0.05)
        self.assertEqual(ACTIVE_LAUNCHER.events, ["execute"])
        self.assertTrue(closer.is_alive())
        release.set()
        request.join(2)
        closer.join(2)
        self.assertFalse(request.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(ACTIVE_LAUNCHER.events, ["execute", "close"])
        self.assertEqual(result[0]["response"]["status"], "PASS")

    @unittest.skipIf(os.name == "nt", "POSIX permission and inode semantics")
    def test_live_ledger_permission_and_inode_drift_block_before_launcher(self):
        task, control = ENDPOINT.response_request("fi-FI")
        config = self.configuration([ENDPOINT.response_route(task)])
        runtime = RUNTIME.open_review_host_runtime(config, initialize_ledger=True)
        client = self.client(runtime)
        self.ledger.chmod(0o644)
        with self.assertRaises(HTTP.HTTPReviewHostFailed):
            client.run_isolated(task, control=control)
        self.assertEqual(ACTIVE_LAUNCHER.calls, [])
        self.ledger.chmod(0o600)
        replacement = self.root / "replacement.sqlite3"
        replacement.write_bytes(b"not a journal")
        replacement.chmod(0o600)
        os.replace(replacement, self.ledger)
        with self.assertRaises(HTTP.HTTPReviewHostFailed):
            client.run_isolated(task, control=control)
        self.assertEqual(ACTIVE_LAUNCHER.calls, [])
        runtime.close()


if __name__ == "__main__":
    unittest.main()
