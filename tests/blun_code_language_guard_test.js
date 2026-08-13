"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const {
  bootstrapLanguageGuardMcp,
  createBlunLanguageGuard,
} = require("../integrations/adapters/blun-code-language-guard");

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "blun-code-guard-"));
const tokenFile = path.join(temporary, "service.token");
fs.writeFileSync(tokenFile, "service-token-with-at-least-32-characters\n");
const records = [];
const store = {
  servers: [],
  getAllInternal() { return this.servers.map(item => ({ ...item })); },
  getSecrets(id) { return this.servers.find(item => item.id === id)?.secrets || {}; },
  save(payload) {
    const secrets = Object.fromEntries(payload.secretText.split("\n").map(line => line.split(/=(.*)/s).slice(0, 2)));
    const item = { ...payload, id: "guard-id", secrets };
    this.servers.push(item);
    return { ok: true, savedId: item.id };
  },
};

const server = net.createServer(socket => {
  let raw = "";
  socket.setEncoding("utf8");
  socket.on("data", chunk => {
    raw += chunk;
    if (!raw.includes("\n")) return;
    const request = JSON.parse(raw.split("\n", 1)[0]);
    records.push(request);
    socket.end(`${JSON.stringify({ valid: request.release_token === "valid", version: "6.3.0" })}\n`);
  });
});

server.listen(0, "127.0.0.1", async () => {
  try {
    const endpoint = `tcp:127.0.0.1:${server.address().port}`;
    const legacyDir = path.join(temporary, ".blun");
    fs.mkdirSync(legacyDir);
    fs.writeFileSync(path.join(legacyDir, "mcp.json"), JSON.stringify({
      mcpServers: {
        "blun-language-guard": {
          command: "python3",
          args: ["guard.py", "serve"],
          env: {
            BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT: endpoint,
            BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE: tokenFile,
          },
        },
      },
    }));
    assert.equal(bootstrapLanguageGuardMcp({ userHome: temporary, store }).installed, true);
    assert.equal(bootstrapLanguageGuardMcp({ userHome: temporary, store }).reason, "already-installed");

    const guard = createBlunLanguageGuard({ store, getConfig: () => ({ language: "de-DE" }), environment: {} });
    assert.equal(guard.mandatory, true);
    const context = guard.context({ messages: [{ role: "user", content: "Antworte bitte." }], meta: {}, channel: "desktop" });
    const events = [];
    const buffered = guard.bufferedEmitter(event => events.push(event));
    buffered({ type: "text-delta", delta: "secret draft" });
    buffered({ type: "tool-start", name: "release_response" });
    assert.deepEqual(events, [{ type: "tool-start", name: "release_response" }]);

    const released = await guard.releaseResult({
      answer: JSON.stringify({ target_text: "Natürlich ist das möglich.", release_token: "valid" }),
    }, context, event => events.push(event));
    assert.equal(released.answer, "Natürlich ist das möglich.");
    assert.equal(released.languageGuardVerified, true);
    assert.equal(records.at(-1).task_kind, "response");
    assert.equal(records.at(-1).service_token, "service-token-with-at-least-32-characters");
    assert.ok(!JSON.stringify(events).includes("secret draft"));

    await assert.rejects(() => guard.releaseResult(
      { answer: JSON.stringify({ target_text: "Manipuliert", release_token: "wrong" }) },
      context,
      () => assert.fail("blocked text must not emit"),
    ));

    assert.throws(() => guard.context({
      messages: [{ role: "user", content: "Translate this." }],
      meta: { languageGuardTaskKind: "translation", languageGuardLanguage: "sv-SE" },
    }), /complete source_text/);
  } finally {
    server.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
