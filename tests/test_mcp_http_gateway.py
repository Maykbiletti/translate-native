from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "integrations" / "mcp_http_gateway.py"
HEADERS_PATH = ROOT / "integrations" / "mcp_auth_headers.py"
SERVICE_PATH = ROOT / "integrations" / "guard_service.py"
INSTALLER_PATH = ROOT / "installer" / "blun_language_guard.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GATEWAY = load("blun_test_mcp_http_gateway", GATEWAY_PATH)
HEADERS = load("blun_test_mcp_auth_headers", HEADERS_PATH)
SERVICE = load("blun_test_mcp_http_guard_service", SERVICE_PATH)
INSTALLER = load("blun_test_mcp_http_installer", INSTALLER_PATH)


class MCPHTTPGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "http-access-token-with-at-least-32-characters"
        self.server = GATEWAY.build_server("127.0.0.1", 0, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        payload: dict | bytes,
        *,
        token: str | None = None,
        origin: str | None = None,
        protocol: str | None = "2025-06-18",
        content_type: str = "application/json",
    ) -> tuple[int, bytes, dict[str, str]]:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": content_type,
            "Accept": "application/json, text/event-stream",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        if protocol is not None:
            headers["MCP-Protocol-Version"] = protocol
        request = urllib.request.Request(self.base_url + "/mcp", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers.items())

    def test_authenticated_stateless_initialize_and_tools(self) -> None:
        status, body, _headers = self.request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        }, token=self.token, protocol=None)
        self.assertEqual(status, 200)
        initialized = json.loads(body)
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("release_response", initialized["result"]["instructions"])

        status, body, _headers = self.request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, token=self.token)
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in json.loads(body)["result"]["tools"]}
        self.assertIn("release_response", names)
        self.assertIn("release_translation", names)

        status, body, _headers = self.request({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }, token=self.token)
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")

    def test_bom_and_bad_request_do_not_kill_service(self) -> None:
        valid = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
        status, body, _headers = self.request(b"\xef\xbb\xbf" + valid, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], {})

        status, _body, _headers = self.request(b"{broken", token=self.token)
        self.assertEqual(status, 400)
        status, body, _headers = self.request(
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}, token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], 2)
        self.assertTrue(self.thread.is_alive())

    def test_authentication_origin_and_protocol_fail_closed(self) -> None:
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        status, _body, headers = self.request(ping)
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        status, _body, _headers = self.request(ping, token=self.token, origin="https://attacker.example")
        self.assertEqual(status, 403)
        status, _body, _headers = self.request(ping, token=self.token, protocol="2099-01-01")
        self.assertEqual(status, 400)
        status, _body, _headers = self.request(ping, token=self.token, origin="http://localhost:3000")
        self.assertEqual(status, 200)

    def test_get_mcp_returns_method_not_allowed(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/mcp",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 405)

    def test_health_checks_complete_isolated_chain(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/healthz", headers={"Authorization": f"Bearer {self.token}"},
        )
        with mock.patch.object(GATEWAY.GUARD, "SERVICE_ENDPOINT", "unix:/guard.sock"), mock.patch.object(
            GATEWAY.GUARD, "_service_token", return_value="service-token"
        ), mock.patch.object(
            GATEWAY.GUARD.SERVICE_CLIENT,
            "call_guard_service",
            return_value={"status": "ok", "isolated_key": True, "version": "6.3.0"},
        ):
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["status"], "ok")

        with mock.patch.object(GATEWAY.GUARD, "SERVICE_ENDPOINT", "unix:/guard.sock"), mock.patch.object(
            GATEWAY.GUARD, "_service_token", return_value="service-token"
        ), mock.patch.object(
            GATEWAY.GUARD.SERVICE_CLIENT,
            "call_guard_service",
            side_effect=OSError("offline"),
        ):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 503)

    def test_real_http_to_isolated_service_release_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service_token = "isolated-service-token-with-at-least-32-characters"
            token_file = root / "service.token"
            token_file.write_text(service_token + "\n", encoding="ascii")
            if os.name != "nt":
                os.chmod(token_file, 0o600)
            service = SERVICE.GuardService(root / "signing.key", root / "audit.jsonl", service_token)
            server = SERVICE._ThreadingTCPServer(("127.0.0.1", 0), SERVICE._RequestHandler)
            server.guard_service = service
            server.socket_path = None
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = f"tcp:127.0.0.1:{server.server_address[1]}"
                with mock.patch.object(GATEWAY.GUARD, "SERVICE_ENDPOINT", endpoint), mock.patch.object(
                    GATEWAY.GUARD, "SERVICE_TOKEN_FILE", str(token_file)
                ):
                    status, body, _headers = self.request({
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "release_response",
                            "arguments": {
                                "target_text": "Natürlich ist das möglich.",
                                "language": "de-DE",
                                "attestations": {"nativeness": True, "orthography": True},
                            },
                        },
                    }, token=self.token)
                    self.assertEqual(status, 200)
                    structured = json.loads(body)["result"]["structuredContent"]
                    self.assertTrue(structured["release_allowed"], structured)
                    self.assertTrue(structured["release_token"].startswith("blg6."))

                    request = urllib.request.Request(
                        self.base_url + "/healthz",
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
                    with urllib.request.urlopen(request, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertTrue(json.loads(response.read())["isolated_key"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_real_deep_monitor_probe_reaches_signer_and_swedish_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service_token = "deep-probe-service-token-with-at-least-32-characters"
            service_token_file = root / "service.token"
            service_token_file.write_text(service_token + "\n", encoding="ascii")
            mcp_token_file = root / "mcp.token"
            mcp_token_file.write_text(self.token + "\n", encoding="ascii")
            if os.name != "nt":
                os.chmod(service_token_file, 0o600)
                os.chmod(mcp_token_file, 0o600)
            service = SERVICE.GuardService(root / "signing.key", root / "audit.jsonl", service_token)
            signer = SERVICE._ThreadingTCPServer(("127.0.0.1", 0), SERVICE._RequestHandler)
            signer.guard_service = service
            signer.socket_path = None
            signer_thread = threading.Thread(target=signer.serve_forever, daemon=True)
            signer_thread.start()
            try:
                endpoint = f"tcp:127.0.0.1:{signer.server_address[1]}"
                with mock.patch.object(GATEWAY.GUARD, "SERVICE_ENDPOINT", endpoint), \
                     mock.patch.object(GATEWAY.GUARD, "SERVICE_TOKEN_FILE", str(service_token_file)), \
                     mock.patch.object(INSTALLER, "MCP_HTTP_URL", self.base_url + "/mcp"), \
                     mock.patch.object(INSTALLER, "MCP_HTTP_TOKEN", mcp_token_file):
                    result = INSTALLER.probe_mcp_http(timeout=2.0)
                self.assertEqual(result["health"]["status"], "ok")
                self.assertEqual(result["health"]["self_test"], {
                    "release": True,
                    "signature": True,
                    "tamper_blocked": True,
                    "audit_paths": True,
                })
                self.assertEqual(result["canary"], {"status": "PASS", "language": "sv-SE"})
                self.assertFalse((root / "audit.jsonl").exists())
            finally:
                signer.shutdown()
                signer.server_close()
                signer_thread.join(timeout=2)

    def test_non_loopback_bind_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            GATEWAY.build_server("0.0.0.0", 0, self.token)


class MCPAuthHeadersTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX gateway-token directory safety test")
    def test_gateway_rejects_unsafe_token_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            redirected = root / "redirected"
            redirected.mkdir()
            redirected_token = redirected / "token"
            redirected_token.write_text("r" * 64 + "\n", encoding="ascii")
            redirected_token.chmod(0o600)
            linked = root / "linked"
            linked.symlink_to(redirected, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "token directory"):
                GATEWAY.load_access_token(linked / "token")

            writable = root / "writable"
            writable.mkdir()
            writable_token = writable / "token"
            writable_token.write_text("w" * 64 + "\n", encoding="ascii")
            writable_token.chmod(0o600)
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside"):
                    GATEWAY.load_access_token(writable_token)
            finally:
                writable.chmod(0o700)

    def test_gateway_does_not_create_missing_token_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "nested" / "token"

            with self.assertRaises(FileNotFoundError):
                GATEWAY.load_access_token(path)

            self.assertFalse(path.parent.exists())

    @unittest.skipIf(os.name == "nt", "POSIX gateway-token directory identity test")
    def test_gateway_detects_token_parent_exchange_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "trusted"
            trusted.mkdir()
            token_path = trusted / "token"
            token_path.write_text("t" * 64 + "\n", encoding="ascii")
            token_path.chmod(0o600)
            replacement = root / "replacement"
            replacement.mkdir()
            replacement_token = replacement / "token"
            replacement_token.write_text("x" * 64 + "\n", encoding="ascii")
            replacement_token.chmod(0o600)
            displaced = root / "displaced"
            real_validate = GATEWAY._validate_access_token_file
            calls = 0

            def exchange_after_open(path, details) -> None:
                nonlocal calls
                real_validate(path, details)
                calls += 1
                if calls == 2:
                    trusted.rename(displaced)
                    replacement.rename(trusted)

            with mock.patch.object(
                GATEWAY, "_validate_access_token_file", side_effect=exchange_after_open
            ):
                with self.assertRaisesRegex(RuntimeError, "token directory changed"):
                    GATEWAY.load_access_token(token_path)

            self.assertEqual((trusted / "token").read_text(encoding="ascii").strip(), "x" * 64)
            self.assertEqual((displaced / "token").read_text(encoding="ascii").strip(), "t" * 64)

    def test_helper_reads_owner_only_token_and_emits_one_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("header-token-with-at-least-32-characters\n", encoding="ascii")
            if os.name != "nt":
                os.chmod(path, 0o600)
            self.assertEqual(
                HEADERS.load_token(path),
                "header-token-with-at-least-32-characters",
            )

    def test_helper_reloads_rotated_token_for_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            if os.name != "nt":
                path.touch(mode=0o600)
            previous = os.environ.get("BLUN_LANGUAGE_GUARD_MCP_TOKEN_FILE")
            os.environ["BLUN_LANGUAGE_GUARD_MCP_TOKEN_FILE"] = str(path)
            try:
                outputs = []
                for token in (
                    "first-header-token-with-at-least-32-characters",
                    "second-header-token-with-at-least-32-characters",
                ):
                    path.write_text(token + "\n", encoding="ascii")
                    if os.name != "nt":
                        os.chmod(path, 0o600)
                    stream = io.StringIO()
                    with contextlib.redirect_stdout(stream):
                        self.assertEqual(HEADERS.main(), 0)
                    outputs.append(json.loads(stream.getvalue())["Authorization"])
                self.assertNotEqual(outputs[0], outputs[1])
                self.assertTrue(outputs[1].endswith("second-header-token-with-at-least-32-characters"))
            finally:
                if previous is None:
                    os.environ.pop("BLUN_LANGUAGE_GUARD_MCP_TOKEN_FILE", None)
                else:
                    os.environ["BLUN_LANGUAGE_GUARD_MCP_TOKEN_FILE"] = previous

    @unittest.skipIf(os.name == "nt", "POSIX permission check")
    def test_helper_rejects_group_readable_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("header-token-with-at-least-32-characters\n", encoding="ascii")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(RuntimeError, "owner-only"):
                HEADERS.load_token(path)

    @unittest.skipIf(os.name == "nt", "POSIX header-token directory safety test")
    def test_helper_rejects_unsafe_token_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            redirected = root / "redirected"
            redirected.mkdir()
            redirected_token = redirected / "token"
            redirected_token.write_text("r" * 64 + "\n", encoding="ascii")
            redirected_token.chmod(0o600)
            linked = root / "linked"
            linked.symlink_to(redirected, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "token directory"):
                HEADERS.load_token(linked / "token")

            writable = root / "writable"
            writable.mkdir()
            writable_token = writable / "token"
            writable_token.write_text("w" * 64 + "\n", encoding="ascii")
            writable_token.chmod(0o600)
            writable.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "writable outside"):
                    HEADERS.load_token(writable_token)
            finally:
                writable.chmod(0o700)

    def test_helper_does_not_create_missing_token_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "nested" / "token"

            with self.assertRaises(FileNotFoundError):
                HEADERS.load_token(path)

            self.assertFalse(path.parent.exists())

    @unittest.skipIf(os.name == "nt", "POSIX header-token directory identity test")
    def test_helper_detects_token_parent_exchange_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "trusted"
            trusted.mkdir()
            token_path = trusted / "token"
            token_path.write_text("t" * 64 + "\n", encoding="ascii")
            token_path.chmod(0o600)
            replacement = root / "replacement"
            replacement.mkdir()
            replacement_token = replacement / "token"
            replacement_token.write_text("x" * 64 + "\n", encoding="ascii")
            replacement_token.chmod(0o600)
            displaced = root / "displaced"
            real_validate = HEADERS._validate_token_file
            calls = 0

            def exchange_after_open(path, details) -> None:
                nonlocal calls
                real_validate(path, details)
                calls += 1
                if calls == 2:
                    trusted.rename(displaced)
                    replacement.rename(trusted)

            with mock.patch.object(HEADERS, "_validate_token_file", side_effect=exchange_after_open):
                with self.assertRaisesRegex(RuntimeError, "token directory changed"):
                    HEADERS.load_token(token_path)

            self.assertEqual((trusted / "token").read_text(encoding="ascii").strip(), "x" * 64)
            self.assertEqual((displaced / "token").read_text(encoding="ascii").strip(), "t" * 64)

    def test_gateway_and_header_helper_reject_links_and_oversized_tokens(self) -> None:
        consumers = (GATEWAY.load_access_token, HEADERS.load_token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.token"
            target.write_text("known-token-" + "x" * 32 + "\n", encoding="ascii")
            if os.name != "nt":
                target.chmod(0o600)
            linked = root / "linked.token"
            try:
                linked.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links are unavailable: {error}")
            for consumer in consumers:
                with self.subTest(consumer=consumer.__module__, case="link"):
                    with self.assertRaisesRegex(RuntimeError, "regular file"):
                        consumer(linked)

            oversized = root / "oversized.token"
            oversized.write_bytes(b"x" * (HEADERS.MAX_TOKEN_BYTES + 1))
            if os.name != "nt":
                oversized.chmod(0o600)
            for consumer in consumers:
                with self.subTest(consumer=consumer.__module__, case="oversized"):
                    with self.assertRaisesRegex(RuntimeError, "invalid size"):
                        consumer(oversized)

    def test_gateway_and_header_helper_reject_token_identity_change(self) -> None:
        consumers = (
            (GATEWAY, GATEWAY.load_access_token),
            (HEADERS, HEADERS.load_token),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (module, consumer) in enumerate(consumers):
                token = root / f"token-{index}"
                replacement = root / f"replacement-{index}"
                token.write_text("a" * 64 + "\n", encoding="ascii")
                replacement.write_text("b" * 64 + "\n", encoding="ascii")
                if os.name != "nt":
                    token.chmod(0o600)
                    replacement.chmod(0o600)
                opened = token.stat()
                changed = replacement.stat()
                if hasattr(module, "_token_fstat"):
                    owner = module
                    attribute = "_token_fstat"
                elif hasattr(module, "_access_token_fstat"):
                    owner = module
                    attribute = "_access_token_fstat"
                else:
                    owner = module.os
                    attribute = "fstat"
                with self.subTest(consumer=consumer.__module__), mock.patch.object(
                    owner, attribute, side_effect=(opened, changed)
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                        consumer(token)


if __name__ == "__main__":
    unittest.main()
