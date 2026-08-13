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
        response = { status: "ok", isolated_key: true, version: "6.7.0" };
      } else if (request.operation === "authorize_delivery") {
        const valid = request.service_token === token && request.release_token === "valid-token";
        const grant = `grant-${crypto.randomUUID()}`;
        if (valid) grants.set(grant, {
          target_text: request.target_text,
          session_id: request.session_id,
          agent_id: request.agent_id
        });
        response = {
          status: valid ? "PASS" : "BLOCK",
          valid,
          ...(valid ? { delivery_grant: grant } : {})
        };
      } else if (request.operation === "consume_delivery") {
        const expected = grants.get(request.delivery_grant);
        const valid = request.service_token === token && expected
          && expected.target_text === request.target_text
          && expected.session_id === request.session_id
          && expected.agent_id === request.agent_id;
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

  const released = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: clean.replace("\n", "\r\n"),
    stop_hook_active: false
  }, environment);
  assert.strictEqual(released.stdout, "");

  fs.writeFileSync(stateFile, copiedRecord, { mode: 0o600 });
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

  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(temporary, { recursive: true, force: true });
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
