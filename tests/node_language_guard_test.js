"use strict";

const assert = require("node:assert/strict");
const net = require("node:net");
const {
  LanguageGuardBlocked,
  routeHostContext,
  parseAgentEnvelope,
  callGuardService,
  verifyForDelivery,
  guardedTelegramSend,
} = require("../integrations/adapters/node-language-guard");

assert.equal(routeHostContext({ response_language: "de-AT" }).taskKind, "response");
assert.equal(routeHostContext({ source_text: "Hello", target_language: "sv-SE" }).taskKind, "translation");
assert.throws(() => routeHostContext({ task_kind: "response", source_text: "Hello", response_language: "de-DE" }), LanguageGuardBlocked);
assert.throws(() => routeHostContext({ task_kind: "translation", operation: "chat", source_text: "Hello", target_language: "sv-SE" }), LanguageGuardBlocked);
assert.throws(() => routeHostContext({ response_language: "auto" }), LanguageGuardBlocked);
assert.throws(() => parseAgentEnvelope("raw answer"), LanguageGuardBlocked);
assert.throws(() => parseAgentEnvelope(JSON.stringify({ target_text: "Hej", release_token: "x", language: "sv-SE" })), LanguageGuardBlocked);

const requests = [];
const server = net.createServer(socket => {
  socket.setEncoding("utf8");
  let raw = "";
  socket.on("data", chunk => {
    raw += chunk;
    const newline = raw.indexOf("\n");
    if (newline < 0) return;
    const request = JSON.parse(raw.slice(0, newline));
    requests.push(request);
    const valid = request.release_token === "valid-token";
    socket.end(`${JSON.stringify({ valid, checks: { target: valid, language: request.language === "sv-SE" || request.language === "de-DE" } })}\n`);
  });
});

server.listen(0, "127.0.0.1", async () => {
  const endpoint = `tcp:127.0.0.1:${server.address().port}`;
  try {
    const result = await callGuardService(endpoint, { operation: "health" });
    assert.equal(result.valid, false);

    const verified = await verifyForDelivery({
      rawEnvelope: JSON.stringify({ target_text: "Natürlich ist das möglich.", release_token: "valid-token" }),
      hostContext: { operation: "chat", response_language: "de-DE" },
      endpoint,
      agentId: "fredrik",
      channel: "desktop",
    });
    assert.equal(verified.text, "Natürlich ist das möglich.");
    assert.equal(requests.at(-1).task_kind, "response");

    const sent = [];
    await guardedTelegramSend({
      rawEnvelope: JSON.stringify({ target_text: "Hej världen.", release_token: "valid-token" }),
      hostContext: { operation: "chat", response_language: "sv-SE" },
      endpoint,
      botToken: "host-only-token",
      chatId: "123",
      telegramRequest: async (token, method, payload) => {
        sent.push({ token, method, payload });
        return { message_id: 1 };
      },
    });
    assert.deepEqual(sent.map(item => item.payload.text), ["Hej världen."]);
    assert.equal(sent[0].token, "host-only-token");

    const before = sent.length;
    await assert.rejects(() => guardedTelegramSend({
      rawEnvelope: JSON.stringify({ target_text: "Manipulerad text.", release_token: "invalid-token" }),
      hostContext: { operation: "chat", response_language: "sv-SE" },
      endpoint,
      botToken: "host-only-token",
      chatId: "123",
      telegramRequest: async () => sent.push("must-not-send"),
    }), error => error instanceof LanguageGuardBlocked && /failed checks: target/.test(error.message));
    assert.equal(sent.length, before);
  } finally {
    server.close();
  }
});
