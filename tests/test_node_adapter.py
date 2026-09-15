from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "integrations" / "guard_service.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class NodeAdapterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_node_adapter_blocks_bypass_and_guards_telegram(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "node_language_guard_test.js")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_real_guard_service_binds_exact_telegram_payload(self) -> None:
        service_module = load("blun_node_adapter_guard_service", SERVICE_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_module.GuardService(
                root / "signing.key", root / "audit.jsonl"
            )
            server = service_module._ThreadingTCPServer(
                ("127.0.0.1", 0), service_module._RequestHandler
            )
            server.guard_service = service
            server.socket_path = None
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                target = (
                    "  Natürlich ist das möglich.\r\n"
                    + "Die Prüfung bleibt exakt. " * 180
                    + "😀\n  "
                )
                released = service.handle({
                    "operation": "release",
                    "task_kind": "response",
                    "target_text": target,
                    "language": "de-DE",
                    "agent_id": "synthetic-agent",
                    "channel": "telegram",
                    "attestations": {"nativeness": True, "orthography": True},
                })
                self.assertTrue(released["release_allowed"], released)
                request = {
                    "rawEnvelope": json.dumps({
                        "target_text": target,
                        "release_token": released["release_token"],
                    }, ensure_ascii=False),
                    "hostContext": {
                        "operation": "chat", "response_language": "de-DE"
                    },
                    "endpoint": f"tcp:127.0.0.1:{server.server_address[1]}",
                    "agentId": "synthetic-agent",
                    "sessionId": "synthetic-session",
                    "sessionEpoch": "a" * 64,
                    "channel": "telegram",
                    "botToken": "synthetic-host-token",
                    "chatId": "synthetic-chat",
                }
                script = r"""
const fs = require("node:fs");
const adapter = require(process.argv[1]);
const options = JSON.parse(fs.readFileSync(0, "utf8"));
const sent = [];
adapter.guardedTelegramSend({
  ...options,
  telegramRequest: async (_token, method, payload) => {
    sent.push({ method, text: payload.text });
    return { message_id: sent.length };
  },
}).then(result => {
  process.stdout.write(JSON.stringify({ sent, result }));
}).catch(error => {
  process.stderr.write(`${error.code || error.name}: ${error.message}\n`);
  process.exitCode = 1;
});
"""
                result = subprocess.run(
                    [
                        "node", "-e", script,
                        str(ROOT / "integrations" / "adapters" / "node-language-guard.js"),
                    ],
                    cwd=ROOT,
                    input=json.dumps(request, ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = json.loads(result.stdout)
                self.assertEqual(
                    "".join(item["text"] for item in observed["sent"]), target
                )
                self.assertTrue(all(
                    item["method"] == "sendMessage" for item in observed["sent"]
                ))
                self.assertEqual(
                    observed["result"]["candidateSha256"],
                    hashlib.sha256(target.encode("utf-8")).hexdigest(),
                )
                records = [
                    json.loads(line)
                    for line in (root / "audit.jsonl").read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]
                self.assertEqual(
                    [record["event"] for record in records[-2:]],
                    ["authorize-delivery", "consume-delivery"],
                )
                self.assertNotIn(target, json.dumps(records, ensure_ascii=False))
                self.assertNotIn(
                    released["release_token"], json.dumps(records, ensure_ascii=False)
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_blun_code_adapter_bootstraps_buffers_and_releases(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "blun_code_language_guard_test.js")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
