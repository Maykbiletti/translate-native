"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const {
  bootstrapLanguageGuardMcp,
  createBlunLanguageGuard,
  loadLegacyGuardConfig,
  readProtectedLegacyConfig,
  readProtectedServiceToken,
  resolveLanguage,
} = require("../integrations/adapters/blun-code-language-guard");

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "blun-code-guard-"));
const tokenFile = path.join(temporary, "service.token");
fs.writeFileSync(tokenFile, "service-token-with-at-least-32-characters\n", { mode: 0o600 });
assert.equal(readProtectedServiceToken(tokenFile), "service-token-with-at-least-32-characters");
if (process.platform !== "win32") {
  const linkedToken = path.join(temporary, "linked-service.token");
  fs.symlinkSync(tokenFile, linkedToken);
  assert.throws(() => readProtectedServiceToken(linkedToken), /regular file/);
  const hardlinkedToken = path.join(temporary, "hardlinked-service.token");
  fs.linkSync(tokenFile, hardlinkedToken);
  assert.throws(() => readProtectedServiceToken(tokenFile), /hard links/);
  fs.unlinkSync(hardlinkedToken);

  const stableTokenStats = fs.statSync(tokenFile);
  const relinkedTokenStats = new Proxy(stableTokenStats, {
    get(target, property) {
      if (property === "nlink") return target.nlink + 1;
      const value = Reflect.get(target, property);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const originalTokenFstatSync = fs.fstatSync;
  let tokenFstatCalls = 0;
  fs.fstatSync = (...args) => {
    const details = originalTokenFstatSync(...args);
    if (details.isDirectory()) return details;
    return ++tokenFstatCalls === 1 ? stableTokenStats : relinkedTokenStats;
  };
  try {
    assert.throws(() => readProtectedServiceToken(tokenFile), /changed while reading/);
  } finally {
    fs.fstatSync = originalTokenFstatSync;
  }

  const tokenTarget = path.join(temporary, "service-token-target");
  fs.mkdirSync(tokenTarget, { mode: 0o700 });
  fs.copyFileSync(tokenFile, path.join(tokenTarget, "service.token"));
  const linkedTokenDirectory = path.join(temporary, "linked-service-token-directory");
  fs.symlinkSync(tokenTarget, linkedTokenDirectory, "dir");
  assert.throws(
    () => readProtectedServiceToken(path.join(linkedTokenDirectory, "service.token")),
    /directory cannot be opened safely/
  );

  const writableTokenDirectory = path.join(temporary, "writable-service-token-directory");
  fs.mkdirSync(writableTokenDirectory, { mode: 0o700 });
  const writableToken = path.join(writableTokenDirectory, "service.token");
  fs.copyFileSync(tokenFile, writableToken);
  fs.chmodSync(writableTokenDirectory, 0o777);
  try {
    assert.throws(
      () => readProtectedServiceToken(writableToken),
      /directory is writable outside its owner/
    );
  } finally {
    fs.chmodSync(writableTokenDirectory, 0o700);
  }

  const missingToken = path.join(temporary, "missing-service-token", "config", "service.token");
  assert.throws(() => readProtectedServiceToken(missingToken));
  assert(!fs.existsSync(path.dirname(missingToken)), "read-only token checks must not create paths");

  const trustedTokenDirectory = path.join(temporary, "trusted-service-token-directory");
  const replacementTokenDirectory = path.join(temporary, "replacement-service-token-directory");
  fs.mkdirSync(trustedTokenDirectory, { mode: 0o700 });
  fs.mkdirSync(replacementTokenDirectory, { mode: 0o700 });
  const originalToken = "b".repeat(64);
  const replacementToken = "c".repeat(64);
  const exchangedToken = path.join(trustedTokenDirectory, "service.token");
  fs.writeFileSync(exchangedToken, `${originalToken}\n`, { mode: 0o600 });
  fs.writeFileSync(
    path.join(replacementTokenDirectory, "service.token"),
    `${replacementToken}\n`,
    { mode: 0o600 }
  );
  const originalExchangeLstatSync = fs.lstatSync;
  const originalExchangeOpenSync = fs.openSync;
  let exchangedTokenDirectory = false;
  let restoredTokenDirectory = false;
  fs.lstatSync = (candidate, ...options) => {
    const details = originalExchangeLstatSync(candidate, ...options);
    if (path.basename(candidate) === path.basename(exchangedToken) && !exchangedTokenDirectory) {
      fs.renameSync(trustedTokenDirectory, `${trustedTokenDirectory}-old`);
      fs.renameSync(replacementTokenDirectory, trustedTokenDirectory);
      exchangedTokenDirectory = true;
    }
    return details;
  };
  fs.openSync = (candidate, ...options) => {
    const descriptor = originalExchangeOpenSync(candidate, ...options);
    if (path.basename(candidate) === path.basename(exchangedToken)
        && exchangedTokenDirectory && !restoredTokenDirectory) {
      fs.renameSync(trustedTokenDirectory, `${trustedTokenDirectory}-replacement`);
      fs.renameSync(`${trustedTokenDirectory}-old`, trustedTokenDirectory);
      restoredTokenDirectory = true;
    }
    return descriptor;
  };
  try {
    assert.equal(readProtectedServiceToken(exchangedToken), originalToken);
  } finally {
    fs.lstatSync = originalExchangeLstatSync;
    fs.openSync = originalExchangeOpenSync;
  }
  assert.equal(
    fs.readFileSync(path.join(`${trustedTokenDirectory}-replacement`, "service.token"), "utf8"),
    `${replacementToken}\n`
  );
}

const protectedHome = path.join(temporary, "protected-home");
const protectedDir = path.join(protectedHome, ".blun");
const protectedConfig = path.join(protectedDir, "mcp.json");
fs.mkdirSync(protectedDir, { recursive: true });
const protectedPayload = JSON.stringify({
  mcpServers: {
    "blun-language-guard": {
      command: "python3",
      args: ["guard.py", "serve"],
      env: {
        BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT: "tcp:127.0.0.1:47631",
        BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE: tokenFile,
      },
    },
  },
});
fs.writeFileSync(protectedConfig, protectedPayload, { mode: 0o600 });
assert.equal(loadLegacyGuardConfig(protectedHome).endpoint, "tcp:127.0.0.1:47631");

const stableStats = fs.statSync(protectedConfig);
const changedStats = new Proxy(stableStats, {
  get(target, property) {
    if (property === "mtimeMs") return target.mtimeMs + 1;
    const value = Reflect.get(target, property);
    return typeof value === "function" ? value.bind(target) : value;
  },
});
const originalFstatSync = fs.fstatSync;
let fstatCalls = 0;
fs.fstatSync = (...args) => (++fstatCalls === 1 ? stableStats : changedStats);
try {
  assert.throws(() => readProtectedLegacyConfig(protectedConfig), /changed while reading/);
} finally {
  fs.fstatSync = originalFstatSync;
}

const oversizedHome = path.join(temporary, "oversized-home");
fs.mkdirSync(path.join(oversizedHome, ".blun"), { recursive: true });
fs.writeFileSync(path.join(oversizedHome, ".blun", "mcp.json"), "x".repeat(1024 * 1024 + 1), { mode: 0o600 });
assert.throws(() => loadLegacyGuardConfig(oversizedHome), /size limit/);

const malformedHome = path.join(temporary, "malformed-home");
fs.mkdirSync(path.join(malformedHome, ".blun"), { recursive: true });
fs.writeFileSync(
  path.join(malformedHome, ".blun", "mcp.json"),
  JSON.stringify({ mcpServers: [] }),
  { mode: 0o600 },
);
assert.throws(() => loadLegacyGuardConfig(malformedHome), /mcpServers must be an object/);

if (process.platform !== "win32") {
  const linkedHome = path.join(temporary, "linked-home");
  fs.mkdirSync(path.join(linkedHome, ".blun"), { recursive: true });
  fs.symlinkSync(protectedConfig, path.join(linkedHome, ".blun", "mcp.json"));
  assert.throws(() => loadLegacyGuardConfig(linkedHome), /regular file/);

  const hardlinkedHome = path.join(temporary, "hardlinked-home");
  fs.mkdirSync(path.join(hardlinkedHome, ".blun"), { recursive: true });
  const hardlinkSource = path.join(temporary, "hardlink-source.json");
  fs.writeFileSync(hardlinkSource, protectedPayload, { mode: 0o600 });
  fs.linkSync(hardlinkSource, path.join(hardlinkedHome, ".blun", "mcp.json"));
  assert.throws(() => loadLegacyGuardConfig(hardlinkedHome), /hard links/);

  const writableHome = path.join(temporary, "writable-home");
  fs.mkdirSync(path.join(writableHome, ".blun"), { recursive: true });
  const writableConfig = path.join(writableHome, ".blun", "mcp.json");
  fs.writeFileSync(writableConfig, protectedPayload, { mode: 0o622 });
  fs.chmodSync(writableConfig, 0o622);
  assert.throws(() => loadLegacyGuardConfig(writableHome), /writable outside/);
}
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

if (process.platform !== "win32") {
  const unsafeHome = path.join(temporary, "unsafe-bootstrap-home");
  fs.mkdirSync(path.join(unsafeHome, ".blun"), { recursive: true });
  fs.symlinkSync(protectedConfig, path.join(unsafeHome, ".blun", "mcp.json"));
  const unsafeStore = { ...store, servers: [] };
  assert.throws(
    () => bootstrapLanguageGuardMcp({ userHome: unsafeHome, store: unsafeStore }),
    error => error instanceof Error && error.code === "guard_unavailable",
  );
  assert.equal(unsafeStore.servers.length, 0, "unsafe legacy state must block before store mutation");
}

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
    assert.equal(context.route.language, "de-DE");
    assert.equal(context.languageSource, "config.language");
    assert.match(guard.mandatoryInstruction(context), /Pass language exactly as "de-DE"/);

    const telegramContext = guard.context({
      messages: [{ role: "user", content: "Antworte bitte." }],
      meta: { telegram: { senderLanguageCode: "en" } },
      channel: "telegram",
    });
    assert.equal(telegramContext.route.language, "de-DE", "Telegram UI language must not override the configured response language");
    assert.equal(telegramContext.languageSource, "config.language");

    const explicitContext = guard.context({
      messages: [{ role: "user", content: "Svara på svenska." }],
      meta: { languageGuardLanguage: "sv-SE", telegram: { senderLanguageCode: "en" } },
      channel: "telegram",
    });
    assert.equal(explicitContext.route.language, "sv-SE");
    assert.equal(explicitContext.languageSource, "meta.languageGuardLanguage");
    assert.deepEqual(
      resolveLanguage({ telegram: { conversationLanguage: "ca-ES", senderLanguageCode: "en" } }, {}),
      { language: "ca-ES", source: "meta.telegram.conversationLanguage" },
    );
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
    assert.equal(released.languageGuard.languageSource, "config.language");
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
