"use strict";

const assert = require("assert");
const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const HOOK = path.join(ROOT, "integrations", "claude_language_hook.js");
const { beginSessionEpoch, readProtectedRecord, readProtectedServiceToken, readSessionEpoch, removeExactRecord } = require(HOOK);

function runHook(mode, input, environment) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [HOOK, mode], {
      env: { ...process.env, ...environment },
      stdio: ["pipe", "pipe", "pipe"]
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, stdout, stderr }));
    child.stdin.end(`${JSON.stringify(input)}\n`);
  });
}

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "blun-claude-hook-"));
  const replacedRecord = path.join(temporary, "replaced-record.json");
  fs.writeFileSync(replacedRecord, '{"grant":"first"}\n', { mode: 0o600 });
  const inspectedRecord = readProtectedRecord(replacedRecord);
  fs.renameSync(replacedRecord, `${replacedRecord}.old`);
  fs.writeFileSync(replacedRecord, '{"grant":"other"}\n', { mode: 0o600 });
  assert.throws(
    () => removeExactRecord(replacedRecord, inspectedRecord.fileIdentity),
    /changed before consumption/
  );
  assert(fs.existsSync(replacedRecord), "a replacement record must not be removed");
  fs.unlinkSync(replacedRecord);
  fs.unlinkSync(`${replacedRecord}.old`);
  const token = "a".repeat(64);
  const serviceTokenFile = path.join(temporary, "service.token");
  fs.writeFileSync(serviceTokenFile, `${token}\n`, { mode: 0o600 });
  assert.strictEqual(readProtectedServiceToken(serviceTokenFile), token);
  if (process.platform !== "win32") {
    const linkedToken = path.join(temporary, "linked-service.token");
    fs.symlinkSync(serviceTokenFile, linkedToken);
    assert.throws(() => readProtectedServiceToken(linkedToken), /regular file/);
  }
  const grants = new Map();
  const sessionEpochs = new Map();
  const sessionEpochHistory = new Map();

  const server = net.createServer((client) => {
    let raw = "";
    client.on("data", (chunk) => {
      raw += chunk.toString("utf8");
      const newline = raw.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(raw.slice(0, newline));
      let response;
      let responseDelay = 0;
      if (request.operation === "health") {
        response = { status: "ok", isolated_key: true, version: "6.25.0" };
      } else if (request.operation === "register_session_epoch") {
        const history = sessionEpochHistory.get(request.session_id) || new Set();
        const valid = request.service_token === token
          && request.session_id !== "session-registration-rejected"
          && /^[a-f0-9]{64}$/.test(String(request.session_epoch || ""))
          && !history.has(request.session_epoch);
        if (valid) {
          history.add(request.session_epoch);
          sessionEpochHistory.set(request.session_id, history);
          sessionEpochs.set(request.session_id, request.session_epoch);
        }
        response = { status: valid ? "PASS" : "BLOCK", registered: valid };
      } else if (request.operation === "retire_session_epoch") {
        const knownEpoch = sessionEpochs.get(request.session_id);
        const valid = request.service_token === token
          && knownEpoch === request.session_epoch;
        if (valid) {
          const tombstone = crypto.randomBytes(32).toString("hex");
          const history = sessionEpochHistory.get(request.session_id) || new Set();
          history.add(tombstone);
          sessionEpochHistory.set(request.session_id, history);
          sessionEpochs.set(request.session_id, tombstone);
        }
        response = { status: valid ? "PASS" : "BLOCK", retired: valid };
      } else if (request.operation === "authorize_delivery") {
        if (request.release_token === "restart-valid-token") {
          grants.clear();
          sessionEpochs.clear();
          sessionEpochHistory.clear();
        }
        const knownEpoch = sessionEpochs.get(request.session_id);
        const history = sessionEpochHistory.get(request.session_id) || new Set();
        const valid = request.service_token === token
          && ["valid-token", "slow-valid-token", "restart-valid-token"].includes(request.release_token)
          && (knownEpoch === request.session_epoch || (knownEpoch === undefined && !history.has(request.session_epoch)));
        if (valid && knownEpoch === undefined) {
          history.add(request.session_epoch);
          sessionEpochHistory.set(request.session_id, history);
          sessionEpochs.set(request.session_id, request.session_epoch);
        }
        if (request.release_token === "slow-valid-token") responseDelay = 100;
        if (request.release_token === "slow-rejected-token") responseDelay = 500;
        const grant = `grant-${crypto.randomUUID()}`;
        if (valid) grants.set(grant, {
          source_sha256: crypto.createHash("sha256").update(
            String(request.source_text || "").replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").normalize("NFC"),
            "utf8"
          ).digest("hex"),
          target_text: request.target_text,
          language: request.language,
          task_kind: request.task_kind,
          content_type: request.content_type || "prose",
          short_text_reviewed: request.short_text_reviewed === true,
          session_id: request.session_id,
          session_epoch: request.session_epoch,
          agent_id: request.agent_id,
          channel: request.channel
        });
        response = {
          status: valid ? "PASS" : "BLOCK",
          valid,
          ...(valid ? { delivery_grant: grant } : {})
        };
      } else if (request.operation === "consume_delivery") {
        const expected = grants.get(request.delivery_grant);
        const valid = request.service_token === token && expected
          && expected.source_sha256 === request.source_sha256
          && expected.target_text === request.target_text
          && expected.language === request.language
          && expected.task_kind === request.task_kind
          && expected.content_type === request.content_type
          && expected.short_text_reviewed === request.short_text_reviewed
          && expected.session_id === request.session_id
          && expected.session_epoch === request.session_epoch
          && sessionEpochs.get(request.session_id) === request.session_epoch
          && expected.agent_id === request.agent_id
          && expected.channel === request.channel;
        if (valid) grants.delete(request.delivery_grant);
        response = { status: valid ? "PASS" : "BLOCK", valid: Boolean(valid) };
      } else {
        response = { status: "BLOCK", valid: false };
      }
      const finish = () => client.end(`${JSON.stringify(response)}\n`);
      if (responseDelay > 0) setTimeout(finish, responseDelay); else finish();
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address === "object");
  fs.writeFileSync(path.join(temporary, "delivery-policy.json"), JSON.stringify({
    mandatory: true,
    isolated_service: {
      required: true,
      endpoint: `tcp:127.0.0.1:${address.port}`,
      token_file: path.join(temporary, "service.token")
    }
  }));

  const environment = { BLUN_LANGUAGE_GUARD_RUNTIME: temporary };
  const previousRuntime = process.env.BLUN_LANGUAGE_GUARD_RUNTIME;
  process.env.BLUN_LANGUAGE_GUARD_RUNTIME = temporary;
  try {
    const protectedInput = { session_id: "session-epoch-hardening", cwd: temporary };
    const protectedEpoch = path.join(
      temporary,
      "claude-hooks",
      `session-${crypto.createHash("sha256").update(protectedInput.session_id).digest("hex")}.epoch`
    );
    fs.mkdirSync(path.dirname(protectedEpoch), { recursive: true, mode: 0o700 });
    const legacyTemporary = `${protectedEpoch}.${process.pid}.tmp`;
    const sentinel = path.join(temporary, "epoch-temp-sentinel.txt");
    fs.writeFileSync(sentinel, "do-not-overwrite\n", { mode: 0o600 });
    fs.symlinkSync(sentinel, legacyTemporary);
    await beginSessionEpoch(protectedInput);
    assert.strictEqual(fs.readFileSync(sentinel, "utf8"), "do-not-overwrite\n");
    assert(fs.lstatSync(legacyTemporary).isSymbolicLink(), "legacy predictable temp link must remain untouched");
    fs.unlinkSync(legacyTemporary);

    const inspectedEpoch = readSessionEpoch(protectedInput);
    fs.renameSync(protectedEpoch, `${protectedEpoch}.old`);
    fs.writeFileSync(protectedEpoch, `${"f".repeat(64)}\n`, { mode: 0o600 });
    assert.throws(
      () => removeExactRecord(protectedEpoch, inspectedEpoch.fileIdentity),
      /changed before consumption/
    );
    assert.strictEqual(fs.readFileSync(protectedEpoch, "utf8"), `${"f".repeat(64)}\n`);
    fs.unlinkSync(protectedEpoch);
    fs.renameSync(`${protectedEpoch}.old`, protectedEpoch);

    fs.writeFileSync(protectedEpoch, `${"a".repeat(129)}\n`, { mode: 0o600 });
    assert.throws(() => readSessionEpoch(protectedInput), /invalid size/);
    fs.unlinkSync(protectedEpoch);
  } finally {
    if (previousRuntime === undefined) delete process.env.BLUN_LANGUAGE_GUARD_RUNTIME;
    else process.env.BLUN_LANGUAGE_GUARD_RUNTIME = previousRuntime;
  }
  const common = { session_id: "session-one", cwd: temporary };
  const clean = "Natürlich läuft die Prüfung für Claude zuverlässig.";
  const tool = {
    ...common,
    hook_event_name: "PostToolUse",
    tool_name: "mcp__plugin_translate-native_guard__release_response",
    tool_use_id: "tool-one",
    tool_input: {
      target_text: clean,
      language: "de-DE",
      attestations: { nativeness: true, orthography: true }
    },
    tool_response: {
      content: [{ type: "text", text: JSON.stringify({ release_allowed: true, release_token: "valid-token" }) }]
    }
  };

  const started = await runHook("session-start", common, environment);
  assert.strictEqual(started.code, 0, started.stderr);
  assert.match(started.stdout, /SessionStart/);
  assert.match(started.stdout, /mandatory/);

  for (const invalidMessage of [undefined, null, { text: clean }]) {
    const malformedInput = {
      ...common,
      hook_event_name: "Stop",
      stop_hook_active: false
    };
    if (invalidMessage !== undefined) malformedInput.last_assistant_message = invalidMessage;
    const malformedStop = await runHook("stop", malformedInput, environment);
    const malformedBlock = JSON.parse(malformedStop.stdout);
    assert.strictEqual(malformedBlock.decision, "block");
    assert.match(malformedBlock.reason, /last_assistant_message/);
  }

  const malformedRepeatedSubagentStop = await runHook("stop", {
    ...common,
    agent_id: "child-invalid-output",
    hook_event_name: "SubagentStop",
    last_assistant_message: [clean],
    stop_hook_active: true
  }, environment);
  const malformedSubagentHardStop = JSON.parse(malformedRepeatedSubagentStop.stdout);
  assert.strictEqual(malformedSubagentHardStop.continue, false);
  assert.match(malformedSubagentHardStop.stopReason, /stopped an unverified response/);

  const policyEnvironment = {
    ...environment,
    BLUN_LANGUAGE_GUARD_LANGUAGE: "de-DE",
    BLUN_LANGUAGE_GUARD_TASK_KIND: "response"
  };
  const policyCommon = { ...common, session_id: "session-policy" };
  const policyStarted = await runHook("session-start", {
    ...policyCommon,
    hook_event_name: "SessionStart",
    source: "startup"
  }, policyEnvironment);
  const policyStartupContext = JSON.parse(policyStarted.stdout).hookSpecificOutput.additionalContext;
  assert.match(policyStartupContext, /requires release_response/);
  assert.match(policyStartupContext, /language exactly as "de-DE"/);

  const rewrittenRelease = await runHook("pre-tool", {
    ...policyCommon,
    hook_event_name: "PreToolUse",
    tool_name: "mcp__plugin_translate-native_guard__release_response",
    tool_input: { target_text: clean, language: "en" }
  }, policyEnvironment);
  const rewrittenOutput = JSON.parse(rewrittenRelease.stdout).hookSpecificOutput;
  assert.strictEqual(rewrittenOutput.hookEventName, "PreToolUse");
  assert.strictEqual(rewrittenOutput.updatedInput.language, "de-DE");
  assert.strictEqual(rewrittenOutput.updatedInput.target_text, clean);
  assert.match(rewrittenOutput.additionalContext, /"de-DE"/);

  const directTelegramReply = await runHook("pre-delivery", {
    ...policyCommon,
    hook_event_name: "PreToolUse",
    tool_name: "mcp__plugin-telegram_telegram__reply",
    tool_input: { chat_id: "private-chat", text: clean }
  }, { BLUN_LANGUAGE_GUARD_RUNTIME: path.join(temporary, "unavailable-guard") });
  const directTelegramOutput = JSON.parse(directTelegramReply.stdout).hookSpecificOutput;
  assert.strictEqual(directTelegramOutput.hookEventName, "PreToolUse");
  assert.strictEqual(directTelegramOutput.permissionDecision, "deny");
  assert.match(directTelegramOutput.permissionDecisionReason, /host-owned bridge/);
  assert(!directTelegramReply.stdout.includes(clean), "direct-delivery denial must not expose candidate text");

  const telegramReadTool = await runHook("pre-delivery", {
    ...policyCommon,
    hook_event_name: "PreToolUse",
    tool_name: "mcp__plugin-telegram_telegram__get_updates",
    tool_input: { chat_id: "private-chat" }
  }, policyEnvironment);
  assert.strictEqual(telegramReadTool.stdout, "", "read-only Telegram tools must remain available");

  const wrongPurpose = await runHook("pre-tool", {
    ...policyCommon,
    hook_event_name: "PreToolUse",
    tool_name: "mcp__plugin_translate-native_guard__release_translation",
    tool_input: { source_text: "Hello", target_text: "Hallo", language: "de-DE" }
  }, policyEnvironment);
  const wrongPurposeOutput = JSON.parse(wrongPurpose.stdout).hookSpecificOutput;
  assert.strictEqual(wrongPurposeOutput.permissionDecision, "deny");
  assert.match(wrongPurposeOutput.permissionDecisionReason, /requires release_response/);

  const invalidPolicy = await runHook("pre-tool", {
    ...policyCommon,
    hook_event_name: "PreToolUse",
    tool_name: "mcp__plugin_translate-native_guard__release_response",
    tool_input: { target_text: clean, language: "de-DE" }
  }, { ...environment, BLUN_LANGUAGE_GUARD_LANGUAGE: "auto" });
  const invalidPolicyOutput = JSON.parse(invalidPolicy.stdout).hookSpecificOutput;
  assert.strictEqual(invalidPolicyOutput.permissionDecision, "deny");
  assert.match(invalidPolicyOutput.permissionDecisionReason, /invalid host release policy/);

  const policyPrompt = await runHook("prompt-boundary", {
    ...policyCommon,
    hook_event_name: "UserPromptSubmit",
    prompt: "Antworte bitte."
  }, policyEnvironment);
  const policyPromptOutput = JSON.parse(policyPrompt.stdout).hookSpecificOutput;
  assert.strictEqual(policyPromptOutput.hookEventName, "UserPromptSubmit");
  assert.match(policyPromptOutput.additionalContext, /language exactly as "de-DE"/);

  const policyTool = {
    ...tool,
    ...policyCommon,
    tool_use_id: "tool-policy"
  };
  const mismatchedPolicyRelease = await runHook("post-tool", {
    ...policyTool,
    tool_input: { ...policyTool.tool_input, language: "de" }
  }, policyEnvironment);
  assert.strictEqual(JSON.parse(mismatchedPolicyRelease.stdout).decision, "block");
  assert.match(mismatchedPolicyRelease.stdout, /language exactly \\"de-DE\\"/);
  assert(!mismatchedPolicyRelease.stdout.includes(clean), "policy rejection must not expose candidate text");

  const matchingPolicyRelease = await runHook("post-tool", policyTool, policyEnvironment);
  assert.strictEqual(matchingPolicyRelease.stdout, "");
  const policyDelivered = await runHook("stop", {
    ...policyCommon,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, policyEnvironment);
  assert.strictEqual(policyDelivered.stdout, "");
  const policyEnded = await runHook("session-end", {
    ...policyCommon,
    hook_event_name: "SessionEnd",
    reason: "other"
  }, policyEnvironment);
  assert.strictEqual(policyEnded.stdout, "");

  const otherSessionStarted = await runHook("session-start", {
    ...common,
    session_id: "session-two",
    hook_event_name: "SessionStart",
    source: "startup"
  }, environment);
  assert.strictEqual(otherSessionStarted.code, 0, otherSessionStarted.stderr);

  const releaseWithoutSessionStart = await runHook("post-tool", {
    ...tool,
    session_id: "session-without-start",
    tool_use_id: "tool-without-session-start"
  }, environment);
  assert.strictEqual(JSON.parse(releaseWithoutSessionStart.stdout).decision, "block");
  assert.match(releaseWithoutSessionStart.stdout, /no valid epoch/);

  const stateDirectory = path.join(temporary, "claude-hooks");
  const brokenSessionHash = crypto.createHash("sha256").update("session-broken").digest("hex");
  const brokenEpochPath = path.join(stateDirectory, `session-${brokenSessionHash}.epoch`);
  fs.mkdirSync(brokenEpochPath);
  const brokenResume = await runHook("session-start", {
    ...common,
    session_id: "session-broken",
    hook_event_name: "SessionStart",
    source: "resume"
  }, environment);
  assert.strictEqual(brokenResume.code, 0, brokenResume.stderr);
  assert.match(brokenResume.stdout, /delivery epoch could not be renewed/);
  const blockedBrokenSession = await runHook("post-tool", {
    ...tool,
    session_id: "session-broken",
    tool_use_id: "tool-broken-session"
  }, environment);
  assert.strictEqual(JSON.parse(blockedBrokenSession.stdout).decision, "block");
  fs.rmSync(brokenEpochPath, { recursive: true, force: true });

  const rejectedRegistration = await runHook("session-start", {
    ...common,
    session_id: "session-registration-rejected",
    hook_event_name: "SessionStart",
    source: "resume"
  }, environment);
  assert.strictEqual(rejectedRegistration.code, 0, rejectedRegistration.stderr);
  assert.match(rejectedRegistration.stdout, /delivery epoch could not be renewed/);
  const rejectedSessionHash = crypto.createHash("sha256").update("session-registration-rejected").digest("hex");
  assert(!fs.existsSync(path.join(stateDirectory, `session-${rejectedSessionHash}.epoch`)));
  const blockedRejectedRegistration = await runHook("post-tool", {
    ...tool,
    session_id: "session-registration-rejected",
    tool_use_id: "tool-rejected-registration"
  }, environment);
  assert.strictEqual(JSON.parse(blockedRejectedRegistration.stdout).decision, "block");

  const subagentStarted = await runHook("subagent-start", {
    ...common,
    hook_event_name: "SubagentStart",
    agent_id: "child-agent",
    agent_type: "Explore"
  }, environment);
  assert.strictEqual(subagentStarted.code, 0, subagentStarted.stderr);
  const subagentContext = JSON.parse(subagentStarted.stdout).hookSpecificOutput;
  assert.strictEqual(subagentContext.hookEventName, "SubagentStart");
  assert.match(subagentContext.additionalContext, /This subagent/);
  assert.match(subagentContext.additionalContext, /release_response/);
  assert.match(subagentContext.additionalContext, /release_translation/);
  assert.match(subagentContext.additionalContext, /this session and agent identity/);

  const firstRecorded = await runHook("post-tool", tool, environment);
  assert.strictEqual(firstRecorded.stdout, "");
  const stateFile = path.join(stateDirectory, fs.readdirSync(stateDirectory).find((name) => name.endsWith(".json")));
  const staleBeforeResume = fs.readFileSync(stateFile, "utf8");
  const epochFilesBeforeResume = fs.readdirSync(stateDirectory).filter((name) => name.endsWith(".epoch"));
  assert.strictEqual(epochFilesBeforeResume.length, 2);
  const mainEpochFile = epochFilesBeforeResume.find((name) => name.includes(crypto.createHash("sha256").update("session-one").digest("hex")));
  assert(mainEpochFile);
  const firstEpoch = fs.readFileSync(path.join(stateDirectory, mainEpochFile), "utf8").trim();

  const resumed = await runHook("session-start", {
    ...common,
    hook_event_name: "SessionStart",
    source: "resume"
  }, environment);
  assert.strictEqual(resumed.code, 0, resumed.stderr);
  assert.match(resumed.stdout, /SessionStart/);
  const secondEpoch = fs.readFileSync(path.join(stateDirectory, mainEpochFile), "utf8").trim();
  assert.notStrictEqual(secondEpoch, firstEpoch, "resume must rotate the delivery epoch");
  assert(!fs.existsSync(stateFile), "resume must remove the earlier local grant");
  if (process.platform !== "win32") {
    fs.chmodSync(path.join(stateDirectory, mainEpochFile), 0o644);
    const unsafeEpochRelease = await runHook("post-tool", {
      ...tool,
      tool_use_id: "tool-unsafe-epoch"
    }, environment);
    assert.strictEqual(JSON.parse(unsafeEpochRelease.stdout).decision, "block");
    assert.match(unsafeEpochRelease.stdout, /no valid epoch/);
    fs.chmodSync(path.join(stateDirectory, mainEpochFile), 0o600);
  }
  fs.writeFileSync(stateFile, staleBeforeResume, { mode: 0o600 });
  fs.writeFileSync(path.join(stateDirectory, mainEpochFile), `${firstEpoch}\n`, { mode: 0o600 });
  const staleAfterResume = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(staleAfterResume.stdout).decision, "block");
  fs.writeFileSync(path.join(stateDirectory, mainEpochFile), `${secondEpoch}\n`, { mode: 0o600 });

  const beforeServiceRestart = await runHook("post-tool", tool, environment);
  assert.strictEqual(beforeServiceRestart.stdout, "");
  const preRestartRecord = fs.readFileSync(stateFile, "utf8");
  const recoveredAfterServiceRestart = await runHook("post-tool", {
    ...tool,
    tool_use_id: "tool-after-service-restart",
    tool_response: {
      structuredContent: { release_allowed: true, release_token: "restart-valid-token" }
    }
  }, environment);
  assert.strictEqual(recoveredAfterServiceRestart.stdout, "");
  const deliveredAfterServiceRestart = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(deliveredAfterServiceRestart.stdout, "");
  fs.writeFileSync(stateFile, preRestartRecord, { mode: 0o600 });
  const oldGrantAfterServiceRestart = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(oldGrantAfterServiceRestart.stdout).decision, "block");

  const recorded = await runHook("post-tool", tool, environment);
  assert.strictEqual(recorded.code, 0, recorded.stderr);
  assert.strictEqual(recorded.stdout, "");
  const copiedRecord = fs.readFileSync(stateFile, "utf8");
  const parsedRecord = JSON.parse(copiedRecord);
  assert.strictEqual(parsedRecord.language, "de-DE");
  assert.strictEqual(parsedRecord.task_kind, "response");
  assert.strictEqual(parsedRecord.session_sha256, crypto.createHash("sha256").update("session-one").digest("hex"));
  assert.strictEqual(parsedRecord.session_epoch_sha256, crypto.createHash("sha256").update(secondEpoch).digest("hex"));
  assert.strictEqual(parsedRecord.source_sha256, crypto.createHash("sha256").update("").digest("hex"));
  assert.strictEqual(parsedRecord.channel, "claude-hook");

  if (process.platform !== "win32") {
    fs.chmodSync(stateFile, 0o644);
    const broadPermissions = await runHook("stop", {
      ...common,
      hook_event_name: "Stop",
      last_assistant_message: clean,
      stop_hook_active: false
    }, environment);
    assert.strictEqual(JSON.parse(broadPermissions.stdout).decision, "block");
    assert(fs.existsSync(stateFile), "an unsafe record must not be consumed");
    fs.chmodSync(stateFile, 0o600);
    const recoveredPermissions = await runHook("stop", {
      ...common,
      hook_event_name: "Stop",
      last_assistant_message: clean,
      stop_hook_active: false
    }, environment);
    assert.strictEqual(recoveredPermissions.stdout, "");

    await runHook("post-tool", { ...tool, tool_use_id: "tool-symlink-record" }, environment);
    const externalRecord = path.join(temporary, "external-grant-record.json");
    const externalBytes = fs.readFileSync(stateFile);
    fs.writeFileSync(externalRecord, externalBytes, { mode: 0o600 });
    fs.unlinkSync(stateFile);
    fs.symlinkSync(externalRecord, stateFile);
    const symlinkRecord = await runHook("stop", {
      ...common,
      hook_event_name: "Stop",
      last_assistant_message: clean,
      stop_hook_active: false
    }, environment);
    assert.strictEqual(JSON.parse(symlinkRecord.stdout).decision, "block");
    assert(fs.lstatSync(stateFile).isSymbolicLink(), "the hook must not consume a symlink as protected state");
    assert.deepStrictEqual(fs.readFileSync(externalRecord), externalBytes, "symlink rejection must not alter its target");
    fs.unlinkSync(stateFile);
    fs.unlinkSync(externalRecord);
  } else {
    fs.unlinkSync(stateFile);
  }

  await runHook("post-tool", { ...tool, tool_use_id: "tool-oversized-record" }, environment);
  fs.writeFileSync(stateFile, `${"x".repeat(64 * 1024)}\n`, { mode: 0o600 });
  const oversizedRecord = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(oversizedRecord.stdout).decision, "block");
  assert(fs.statSync(stateFile).size > 64 * 1024, "oversized state must remain unconsumed");
  fs.unlinkSync(stateFile);

  const childTool = { ...tool, agent_id: "child-agent", tool_use_id: "tool-child" };
  const otherSessionTool = { ...tool, session_id: "session-two", tool_use_id: "tool-other-session" };
  await runHook("post-tool", childTool, environment);
  await runHook("post-tool", otherSessionTool, environment);
  const legacyChild = { ...parsedRecord };
  delete legacyChild.session_sha256;
  const legacyChildFile = path.join(
    stateDirectory,
    `${crypto.createHash("sha256").update("legacy-session\0child-agent").digest("hex")}.json`
  );
  fs.writeFileSync(legacyChildFile, `${JSON.stringify(legacyChild)}\n`, { mode: 0o600 });
  const invalidated = await runHook("prompt-boundary", {
    ...common,
    hook_event_name: "UserPromptSubmit",
    prompt: "Beginne eine neue Aufgabe."
  }, environment);
  assert.strictEqual(invalidated.stdout, "");
  const remainingAfterBoundary = fs.readdirSync(stateDirectory).filter((name) => name.endsWith(".json"));
  assert.strictEqual(remainingAfterBoundary.length, 1, "only another labeled session's grant may remain");
  const remainingRecord = JSON.parse(fs.readFileSync(path.join(stateDirectory, remainingAfterBoundary[0]), "utf8"));
  assert.strictEqual(remainingRecord.session_sha256, crypto.createHash("sha256").update("session-two").digest("hex"));

  const crossTurnReplay = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(crossTurnReplay.stdout).decision, "block");
  const concurrentSessionReleased = await runHook("stop", {
    ...common,
    session_id: "session-two",
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(concurrentSessionReleased.stdout, "");
  assert.strictEqual(fs.readdirSync(stateDirectory).filter((name) => name.endsWith(".json")).length, 0);

  await runHook("post-tool", tool, environment);
  await runHook("post-tool", childTool, environment);
  await runHook("post-tool", otherSessionTool, environment);
  const recordBeforeStopFailure = fs.readFileSync(stateFile, "utf8");
  const epochBeforeStopFailure = fs.readFileSync(path.join(stateDirectory, mainEpochFile), "utf8").trim();
  const failedTurn = await runHook("stop-failure", {
    ...common,
    hook_event_name: "StopFailure",
    error: "rate_limit",
    error_details: `private diagnostic containing ${clean}`,
    last_assistant_message: `API Error containing ${clean}`
  }, environment);
  assert.strictEqual(failedTurn.code, 0, failedTurn.stderr);
  assert.strictEqual(failedTurn.stdout, "", "StopFailure cleanup must not expose failure or candidate text");
  const epochAfterStopFailure = fs.readFileSync(path.join(stateDirectory, mainEpochFile), "utf8").trim();
  assert.notStrictEqual(epochAfterStopFailure, epochBeforeStopFailure, "StopFailure must rotate the service-authoritative epoch");
  const remainingAfterStopFailure = fs.readdirSync(stateDirectory).filter((name) => name.endsWith(".json"));
  assert.strictEqual(remainingAfterStopFailure.length, 1, "StopFailure must clear every grant for only its session");
  const preservedAfterStopFailure = JSON.parse(fs.readFileSync(path.join(stateDirectory, remainingAfterStopFailure[0]), "utf8"));
  assert.strictEqual(preservedAfterStopFailure.session_sha256, crypto.createHash("sha256").update("session-two").digest("hex"));
  const mainAfterStopFailure = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(mainAfterStopFailure.stdout).decision, "block");
  const childAfterStopFailure = await runHook("stop", {
    ...common,
    hook_event_name: "SubagentStop",
    agent_id: "child-agent",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(childAfterStopFailure.stdout).decision, "block");
  const otherSessionAfterStopFailure = await runHook("stop", {
    ...common,
    session_id: "session-two",
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(otherSessionAfterStopFailure.stdout, "");

  fs.writeFileSync(stateFile, recordBeforeStopFailure, { mode: 0o600 });
  fs.writeFileSync(path.join(stateDirectory, mainEpochFile), `${epochBeforeStopFailure}\n`, { mode: 0o600 });
  const restoredAfterStopFailure = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(restoredAfterStopFailure.stdout).decision, "block", "restored local state must fail against the rotated service epoch");
  fs.writeFileSync(path.join(stateDirectory, mainEpochFile), `${epochAfterStopFailure}\n`, { mode: 0o600 });
  const freshAfterStopFailure = await runHook("post-tool", {
    ...tool,
    tool_use_id: "tool-fresh-after-stop-failure"
  }, environment);
  assert.strictEqual(freshAfterStopFailure.stdout, "");
  const deliveredFreshAfterStopFailure = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(deliveredFreshAfterStopFailure.stdout, "");

  await runHook("post-tool", tool, environment);
  await runHook("post-tool", childTool, environment);
  await runHook("post-tool", otherSessionTool, environment);
  const recordBeforeSessionEnd = fs.readFileSync(stateFile, "utf8");
  const epochBeforeSessionEnd = fs.readFileSync(path.join(stateDirectory, mainEpochFile), "utf8").trim();
  const ended = await runHook("session-end", {
    ...common,
    hook_event_name: "SessionEnd",
    reason: "other"
  }, environment);
  assert.strictEqual(ended.code, 0, ended.stderr);
  assert.strictEqual(ended.stdout, "", "SessionEnd cleanup must be silent");
  assert(!fs.existsSync(path.join(stateDirectory, mainEpochFile)), "SessionEnd must remove the local epoch first");
  const remainingAfterSessionEnd = fs.readdirSync(stateDirectory).filter((name) => name.endsWith(".json"));
  assert.strictEqual(remainingAfterSessionEnd.length, 1, "SessionEnd must clear only its session");
  const preservedAfterSessionEnd = JSON.parse(fs.readFileSync(path.join(stateDirectory, remainingAfterSessionEnd[0]), "utf8"));
  assert.strictEqual(preservedAfterSessionEnd.session_sha256, crypto.createHash("sha256").update("session-two").digest("hex"));

  fs.writeFileSync(stateFile, recordBeforeSessionEnd, { mode: 0o600 });
  fs.writeFileSync(path.join(stateDirectory, mainEpochFile), `${epochBeforeSessionEnd}\n`, { mode: 0o600 });
  const restoredAfterSessionEnd = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(restoredAfterSessionEnd.stdout).decision, "block", "restored ended-session state must fail against the service tombstone");

  const resumedAfterSessionEnd = await runHook("session-start", {
    ...common,
    hook_event_name: "SessionStart",
    source: "resume"
  }, environment);
  assert.strictEqual(resumedAfterSessionEnd.code, 0, resumedAfterSessionEnd.stderr);
  const epochAfterSessionEnd = fs.readFileSync(path.join(stateDirectory, mainEpochFile), "utf8").trim();
  assert.notStrictEqual(epochAfterSessionEnd, epochBeforeSessionEnd, "resume must replace the retired epoch");
  const freshAfterSessionEnd = await runHook("post-tool", {
    ...tool,
    tool_use_id: "tool-fresh-after-session-end"
  }, environment);
  assert.strictEqual(freshAfterSessionEnd.stdout, "");
  const deliveredAfterSessionEnd = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(deliveredAfterSessionEnd.stdout, "");
  const parallelAfterSessionEnd = await runHook("stop", {
    ...common,
    session_id: "session-two",
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(parallelAfterSessionEnd.stdout, "");

  await runHook("post-tool", tool, environment);
  await runHook("post-tool", otherSessionTool, environment);
  const failedRelease = await runHook("post-tool-failure", {
    ...common,
    hook_event_name: "PostToolUseFailure",
    tool_name: "mcp__plugin_translate-native_guard__release_response",
    tool_use_id: "tool-failed-release",
    tool_input: tool.tool_input,
    error: "connection closed"
  }, environment);
  assert.strictEqual(failedRelease.code, 0, failedRelease.stderr);
  const failedReleaseOutput = JSON.parse(failedRelease.stdout);
  assert.strictEqual(failedReleaseOutput.decision, "block");
  assert.strictEqual(failedReleaseOutput.hookSpecificOutput.hookEventName, "PostToolUseFailure");
  assert.match(failedReleaseOutput.hookSpecificOutput.additionalContext, /earlier unconsumed grant.*invalidated/);
  assert.match(failedReleaseOutput.hookSpecificOutput.additionalContext, /release_translation/);
  assert.match(failedReleaseOutput.hookSpecificOutput.additionalContext, /release_response/);
  assert(!failedRelease.stdout.includes(clean), "failure output must not expose candidate text");
  const remainingAfterFailure = fs.readdirSync(stateDirectory).filter((name) => name.endsWith(".json"));
  assert.strictEqual(remainingAfterFailure.length, 1, "a failed release may invalidate only the same session and agent");
  const preservedAfterFailure = JSON.parse(fs.readFileSync(path.join(stateDirectory, remainingAfterFailure[0]), "utf8"));
  assert.strictEqual(preservedAfterFailure.session_sha256, crypto.createHash("sha256").update("session-two").digest("hex"));

  const staleAfterFailure = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(staleAfterFailure.stdout).decision, "block");
  const preservedSessionAfterFailure = await runHook("stop", {
    ...common,
    session_id: "session-two",
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(preservedSessionAfterFailure.stdout, "");

  const unrelatedFailure = await runHook("post-tool-failure", {
    ...common,
    hook_event_name: "PostToolUseFailure",
    tool_name: "Read",
    error: "missing file"
  }, environment);
  assert.strictEqual(unrelatedFailure.stdout, "");

  await runHook("post-tool", tool, environment);
  await runHook("post-tool", otherSessionTool, environment);
  const missingReceipt = await runHook("post-tool", {
    ...tool,
    tool_use_id: "tool-missing-receipt",
    tool_response: { structuredContent: { release_allowed: false, findings: ["native-review-required"] } }
  }, environment);
  assert.strictEqual(JSON.parse(missingReceipt.stdout).decision, "block");
  assert(!missingReceipt.stdout.includes(clean), "receipt rejection must not expose candidate text");
  const remainingAfterMissingReceipt = fs.readdirSync(stateDirectory).filter((name) => name.endsWith(".json"));
  assert.strictEqual(remainingAfterMissingReceipt.length, 1, "logical release failure may clear only the same session and agent");
  const preservedAfterMissingReceipt = JSON.parse(fs.readFileSync(path.join(stateDirectory, remainingAfterMissingReceipt[0]), "utf8"));
  assert.strictEqual(preservedAfterMissingReceipt.session_sha256, crypto.createHash("sha256").update("session-two").digest("hex"));
  const staleAfterMissingReceipt = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(staleAfterMissingReceipt.stdout).decision, "block");
  const parallelAfterMissingReceipt = await runHook("stop", {
    ...common,
    session_id: "session-two",
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(parallelAfterMissingReceipt.stdout, "");

  await runHook("post-tool", tool, environment);
  const rejectedReceipt = await runHook("post-tool", {
    ...tool,
    tool_use_id: "tool-rejected-receipt",
    tool_response: { structuredContent: { release_allowed: true, release_token: "forged" } }
  }, environment);
  assert.strictEqual(JSON.parse(rejectedReceipt.stdout).decision, "block");
  const staleAfterRejectedReceipt = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(staleAfterRejectedReceipt.stdout).decision, "block");

  const slowValid = {
    ...tool,
    tool_use_id: "tool-slow-valid",
    tool_response: { structuredContent: { release_allowed: true, release_token: "slow-valid-token" } }
  };
  const slowRejected = {
    ...tool,
    tool_use_id: "tool-slow-rejected",
    tool_response: { structuredContent: { release_allowed: true, release_token: "slow-rejected-token" } }
  };
  const [parallelSuccess, parallelRejection] = await Promise.all([
    runHook("post-tool", slowValid, environment),
    runHook("post-tool", slowRejected, environment)
  ]);
  assert.strictEqual(parallelSuccess.stdout, "");
  assert.strictEqual(JSON.parse(parallelRejection.stdout).decision, "block");
  const staleAfterParallelRejection = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(staleAfterParallelRejection.stdout).decision, "block");

  fs.mkdirSync(stateFile);
  const unclearedPriorState = await runHook("post-tool", tool, environment);
  assert.strictEqual(JSON.parse(unclearedPriorState.stdout).decision, "block");
  assert.match(unclearedPriorState.stdout, /could not clear the prior release/);
  fs.rmSync(stateFile, { recursive: true, force: true });

  await runHook("post-tool", tool, environment);
  const consumedRecord = fs.readFileSync(stateFile, "utf8");

  const released = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean.replace("\n", "\r\n"),
    stop_hook_active: false
  }, environment);
  assert.strictEqual(released.stdout, "");

  fs.writeFileSync(stateFile, consumedRecord, { mode: 0o600 });
  const replay = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(replay.stdout).decision, "block");

  await runHook("post-tool", tool, environment);
  const changed = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: `${clean} Geändert.`,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(changed.stdout).decision, "block");

  const stoppedAfterFailedCorrection = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: `${clean} Noch immer ungeprüft.`,
    stop_hook_active: true
  }, environment);
  const hardStop = JSON.parse(stoppedAfterFailedCorrection.stdout);
  assert.strictEqual(hardStop.continue, false);
  assert.match(hardStop.stopReason, /stopped an unverified response/);
  assert(!hardStop.stopReason.includes(clean), "hard-stop reason must not expose candidate text");
  assert.strictEqual(hardStop.decision, undefined);

  await runHook("post-tool", tool, environment);
  const correctedDuringStopContinuation = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: true
  }, environment);
  assert.strictEqual(correctedDuringStopContinuation.stdout, "");

  const stoppedSubagent = await runHook("stop", {
    ...common,
    agent_id: "child-agent",
    hook_event_name: "SubagentStop",
    last_assistant_message: "Der Unteragent versucht weiterhin eine ungeprüfte Antwort auszugeben.",
    stop_hook_active: true
  }, environment);
  const subagentHardStop = JSON.parse(stoppedSubagent.stdout);
  assert.strictEqual(subagentHardStop.continue, false);
  assert.match(subagentHardStop.stopReason, /stopped an unverified response/);

  await runHook("post-tool", tool, environment);
  const noLanguage = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "```\n1234\n```",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(noLanguage.stdout, "");
  const delayedAfterCode = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(delayedAfterCode.stdout).decision, "block");

  const forgedRecord = JSON.parse(copiedRecord);
  forgedRecord.delivery_grant = "forged-delivery-grant";
  forgedRecord.authorized_at = Date.now();
  fs.writeFileSync(stateFile, `${JSON.stringify(forgedRecord)}\n`, { mode: 0o600 });
  const forgedState = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(forgedState.stdout).decision, "block");

  const source = "Die Größe des Gebäudes wird täglich geprüft.";
  const translation = "Byggnadens storlek kontrolleras dagligen.";
  const translationTool = {
    ...common,
    hook_event_name: "PostToolUse",
    tool_name: "mcp__plugin_translate-native_guard__release_translation",
    tool_use_id: "tool-translation",
    tool_input: {
      source_text: source,
      target_text: translation,
      language: "sv-SE",
      content_type: "prose",
      attestations: {
        meaning: true,
        completeness: true,
        precision: true,
        nativeness: true,
        locale_fit: true,
        orthography: true,
        integrity: true
      }
    },
    tool_response: {
      structuredContent: { release_allowed: true, release_token: "valid-token" }
    }
  };
  await runHook("post-tool", translationTool, environment);
  const translationRecord = fs.readFileSync(stateFile, "utf8");
  assert(!translationRecord.includes(source), "hook state must not persist the source text");
  const translated = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: translation,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(translated.stdout, "");

  for (const [field, value] of [
    ["source_sha256", "0".repeat(64)],
    ["language", "de-DE"],
    ["task_kind", "response"],
    ["content_type", "title"],
    ["short_text_reviewed", true],
    ["channel", "other-hook"]
  ]) {
    await runHook("post-tool", translationTool, environment);
    const changedRecord = JSON.parse(fs.readFileSync(stateFile, "utf8"));
    changedRecord[field] = value;
    fs.writeFileSync(stateFile, `${JSON.stringify(changedRecord)}\n`, { mode: 0o600 });
    const contextualTamper = await runHook("stop", {
      ...common,
      hook_event_name: "Stop",
      last_assistant_message: translation,
      stop_hook_active: false
    }, environment);
    assert.strictEqual(JSON.parse(contextualTamper.stdout).decision, "block", field);
  }

  await runHook("post-tool", tool, environment);
  await new Promise((resolve) => server.close(resolve));
  const failedTurnWithoutGuard = await runHook("stop-failure", {
    ...common,
    hook_event_name: "StopFailure",
    error: "server_error",
    error_details: "guard also unavailable"
  }, environment);
  assert.strictEqual(failedTurnWithoutGuard.stdout, "");
  assert(!fs.existsSync(path.join(stateDirectory, mainEpochFile)), "failed StopFailure rotation must remove the old local epoch");
  const verifierUnavailable = await runHook("post-tool", {
    ...tool,
    tool_use_id: "tool-verifier-unavailable"
  }, environment);
  assert.strictEqual(JSON.parse(verifierUnavailable.stdout).decision, "block");
  const staleAfterVerifierUnavailable = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean,
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(staleAfterVerifierUnavailable.stdout).decision, "block");
  const unavailableSubagent = await runHook("subagent-start", {
    ...common,
    hook_event_name: "SubagentStart",
    agent_id: "offline-child",
    agent_type: "general-purpose"
  }, environment);
  const unavailableContext = JSON.parse(unavailableSubagent.stdout).hookSpecificOutput;
  assert.strictEqual(unavailableContext.hookEventName, "SubagentStart");
  assert.match(unavailableContext.additionalContext, /isolated guard is unavailable/);
  assert.match(unavailableContext.additionalContext, /Fail closed/);
  fs.rmSync(temporary, { recursive: true, force: true });
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
