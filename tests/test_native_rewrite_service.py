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
    SESSION_ID = "rewrite-session"
    SESSION_EPOCH = "a" * 64
    AGENT_ID = "writer"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def setup_pipeline(self, target="Teksti on selkeä.", locale="fi-FI", **options):
        host = options.pop("host", FIX.Host())
        creator = options.pop("creator", FIX.Creator(target))
        worker = FIX.RW.NativeRewriteWorker(
            creator, host, ledger_path=self.root / "rewrite.sqlite",
            creator_id="writer", creator_session_id="writer-session",
            model_id="fixture-model", model_version=options.pop("model_version", "fixture-v1"),
            host_policy_version="fixture-v1", profile=FIX.profile(locale), **options)
        service = SERVICE.GuardService(self.root / "key", self.root / "audit.jsonl",
                                       rewrite_workers={"standard": worker})
        client = ADAPTER.NativeRewriteClient(service.handle)
        client.register_session(session_id=self.SESSION_ID, session_epoch=self.SESSION_EPOCH)
        return service, client, host, creator

    def test_29705_character_document_crosses_adapter_reviews_and_guard(self):
        # Synthetic protocol fixture only; this is not the user's unavailable
        # original and provides no Finnish native-quality evidence.
        seed = "Tämä on synteettinen pitkä kappale, jossa säilyvät ääkköset ja numerot 42.\n\n"
        source = (seed * ((29_705 // len(seed)) + 2))[:29_705]
        creator = FIX.Creator(lambda request: request.input["owned_source"]["text"])
        service, client, host, creator = self.setup_pipeline(creator=creator)
        result = self.rewrite(client, source_text=source, request_id="synthetic-long-29705")
        self.assertTrue(result["release_allowed"], result)
        self.assertEqual(result["target_text"], source)
        self.assertGreater(len(creator.calls), 1)
        self.assertLessEqual(len(creator.calls), FIX.RW.LONG_MAX_CHUNKS)
        self.assertEqual([task["phase"] for task, _control in host.calls],
                         ["target_native", "source_fidelity"])
        native, fidelity = (task for task, _control in host.calls)
        self.assertNotIn("source", native["input"])
        self.assertNotIn("review_scope", native["input"])
        self.assertEqual(fidelity["input"]["source"]["text"], source)
        self.assertTrue(self.verify(service, result, source_text=source,
                                    request_id="synthetic-long-29705")["valid"])

    def test_long_json_finnish_and_maltese_cross_adapter_reviews_and_guard(self):
        cases = (
            ("fi-FI", "Selkeä arvo säilyttää ääkköset ja numeron 42. "),
            ("mt-MT", "Valur ċar iżomm ċ, ġ, għ, ħ, ż u n-numru 42. "),
        )
        for index, (locale, seed) in enumerate(cases):
            source = ("{\n  \"copy\": " + json.dumps(seed * 100, ensure_ascii=False)
                      + ",\n  \"count\": 42,\n  \"enabled\": true,\n"
                      + "  \"placeholder\": \"{{name}}\"\n}\n")
            creator = FIX.Creator("fixture-keeps-json-values")
            service, client, host, creator = self.setup_pipeline(
                creator=creator, locale=locale)
            request_id = "long-json-" + str(index)
            result = self.rewrite(client, source_text=source, language=locale,
                                  request_id=request_id)
            self.assertTrue(result["release_allowed"], result)
            self.assertEqual(result["target_text"], source)
            self.assertGreaterEqual(len(creator.calls), 1)
            self.assertEqual([task["phase"] for task, _control in host.calls],
                             ["target_native", "source_fidelity"])
            self.assertNotIn("source", host.calls[0][0]["input"])
            self.assertEqual(host.calls[1][0]["input"]["source"]["text"], source)
            self.assertTrue(self.verify(
                service, result, source_text=source, language=locale,
                request_id=request_id)["valid"])

    def test_long_html_finnish_maltese_and_arabic_cross_adapter_and_guard(self):
        cases = (
            ("fi-FI", "Selkeä teksti säilyttää ääkköset ja numeron 42. "),
            ("mt-MT", "Test ċar iżomm ċ, ġ, għ, ħ, ż u n-numru 42. "),
            ("ar", "نص واضح يحافظ على الرقم 42 وعلامات الترقيم. "),
        )
        for index, (locale, seed) in enumerate(cases):
            source = ('<!doctype html><html><head><meta name="description" '
                      'content="Kuvaus 42."><script>const fixed = 42;</script>'
                      '</head><body><main><p>' + (seed * 300) +
                      '</p><a href="https://example.test/x">Avaa</a>'
                      '<pre><code>const fixed = 42;</code></pre>'
                      '</main></body></html>')
            creator = FIX.Creator("fixture-keeps-html-spans")
            service, client, host, creator = self.setup_pipeline(
                creator=creator, locale=locale, max_output_tokens=8192)
            request_id = "long-html-" + str(index)
            result = self.rewrite(client, source_text=source, language=locale,
                                  request_id=request_id,
                                  content_type="documentation")
            self.assertTrue(result["release_allowed"], result)
            self.assertEqual(result["target_text"], source)
            self.assertGreaterEqual(len(creator.calls), 1)
            self.assertEqual([task["phase"] for task, _control in host.calls],
                             ["target_native", "source_fidelity"])
            self.assertNotIn("source", host.calls[0][0]["input"])
            self.assertEqual(host.calls[1][0]["input"]["source"]["text"], source)
            self.assertTrue(self.verify(
                service, result, source_text=source, language=locale,
                request_id=request_id, content_type="documentation")["valid"])

    def test_guard_recomputes_long_json_manifest_and_rejects_tampering(self):
        source = json.dumps({"copy": "Täsmällinen arvo 42 säilyy. " * 180,
                             "count": 42, "enabled": True}, ensure_ascii=False,
                            indent=2)
        creator = FIX.Creator("fixture-keeps-json-values")
        service, _client, _host, _creator = self.setup_pipeline(creator=creator)
        worker = service.rewrite_workers["standard"]
        reviewed = json.loads(json.dumps(worker.run(source, "prose", "tampered-json")))
        reviewed["evidence"]["document"]["groups"][0][
            "creation_response_sha256"] = "0" * 64
        reviewed["evidence_sha256"] = FIX.RW._hash(reviewed["evidence"])
        worker.run = mock.Mock(return_value=reviewed)
        request = self.prepared_request(
            service, source_text=source, request_id="tampered-json")
        result = service.handle(request)
        self.assertEqual(result, {"status": "BLOCK", "release_allowed": False,
                                  "reason": "rewrite.worker_failed"})

    def test_guard_recomputes_long_html_manifest_and_rejects_tampering(self):
        source = "<main><p>" + ("Täsmällinen arvo 42 säilyy. " * 240) + "</p></main>"
        creator = FIX.Creator("fixture-keeps-html-spans")
        service, _client, _host, _creator = self.setup_pipeline(
            creator=creator, max_output_tokens=8192)
        worker = service.rewrite_workers["standard"]
        reviewed = json.loads(json.dumps(
            worker.run(source, "prose", "tampered-html")))
        reviewed["evidence"]["document"]["groups"][0][
            "creation_response_sha256"] = "0" * 64
        reviewed["evidence_sha256"] = FIX.RW._hash(reviewed["evidence"])
        worker.run = mock.Mock(return_value=reviewed)
        request = self.prepared_request(
            service, source_text=source, request_id="tampered-html")
        result = service.handle(request)
        self.assertEqual(result, {"status": "BLOCK", "release_allowed": False,
                                  "reason": "rewrite.worker_failed"})

    def test_guard_recomputes_long_manifest_and_rejects_tampered_worker_evidence(self):
        source = ("Täsmällinen pitkä alku 42 säilyy.\n\n" * 180).strip()
        creator = FIX.Creator(lambda request: request.input["owned_source"]["text"])
        service, _client, _host, _creator = self.setup_pipeline(creator=creator)
        worker = service.rewrite_workers["standard"]
        reviewed = worker.run(source, "prose", "tampered-long")
        reviewed = json.loads(json.dumps(reviewed))
        reviewed["evidence"]["document"]["chunks"][0][
            "creation_response_sha256"] = "0" * 64
        reviewed["evidence_sha256"] = FIX.RW._hash(reviewed["evidence"])
        worker.run = mock.Mock(return_value=reviewed)
        request = self.prepared_request(
            service, source_text=source, request_id="tampered-long")
        result = service.handle(request)
        self.assertEqual(result, {"status": "BLOCK", "release_allowed": False,
                                  "reason": "rewrite.worker_failed"})
        worker.run.assert_called_once_with(source, "prose", "tampered-long")

    def request(self, **extra):
        return {"source_text": "On tärkeää huomata, että teksti on selkeä.",
                "language": "fi-FI", "profile_id": "standard", "request_id": "one",
                "content_type": "prose", **extra}

    def rewrite(self, client, **extra):
        return client.rewrite(**self.request(**extra), session_id=self.SESSION_ID,
                              session_epoch=self.SESSION_EPOCH, agent_id=self.AGENT_ID)

    def prepared_request(self, service, **extra):
        request = self.request(**extra)
        prepared = service.handle({"operation": "prepare_rewrite_context",
                                   "task_kind": "rewrite", **request,
                                   "session_id": self.SESSION_ID,
                                   "session_epoch": self.SESSION_EPOCH,
                                   "agent_id": self.AGENT_ID})
        return {"operation": "rewrite_text", **request,
                "rewrite_context_token": prepared["rewrite_context_token"],
                "session_id": self.SESSION_ID, "session_epoch": self.SESSION_EPOCH,
                "agent_id": self.AGENT_ID}

    def verify(self, service, result, **changes):
        data = self.request()
        return service.handle({**data, "operation": "verify", "task_kind": "rewrite",
                               "session_id": self.SESSION_ID,
                               "session_epoch": self.SESSION_EPOCH,
                               "agent_id": self.AGENT_ID,
                               "target_text": result["target_text"],
                               "release_token": result["release_token"], **changes})

    def test_full_api_receipt_delivery_and_content_free_audit(self):
        service, client, host, creator = self.setup_pipeline()
        result = self.rewrite(client)
        self.assertTrue(result["release_allowed"], result)
        self.assertTrue(self.verify(service, result)["valid"])
        sent = []
        client.deliver(result, source_text=self.request()["source_text"], language="fi-FI",
                       profile_id="standard", request_id="one", content_type="prose",
                       session_id=self.SESSION_ID, session_epoch=self.SESSION_EPOCH,
                       agent_id=self.AGENT_ID, channel="test", send=sent.append)
        self.assertEqual(sent, [result["target_text"]])
        audit = (self.root / "audit.jsonl").read_text()
        for secret in (self.request()["source_text"], result["target_text"], result["release_token"]):
            self.assertNotIn(secret, audit)
        self.assertEqual(len(creator.calls), 1)
        self.assertEqual(len(host.ledger), 2)

    def test_mutation_locale_source_purpose_and_profile_invalidate(self):
        service, client, _, _ = self.setup_pipeline()
        result = self.rewrite(client)
        for changes in ({"target_text": result["target_text"] + "\r\n"},
                        {"source_text": self.request()["source_text"] + "\r\n"},
                        {"language": "mt-MT"}, {"profile_id": "missing"},
                        {"task_kind": "translation"}, {"content_type": "marketing"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.verify(service, result, **changes)["valid"])
        newer, _, _, _ = self.setup_pipeline(model_version="fixture-v2")
        self.assertFalse(self.verify(newer, result)["valid"])

    def test_rewrite_context_blocks_missing_forged_mutated_replayed_and_stale_calls(self):
        service, _, host, creator = self.setup_pipeline()
        with self.assertRaises(SERVICE.GuardProtocolError):
            service.handle({"operation": "rewrite_text", **self.request()})
        bound = self.prepared_request(service)
        for changes in ({"source_text": bound["source_text"] + " "},
                        {"language": "fi"}, {"profile_id": "missing"},
                        {"content_type": "marketing"}, {"request_id": "other"},
                        {"agent_id": "other-writer"}, {"session_id": "other-session"},
                        {"rewrite_context_token": "forged"}):
            with self.subTest(changes=changes), self.assertRaises(SERVICE.GuardProtocolError):
                service.handle({**bound, **changes})
        self.assertEqual(creator.calls, [])
        self.assertEqual(host.calls, [])
        accepted = service.handle(bound)
        self.assertTrue(accepted["release_allowed"])
        with self.assertRaises(SERVICE.GuardProtocolError):
            service.handle(bound)

        fresh = self.prepared_request(service, request_id="stale")
        service.handle({"operation": "retire_session_epoch", "session_id": self.SESSION_ID,
                        "session_epoch": self.SESSION_EPOCH})
        with self.assertRaises(SERVICE.GuardProtocolError):
            service.handle(fresh)

    def test_mcp_calls_actual_service_and_missing_host_blocks(self):
        service, _, host, _ = self.setup_pipeline()
        guard = SERVICE.GATEWAY.GUARD
        args = self.request()
        with mock.patch.object(guard, "SERVICE_ENDPOINT", "test"), \
                mock.patch.object(guard, "_service_token", return_value=""), \
                mock.patch.object(guard.SERVICE_CLIENT, "call_guard_service",
                                  side_effect=lambda endpoint, request, **kw: service.handle(request)):
            prepared = service.handle({"operation": "prepare_rewrite_context", "task_kind": "rewrite",
                                       **args, "session_id": self.SESSION_ID,
                                       "session_epoch": self.SESSION_EPOCH, "agent_id": self.AGENT_ID})
            tool_args = {**args, "rewrite_context_token": prepared["rewrite_context_token"],
                         "session_id": self.SESSION_ID, "session_epoch": self.SESSION_EPOCH,
                         "agent_id": self.AGENT_ID}
            response = guard.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "rewrite_text", "arguments": tool_args}})
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
            self.rewrite(client)
        with self.assertRaises(SERVICE.GuardProtocolError):
            service.handle({"operation": "rewrite_text", **self.request()})

    def test_one_time_grants_raw_bytes_and_delivery_outage(self):
        service, client, _, _ = self.setup_pipeline()
        result = self.rewrite(client)
        base = {"task_kind": "rewrite", "language": "fi-FI", "profile_id": "standard",
                "request_id": "one",
                "content_type": "prose", "target_text": result["target_text"],
                "session_id": self.SESSION_ID, "session_epoch": self.SESSION_EPOCH,
                "agent_id": "writer", "channel": "test"}
        grant = service.handle({**base, "operation": "authorize_delivery",
                                "source_text": self.request()["source_text"],
                                "release_token": result["release_token"]})
        replayed_authorization = service.handle({**base, "operation": "authorize_delivery",
                                                 "source_text": self.request()["source_text"],
                                                 "release_token": result["release_token"]})
        self.assertTrue(replayed_authorization["valid"])
        self.assertEqual(replayed_authorization["delivery_grant"], grant["delivery_grant"])
        for changes in ({"agent_id": "other-writer"}, {"request_id": "other-request"},
                        {"session_id": "other-session"}):
            rejected = service.handle({**base, **changes, "operation": "authorize_delivery",
                                       "source_text": self.request()["source_text"],
                                       "release_token": result["release_token"]})
            self.assertFalse(rejected["valid"])
        consume = {**base, "operation": "consume_delivery", "delivery_grant": grant["delivery_grant"],
                   "source_sha256": hashlib.sha256(self.request()["source_text"].encode()).hexdigest()}
        self.assertTrue(service.handle(consume)["valid"])
        self.assertFalse(service.handle(consume)["valid"])
        sent = []
        broken = ADAPTER.NativeRewriteClient(lambda _: {"valid": False})
        with self.assertRaises(ADAPTER.RewriteDeliveryBlocked):
            broken.deliver(result, source_text=self.request()["source_text"], language="fi-FI",
                           profile_id="standard", request_id="one", content_type="prose", session_id="session",
                           session_epoch="b" * 64, agent_id="writer", channel="test", send=sent.append)
        self.assertEqual(sent, [])

    def test_maltese_and_non_latin_reach_actual_guard(self):
        service, client, _, _ = self.setup_pipeline("It-test huwa ċar u jinftiehem.", "mt-MT")
        self.assertTrue(self.rewrite(client, language="mt-MT", source_text=
            "Huwa importanti li ngħidu li t-test huwa ċar u jinftiehem.")["release_allowed"])
        service, client, _, _ = self.setup_pipeline("這段文字很清楚。", "zh-Hant-TW")
        self.assertTrue(self.rewrite(client, language="zh-Hant-TW", request_id="chinese",
            source_text="需要指出的是，這段文字很清楚。")["release_allowed"])

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
            result = client.rewrite(**request, session_id=self.SESSION_ID,
                                    session_epoch=self.SESSION_EPOCH, agent_id=self.AGENT_ID)
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
