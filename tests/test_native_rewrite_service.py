"""Synthetic end-to-end fixtures, not proof of native quality or AI authorship."""
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FIX = load("rewrite_service_fixtures", "tests/test_native_rewrite_worker.py")
SERVICE = load("rewrite_test_service", "integrations/guard_service.py")
ADAPTER = load("rewrite_test_client", "integrations/adapters/native_rewrite.py")


class RewriteServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def setup_pipeline(self, target="Teksti on selkeä.", locale="fi-FI", **options):
        host = options.pop("host", FIX.Host())
        creator = FIX.Creator(target)
        worker = FIX.RW.NativeRewriteWorker(
            creator, host, ledger_path=self.root / "rewrite.sqlite",
            creator_id="writer", creator_session_id="writer-session",
            model_id="fixture-model", model_version=options.pop("model_version", "fixture-v1"),
            host_policy_version="fixture-v1", profile=FIX.profile(locale), **options)
        service = SERVICE.GuardService(self.root / "key", self.root / "audit.jsonl",
                                       rewrite_workers={"standard": worker})
        return service, ADAPTER.NativeRewriteClient(service.handle), host, creator

    def request(self, **extra):
        return {"source_text": "On tärkeää huomata, että teksti on selkeä.",
                "language": "fi-FI", "profile_id": "standard", "request_id": "one",
                "content_type": "prose", **extra}

    def verify(self, service, result, **changes):
        data = self.request()
        data.pop("request_id")
        return service.handle({**data, "operation": "verify", "task_kind": "rewrite",
                               "target_text": result["target_text"],
                               "release_token": result["release_token"], **changes})

    def test_full_api_receipt_delivery_and_content_free_audit(self):
        service, client, host, creator = self.setup_pipeline()
        result = client.rewrite(**self.request())
        self.assertTrue(result["release_allowed"], result)
        self.assertTrue(self.verify(service, result)["valid"])
        sent = []
        client.deliver(result, source_text=self.request()["source_text"], language="fi-FI",
                       profile_id="standard", content_type="prose", session_id="session",
                       session_epoch="a" * 64, agent_id="writer", channel="test", send=sent.append)
        self.assertEqual(sent, [result["target_text"]])
        audit = (self.root / "audit.jsonl").read_text()
        for secret in (self.request()["source_text"], result["target_text"], result["release_token"]):
            self.assertNotIn(secret, audit)
        self.assertEqual(len(creator.calls), 1)
        self.assertEqual(len(host.ledger), 2)

    def test_mutation_locale_source_purpose_and_profile_invalidate(self):
        service, client, _, _ = self.setup_pipeline()
        result = client.rewrite(**self.request())
        for changes in ({"target_text": result["target_text"] + "\r\n"},
                        {"source_text": self.request()["source_text"] + "\r\n"},
                        {"language": "mt-MT"}, {"profile_id": "missing"},
                        {"task_kind": "translation"}, {"content_type": "marketing"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.verify(service, result, **changes)["valid"])
        newer, _, _, _ = self.setup_pipeline(model_version="fixture-v2")
        self.assertFalse(self.verify(newer, result)["valid"])

    def test_mcp_calls_actual_service_and_missing_host_blocks(self):
        service, _, host, _ = self.setup_pipeline()
        guard = SERVICE.GATEWAY.GUARD
        args = self.request()
        with mock.patch.object(guard, "SERVICE_ENDPOINT", "test"), \
                mock.patch.object(guard, "_service_token", return_value=""), \
                mock.patch.object(guard.SERVICE_CLIENT, "call_guard_service",
                                  side_effect=lambda endpoint, request, **kw: service.handle(request)):
            response = guard.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "rewrite_text", "arguments": args}})
            result = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(result["release_allowed"], result)
            self.assertEqual(len(host.calls), 2)
        with mock.patch.object(guard, "SERVICE_ENDPOINT", ""):
            self.assertEqual(guard.rewrite_text(args)["reason"], "rewrite.host_unavailable")
        absent = SERVICE.GuardService(self.root / "key", self.root / "audit2.jsonl")
        with self.assertRaises(SERVICE.GuardProtocolError):
            absent.handle({"operation": "rewrite_text", **args})

    def test_untrusted_attestations_candidate_and_failures_never_release(self):
        service, client, _, _ = self.setup_pipeline(host=FIX.Host(confidence="low"))
        for extra in ({"target_text": "Injected"}, {"attestations": {"nativeness": True}},
                      {"profile_sha256": "0" * 64}):
            with self.assertRaises(SERVICE.GuardProtocolError):
                service.handle({"operation": "rewrite_text", **self.request(), **extra})
        with self.assertRaises(ADAPTER.RewriteDeliveryBlocked):
            client.rewrite(**self.request())
        failed = service.handle({"operation": "rewrite_text", **self.request()})
        self.assertFalse(failed["release_allowed"])
        self.assertNotIn("target_text", failed)

    def test_one_time_grants_raw_bytes_and_delivery_outage(self):
        service, client, _, _ = self.setup_pipeline()
        result = client.rewrite(**self.request())
        base = {"task_kind": "rewrite", "language": "fi-FI", "profile_id": "standard",
                "content_type": "prose", "target_text": result["target_text"],
                "session_id": "session", "session_epoch": "b" * 64,
                "agent_id": "writer", "channel": "test"}
        grant = service.handle({**base, "operation": "authorize_delivery",
                                "source_text": self.request()["source_text"],
                                "release_token": result["release_token"]})
        consume = {**base, "operation": "consume_delivery", "delivery_grant": grant["delivery_grant"],
                   "source_sha256": hashlib.sha256(self.request()["source_text"].encode()).hexdigest()}
        self.assertTrue(service.handle(consume)["valid"])
        self.assertFalse(service.handle(consume)["valid"])
        sent = []
        broken = ADAPTER.NativeRewriteClient(lambda _: {"valid": False})
        with self.assertRaises(ADAPTER.RewriteDeliveryBlocked):
            broken.deliver(result, source_text=self.request()["source_text"], language="fi-FI",
                           profile_id="standard", content_type="prose", session_id="session",
                           session_epoch="b" * 64, agent_id="writer", channel="test", send=sent.append)
        self.assertEqual(sent, [])

    def test_maltese_and_non_latin_reach_actual_guard(self):
        service, client, _, _ = self.setup_pipeline("It-test huwa ċar u jinftiehem.", "mt-MT")
        self.assertTrue(client.rewrite(**self.request(language="mt-MT", source_text=
            "Huwa importanti li ngħidu li t-test huwa ċar u jinftiehem."))["release_allowed"])
        service, client, _, _ = self.setup_pipeline("這段文字很清楚。", "zh-Hant-TW")
        self.assertTrue(client.rewrite(**self.request(language="zh-Hant-TW", request_id="chinese",
            source_text="需要指出的是，這段文字很清楚。"))["release_allowed"])

    def test_short_ui_and_marketing_over_socket_and_mcp_verifier(self):
        service, _, _, _ = self.setup_pipeline("Avaa asetukset.")
        server = SERVICE._ThreadingTCPServer(("127.0.0.1", 0), SERVICE._RequestHandler)
        server.guard_service = service
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = "tcp:127.0.0.1:" + str(server.server_address[1])
        client = ADAPTER.NativeRewriteClient(lambda req: SERVICE.CLIENT.call_guard_service(endpoint, req))
        guard = SERVICE.GATEWAY.GUARD
        verifier = next(t for t in guard.TOOLS if t["name"] == "verify_release_token")
        for content_type in ("ui", "marketing"):
            self.assertIn(content_type, verifier["inputSchema"]["properties"]["content_type"]["enum"])
            request = self.request(content_type=content_type, request_id=content_type,
                                   source_text="Voit avata asetukset tästä.")
            result = client.rewrite(**request)
            with mock.patch.object(guard, "SERVICE_ENDPOINT", endpoint), \
                    mock.patch.object(guard, "_service_token", return_value=""):
                verified = guard.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "verify_release_token", "arguments": {
                        "purpose": "rewrite", "source_text": request["source_text"],
                        "language": "fi-FI", "content_type": content_type, "profile_id": "standard",
                        "target_text": result["target_text"], "release_token": result["release_token"]}}})
                self.assertTrue(json.loads(verified["result"]["content"][0]["text"])["valid"])

    def test_installed_gateway_parity_and_han_variant_measurement(self):
        installed = load("rewrite_installed_gateway", "translate-native/scripts/language_gateway.py")
        for gateway in (installed, SERVICE.GATEWAY):
            with mock.patch.object(gateway.GUARD, "rewrite_text", return_value={"status": "BLOCK"}) as rewrite:
                self.assertEqual(gateway.gate({"task_kind": "rewrite", **self.request()}), {"status": "BLOCK"})
                rewrite.assert_called_once_with(self.request())
        for locale in ("zh-Hant-TW", "zh-Hans-CN"):
            self.assertEqual(SERVICE.QUALITY.script_report("中文文字", locale)["status"], "pass")
            self.assertEqual(SERVICE.QUALITY.script_report("English words", locale)["status"], "fail")
        self.assertEqual(SERVICE.QUALITY.script_report("ꆈꌠ", "ii-Yiii")["status"], "not-evaluated")


if __name__ == "__main__":
    unittest.main()
