"use strict";

const assert = require("node:assert/strict");
const net = require("node:net");
const {
  LanguageGuardBlocked,
  routeHostContext,
  parseAgentEnvelope,
  callGuardService,
  exactUtf8Hash,
  bindCandidate,
  verifyForDelivery,
  authorizeForDelivery,
  consumeAuthorizedDelivery,
  splitTelegramMessage,
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
const grants = new Map();
const consumed = new Set();
let grantCounter = 0;

function decisionFor(request) {
  if (request.operation === "verify") {
    const valid = request.release_token === "valid-token";
    return { valid, checks: { target: valid, language: ["sv-SE", "de-DE"].includes(request.language) } };
  }
  if (request.operation === "authorize_delivery") {
    if (request.release_token !== "valid-token") {
      return { valid: false, checks: { target: false } };
    }
    const deliveryGrant = `grant-${++grantCounter}`;
    grants.set(deliveryGrant, { ...request });
    return { valid: true, delivery_grant: deliveryGrant };
  }
  if (request.operation === "consume_delivery") {
    const authorized = grants.get(request.delivery_grant);
    const checks = {
      target: authorized?.target_text === request.target_text,
      source: authorized?.source_text === "" && request.source_sha256 === exactUtf8Hash(""),
      session: authorized?.session_id === request.session_id,
      session_epoch: authorized?.session_epoch === request.session_epoch,
      agent: authorized?.agent_id === request.agent_id,
      language: authorized?.language === request.language,
      purpose: authorized?.task_kind === request.task_kind,
      content_type: authorized?.content_type === request.content_type,
      short_text_reviewed: authorized?.short_text_reviewed === request.short_text_reviewed,
      channel: authorized?.channel === request.channel,
      one_time: !consumed.has(request.delivery_grant),
    };
    if (authorized) consumed.add(request.delivery_grant);
    return { valid: Boolean(authorized) && Object.values(checks).every(Boolean), checks };
  }
  return { valid: false };
}

const server = net.createServer(socket => {
  socket.setEncoding("utf8");
  let raw = "";
  socket.on("data", chunk => {
    raw += chunk;
    const newline = raw.indexOf("\n");
    if (newline < 0) return;
    const request = JSON.parse(raw.slice(0, newline));
    requests.push(request);
    socket.end(`${JSON.stringify(decisionFor(request))}\n`);
  });
});

function listen() {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
}

function close() {
  return new Promise(resolve => server.close(resolve));
}

const SESSION_EPOCH = "a".repeat(64);
const base = {
  rawEnvelope: JSON.stringify({ target_text: "Hej världen.", release_token: "valid-token" }),
  hostContext: { operation: "chat", response_language: "sv-SE" },
  agentId: "synthetic-agent",
  sessionId: "synthetic-session",
  sessionEpoch: SESSION_EPOCH,
  channel: "telegram",
};

async function main() {
  await listen();
  const endpoint = `tcp:127.0.0.1:${server.address().port}`;
  try {
    const result = await callGuardService(endpoint, { operation: "health" });
    assert.equal(result.valid, false);

    const verified = await verifyForDelivery({
      rawEnvelope: JSON.stringify({ target_text: "Natürlich ist das möglich.", release_token: "valid-token" }),
      hostContext: { operation: "chat", response_language: "de-DE" },
      endpoint,
      agentId: "synthetic-agent",
      channel: "desktop",
    });
    assert.equal(verified.text, "Natürlich ist das möglich.");
    assert.equal(verified.candidate.sha256, exactUtf8Hash(verified.text));
    assert.equal(Object.isFrozen(verified.candidate), true);
    assert.equal(requests.at(-1).task_kind, "response");

    const exactText = `  Hej\r\n${"åäö ".repeat(40)}😀\n  slut  `;
    const chunks = splitTelegramMessage(exactText, 32);
    assert.equal(chunks.join(""), exactText);
    assert.equal(exactUtf8Hash(chunks.join("")), exactUtf8Hash(exactText));
    assert.ok(chunks.every(chunk => Buffer.from(chunk, "utf8").toString("utf8") === chunk));
    assert.throws(() => splitTelegramMessage(exactText, 1), LanguageGuardBlocked);

    const sent = [];
    const sentResult = await guardedTelegramSend({
      ...base,
      rawEnvelope: JSON.stringify({ target_text: exactText, release_token: "valid-token" }),
      endpoint,
      botToken: "host-only-token",
      chatId: "123",
      chunkLimit: 32,
      telegramRequest: async (token, method, payload) => {
        sent.push({ token, method, payload });
        return { message_id: sent.length };
      },
    });
    assert.equal(sent.map(item => item.payload.text).join(""), exactText);
    assert.equal(sent[0].token, "host-only-token");
    assert.equal(sentResult.candidateSha256, exactUtf8Hash(exactText));
    assert.deepEqual(requests.slice(-2).map(request => request.operation), ["authorize_delivery", "consume_delivery"]);
    assert.equal(requests.at(-1).target_text, exactText);

    const authorization = await authorizeForDelivery({ ...base, endpoint });
    await assert.rejects(() => consumeAuthorizedDelivery({
      authorization,
      candidate: bindCandidate(`${authorization.candidate.text} `),
      endpoint,
    }), error => error instanceof LanguageGuardBlocked && error.code === "candidate_changed");
    await consumeAuthorizedDelivery({ authorization, endpoint });
    await assert.rejects(() => consumeAuthorizedDelivery({ authorization, endpoint }), error => (
      error instanceof LanguageGuardBlocked
      && error.code === "delivery_rejected"
      && /one_time/.test(error.message)
    ));

    const before = sent.length;
    await assert.rejects(() => guardedTelegramSend({
      ...base,
      rawEnvelope: JSON.stringify({ target_text: "Manipulerad text.", release_token: "invalid-token" }),
      endpoint,
      botToken: "host-only-token",
      chatId: "123",
      telegramRequest: async () => sent.push("must-not-send"),
    }), error => error instanceof LanguageGuardBlocked && /failed checks: target/.test(error.message));
    assert.equal(sent.length, before);

    const requestCount = requests.length;
    await assert.rejects(() => guardedTelegramSend({
      ...base,
      endpoint,
      telegramRequest: null,
    }), error => error instanceof LanguageGuardBlocked && error.code === "delivery_unavailable");
    assert.equal(requests.length, requestCount);

    await assert.rejects(() => guardedTelegramSend({
      ...base,
      endpoint: "tcp:127.0.0.1:1",
      telegramRequest: async () => sent.push("must-not-send"),
    }), error => error instanceof LanguageGuardBlocked && error.code === "guard_unavailable");
    assert.equal(sent.length, before);
  } finally {
    await close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
