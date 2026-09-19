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
assert.equal(routeHostContext({ response_language: "de-AT", session_id: "chat-session" }).taskKind, "response");
assert.equal(routeHostContext({ source_text: "Hello", target_language: "sv-SE" }).taskKind, "translation");
const rewriteRoute = routeHostContext({
  task_kind: "rewrite", operation: "rewrite", source_text: "On tärkeää huomata, että teksti on selkeä.",
  language: "fi-FI", profile_id: "native-fi-general-v1", request_id: "rewrite-one",
  session_id: "rewrite-session", session_epoch: "a".repeat(64), content_type: "marketing",
});
assert.equal(rewriteRoute.taskKind, "rewrite");
assert.equal(rewriteRoute.profileId, "native-fi-general-v1");
assert.equal(routeHostContext({ source_text: "Original", target_language: "en-GB" }).taskKind, "translation");
assert.throws(() => routeHostContext({ task_kind: "rewrite", source_text: "Original", language: "en-GB" }), LanguageGuardBlocked);
assert.throws(() => routeHostContext({ task_kind: "translation", operation: "rewrite", source_text: "Original", target_language: "de-DE" }), LanguageGuardBlocked);
assert.throws(() => routeHostContext({ task_kind: "response", source_text: "Hello", response_language: "de-DE" }), LanguageGuardBlocked);
assert.throws(() => routeHostContext({ task_kind: "translation", operation: "chat", source_text: "Hello", target_language: "sv-SE" }), LanguageGuardBlocked);
assert.throws(() => routeHostContext({ response_language: "auto" }), LanguageGuardBlocked);
assert.throws(() => parseAgentEnvelope("raw answer"), LanguageGuardBlocked);
assert.throws(() => parseAgentEnvelope(JSON.stringify({ target_text: "Hej", release_token: "x", language: "sv-SE" })), LanguageGuardBlocked);

const requests = [];
const rewriteGrants = new Set();
const server = net.createServer(socket => {
  socket.setEncoding("utf8");
  let raw = "";
  socket.on("data", chunk => {
    raw += chunk;
    const newline = raw.indexOf("\n");
    if (newline < 0) return;
    const request = JSON.parse(raw.slice(0, newline));
    requests.push(request);
    if (request.operation === "authorize_delivery" && request.task_kind === "rewrite") {
      const valid = request.release_token === "valid-token"
        && request.profile_id === "native-fi-general-v1"
        && request.request_id === "rewrite-one";
      if (valid) rewriteGrants.add("rewrite-grant");
      socket.end(`${JSON.stringify({ valid, ...(valid ? { delivery_grant: "rewrite-grant" } : {}) })}\n`);
      return;
    }
    if (request.operation === "consume_delivery" && request.task_kind === "rewrite") {
      const valid = rewriteGrants.delete(request.delivery_grant)
        && request.profile_id === "native-fi-general-v1"
        && request.request_id === "rewrite-one"
        && request.source_sha256 === require("node:crypto").createHash("sha256")
          .update("On tärkeää huomata, että teksti on selkeä.", "utf8").digest("hex");
      socket.end(`${JSON.stringify({ valid, checks: { one_time: valid } })}\n`);
      return;
    }
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

    const rewritten = await verifyForDelivery({
      rawEnvelope: JSON.stringify({ target_text: "Teksti on selkeä.", release_token: "valid-token" }),
      hostContext: {
        task_kind: "rewrite", operation: "rewrite",
        source_text: "On tärkeää huomata, että teksti on selkeä.", language: "fi-FI",
        profile_id: "native-fi-general-v1", request_id: "rewrite-one",
        session_id: "rewrite-session", session_epoch: "a".repeat(64), content_type: "marketing",
      },
      endpoint,
      agentId: "writer",
      channel: "desktop",
    });
    assert.equal(rewritten.text, "Teksti on selkeä.");
    assert.equal(requests.at(-2).operation, "authorize_delivery");
    assert.equal(requests.at(-1).operation, "consume_delivery");

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
