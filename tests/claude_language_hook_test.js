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
  const token = "a".repeat(64);
  fs.writeFileSync(path.join(temporary, "service.token"), `${token}\n`, { mode: 0o600 });
  const grants = new Map();

  const server = net.createServer((client) => {
    let raw = "";
    client.on("data", (chunk) => {
      raw += chunk.toString("utf8");
      const newline = raw.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(raw.slice(0, newline));
      let response;
      if (request.operation === "health") {
        response = { status: "ok", isolated_key: true, version: "6.14.0" };
      } else if (request.operation === "authorize_delivery") {
        const valid = request.service_token === token && request.release_token === "valid-token";
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
          && expected.agent_id === request.agent_id
          && expected.channel === request.channel;
        if (valid) grants.delete(request.delivery_grant);
        response = { status: valid ? "PASS" : "BLOCK", valid: Boolean(valid) };
      } else {
        response = { status: "BLOCK", valid: false };
      }
      client.end(`${JSON.stringify(response)}\n`);
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

  const recorded = await runHook("post-tool", tool, environment);
  assert.strictEqual(recorded.code, 0, recorded.stderr);
  assert.strictEqual(recorded.stdout, "");
  const stateDirectory = path.join(temporary, "claude-hooks");
  const stateFile = path.join(stateDirectory, fs.readdirSync(stateDirectory)[0]);
  const copiedRecord = fs.readFileSync(stateFile, "utf8");
  const parsedRecord = JSON.parse(copiedRecord);
  assert.strictEqual(parsedRecord.language, "de-DE");
  assert.strictEqual(parsedRecord.task_kind, "response");
  assert.strictEqual(parsedRecord.session_sha256, crypto.createHash("sha256").update("session-one").digest("hex"));
  assert.strictEqual(parsedRecord.source_sha256, crypto.createHash("sha256").update("").digest("hex"));
  assert.strictEqual(parsedRecord.channel, "claude-hook");

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

  const badReceipt = await runHook("post-tool", {
    ...tool,
    tool_response: { structuredContent: { release_allowed: true, release_token: "forged" } }
  }, environment);
  assert.strictEqual(JSON.parse(badReceipt.stdout).decision, "block");

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

  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(temporary, { recursive: true, force: true });
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
