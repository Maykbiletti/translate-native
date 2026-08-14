#!/usr/bin/env node
"use strict";

// Claude lifecycle boundary: exchange independently verified MCP releases for
// service-owned one-time delivery grants, then consume one for the exact output.

const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_RECORD_AGE_MS = 10 * 60 * 1000;
const DEFAULT_RUNTIME = path.join(os.homedir(), ".config", "blun-language-guard");
let currentHookInput = null;

function canonicalText(value) {
  return String(value || "")
    .replace(/^\uFEFF/, "")
    .replace(/\r\n?/g, "\n")
    .normalize("NFC");
}

function textHash(value) {
  return crypto.createHash("sha256").update(canonicalText(value), "utf8").digest("hex");
}

function hasNaturalLanguage(value) {
  return /\p{L}/u.test(String(value || ""));
}

function safeReadJson(file) {
  const raw = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "");
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("JSON root must be an object");
  }
  return parsed;
}

function runtimeConfig() {
  const runtime = process.env.BLUN_LANGUAGE_GUARD_RUNTIME || DEFAULT_RUNTIME;
  const policyPath = process.env.BLUN_LANGUAGE_GUARD_POLICY || path.join(runtime, "delivery-policy.json");
  const policy = safeReadJson(policyPath);
  const isolated = policy.isolated_service;
  if (policy.mandatory !== true || !isolated || isolated.required !== true) {
    throw new Error("mandatory isolated-service policy is missing");
  }
  const endpoint = process.env.BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT || isolated.endpoint;
  const tokenFile = process.env.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE || isolated.token_file;
  const token = process.env.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN || fs.readFileSync(tokenFile, "utf8").replace(/^\uFEFF/, "").trim();
  if (!endpoint || token.length < 32) {
    throw new Error("isolated service endpoint or token is invalid");
  }
  return { endpoint, token, runtime };
}

function parseEndpoint(endpoint) {
  if (endpoint.startsWith("unix:") && endpoint.length > 5) {
    return { path: endpoint.slice(5) };
  }
  if (endpoint.startsWith("tcp:")) {
    const value = endpoint.slice(4);
    const split = value.lastIndexOf(":");
    const host = value.slice(0, split);
    const port = Number(value.slice(split + 1));
    if (!["127.0.0.1", "localhost", "::1"].includes(host) || !Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error("guard service TCP endpoint must use loopback");
    }
    return { host, port };
  }
  throw new Error("guard service endpoint must be a Unix socket or loopback TCP endpoint");
}

function callGuard(request, timeoutMs = 5000) {
  const config = runtimeConfig();
  const address = parseEndpoint(config.endpoint);
  const payload = Buffer.from(JSON.stringify({ ...request, service_token: config.token }) + "\n", "utf8");
  if (payload.length > MAX_INPUT_BYTES) {
    return Promise.reject(new Error("guard request is too large"));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let buffered = Buffer.alloc(0);
    const socket = net.createConnection(address);
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      if (error) reject(error); else resolve(result);
    };
    socket.setTimeout(timeoutMs, () => finish(new Error("guard service timed out")));
    socket.on("error", (error) => finish(error));
    socket.on("connect", () => socket.write(payload));
    socket.on("data", (chunk) => {
      buffered = Buffer.concat([buffered, chunk]);
      if (buffered.length > MAX_INPUT_BYTES) return finish(new Error("guard response is too large"));
      const newline = buffered.indexOf(10);
      if (newline < 0) return;
      try {
        finish(null, JSON.parse(buffered.subarray(0, newline).toString("utf8").replace(/^\uFEFF/, "")));
      } catch (error) {
        finish(new Error("guard service returned invalid JSON"));
      }
    });
    socket.on("end", () => {
      if (!settled) finish(new Error("guard service closed without a response"));
    });
  });
}

function findRelease(value, seen = new Set()) {
  if (typeof value === "string") {
    const trimmed = value.trim().replace(/^\uFEFF/, "");
    if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && trimmed.length <= MAX_INPUT_BYTES) {
      try { return findRelease(JSON.parse(trimmed), seen); } catch (_) { return null; }
    }
    return null;
  }
  if (!value || typeof value !== "object" || seen.has(value)) return null;
  seen.add(value);
  if (value.release_allowed === true && typeof value.release_token === "string") return value;
  const children = Array.isArray(value) ? value : Object.values(value);
  for (const child of children) {
    const found = findRelease(child, seen);
    if (found) return found;
  }
  return null;
}

function stateDirectory() {
  if (process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR) return process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
  return path.join(runtimeConfig().runtime, "claude-hooks");
}

function identity(input) {
  const session = String(input.session_id || "");
  const agent = String(input.agent_id || "main");
  if (!session) throw new Error("Claude hook input has no session_id");
  return crypto.createHash("sha256").update(`${session}\0${agent}`, "utf8").digest("hex");
}

function sessionHash(input) {
  const session = String(input.session_id || "");
  if (!session) throw new Error("Claude hook input has no session_id");
  return crypto.createHash("sha256").update(session, "utf8").digest("hex");
}

function sessionEpochPath(input) {
  return path.join(stateDirectory(), `session-${sessionHash(input)}.epoch`);
}

function readSessionEpoch(input) {
  const destination = sessionEpochPath(input);
  const stats = fs.lstatSync(destination);
  if (!stats.isFile() || stats.isSymbolicLink()) throw new Error("session epoch must be a regular file");
  if (process.platform !== "win32" && (stats.mode & 0o077) !== 0) {
    throw new Error("session epoch permissions are too broad");
  }
  const epoch = fs.readFileSync(destination, "utf8").replace(/^\uFEFF/, "").trim();
  if (!/^[a-f0-9]{64}$/.test(epoch)) throw new Error("session epoch is invalid");
  return { destination, epoch };
}

async function beginSessionEpoch(input) {
  const directory = stateDirectory();
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const destination = sessionEpochPath(input);
  try {
    fs.unlinkSync(destination);
  } catch (error) {
    if (!error || error.code !== "ENOENT") throw error;
  }
  invalidateSessionRecords(input);
  const epoch = crypto.randomBytes(32).toString("hex");
  const registration = await callGuard({
    operation: "register_session_epoch",
    session_id: String(input.session_id || ""),
    session_epoch: epoch
  }, 3000);
  if (registration.status !== "PASS" || registration.registered !== true) {
    throw new Error("isolated guard rejected the session epoch");
  }
  const temporary = `${destination}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temporary, `${epoch}\n`, { encoding: "utf8", mode: 0o600 });
    fs.renameSync(temporary, destination);
  } finally {
    try { fs.unlinkSync(temporary); } catch (error) { if (!error || error.code !== "ENOENT") throw error; }
  }
  try { fs.chmodSync(destination, 0o600); } catch (_) {}
  return epoch;
}

function statePath(input) {
  return path.join(stateDirectory(), `${identity(input)}.json`);
}

function writeRecord(input, record) {
  const directory = stateDirectory();
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const destination = statePath(input);
  const temporary = `${destination}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 });
  fs.renameSync(temporary, destination);
  try { fs.chmodSync(destination, 0o600); } catch (_) {}
}

function readRecord(input) {
  const destination = statePath(input);
  try {
    return { destination, record: safeReadJson(destination) };
  } catch (error) {
    if (error && error.code === "ENOENT") return { destination, record: null };
    throw error;
  }
}

function invalidateAgentRecord(input) {
  const destination = statePath(input);
  try {
    fs.unlinkSync(destination);
  } catch (error) {
    if (!error || error.code !== "ENOENT") throw error;
  }
}

function invalidateSessionRecords(input) {
  const directory = stateDirectory();
  const expectedSession = sessionHash(input);
  const legacyMainRecord = statePath(input);
  let entries;
  try {
    entries = fs.readdirSync(directory, { withFileTypes: true });
  } catch (error) {
    if (error && error.code === "ENOENT") return;
    throw error;
  }
  for (const entry of entries) {
    if ((!entry.isFile() && !entry.isSymbolicLink()) || !entry.name.endsWith(".json")) continue;
    const candidate = path.join(directory, entry.name);
    let belongsToSession = candidate === legacyMainRecord;
    if (!belongsToSession) {
      try {
        const record = safeReadJson(candidate);
        const legacyGrant = typeof record.session_sha256 !== "string"
          && typeof record.delivery_grant === "string"
          && Number.isFinite(record.authorized_at);
        belongsToSession = record.session_sha256 === expectedSession || legacyGrant;
      } catch (_) {
        continue;
      }
    }
    if (!belongsToSession) continue;
    try {
      fs.unlinkSync(candidate);
    } catch (error) {
      if (!error || error.code !== "ENOENT") throw error;
    }
  }
}

function emit(payload) {
  if (payload) process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function blocked(reason) {
  return { decision: "block", reason };
}

function blockedStop(input, reason) {
  const repeatedStop = input && input.stop_hook_active === true
    && ["Stop", "SubagentStop"].includes(String(input.hook_event_name || ""));
  if (!repeatedStop) return blocked(reason);
  return {
    continue: false,
    stopReason: "BLUN Language Guard stopped an unverified response after the protected correction attempt failed. Reconnect or repair the guard, then retry the response."
  };
}

function startupMessage(eventName, healthy) {
  const subject = eventName === "SubagentStart" ? "This subagent" : "This Claude session";
  if (!healthy) {
    return `${subject} is protected by mandatory BLUN Translate Native, but its isolated guard is unavailable. Fail closed: do not finish or deliver natural-language output until the service is healthy.`;
  }
  return `${subject} is protected by mandatory BLUN Translate Native. Before every natural-language final answer call release_response with the exact final text. For translations load the translate-native skill and call release_translation with the complete source and target. Do not rely on another agent's release: the final visible text must use a fresh grant bound to this session and agent identity and remain byte-for-byte equivalent after Unicode normalization to the released target.`;
}

async function startupContext(eventName) {
  try {
    const result = await callGuard({ operation: "health" }, 3000);
    const healthy = result.status === "ok" && result.isolated_key === true;
    emit({
      hookSpecificOutput: {
        hookEventName: eventName,
        additionalContext: startupMessage(eventName, healthy)
      }
    });
  } catch (_) {
    emit({
      hookSpecificOutput: {
        hookEventName: eventName,
        additionalContext: startupMessage(eventName, false)
      }
    });
  }
}

async function sessionStart(input) {
  try {
    await beginSessionEpoch(input);
  } catch (_) {
    emit({
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: "This Claude session is protected by mandatory BLUN Translate Native, but its delivery epoch could not be renewed. Fail closed: do not finish or deliver natural-language output until protected session state is repaired and SessionStart succeeds."
      }
    });
    return;
  }
  return startupContext("SessionStart");
}

async function subagentStart() {
  return startupContext("SubagentStart");
}

function promptBoundary(input) {
  try {
    invalidateSessionRecords(input);
  } catch (_) {
    emit(blocked("BLUN Language Guard could not invalidate release state from the prior turn. The prompt is blocked to prevent cross-turn receipt reuse."));
  }
}

async function stopFailure(input) {
  try {
    await beginSessionEpoch(input);
  } catch (_) {
    // beginSessionEpoch removes the old local marker before service registration.
    // Claude ignores StopFailure output and exit status, so any incomplete
    // rotation remains silent and fail-closed until SessionStart repairs it.
  }
}

function invalidateReleaseState(input, failureReason) {
  try {
    invalidateAgentRecord(input);
    return true;
  } catch (_) {
    emit(blocked(failureReason));
    return false;
  }
}

function rejectRelease(input, reason) {
  if (!invalidateReleaseState(
    input,
    "BLUN Language Guard could not invalidate stale release state after rejecting a release attempt. Do not finish or deliver natural-language output until protected state is repaired."
  )) return;
  emit(blocked(reason));
}

async function postTool(input) {
  const toolName = String(input.tool_name || "");
  const purpose = toolName.endsWith("__release_translation") ? "translation"
    : toolName.endsWith("__release_response") ? "response" : "";
  if (!purpose) return;
  if (!invalidateReleaseState(
    input,
    "BLUN Language Guard could not clear the prior release before processing a new attempt. The new receipt is not trusted; repair protected state and release the exact final text again."
  )) return;
  let sessionEpoch;
  try {
    sessionEpoch = readSessionEpoch(input).epoch;
  } catch (_) {
    rejectRelease(input, "BLUN Language Guard has no valid epoch for this Claude session. Start or resume the session again, then release the exact final text with a fresh receipt.");
    return;
  }
  const args = input.tool_input;
  const release = findRelease(input.tool_response);
  if (!args || typeof args !== "object" || !release) {
    rejectRelease(input, "BLUN Language Guard returned no usable release receipt. Correct the finding and call the proper release tool again.");
    return;
  }
  const target = typeof args.target_text === "string" ? args.target_text : "";
  const source = purpose === "translation" && typeof args.source_text === "string" ? args.source_text : "";
  const language = typeof args.language === "string" ? args.language : "";
  const request = {
    operation: "authorize_delivery",
    task_kind: purpose,
    source_text: source,
    target_text: target,
    language,
    release_token: release.release_token,
    content_type: typeof args.content_type === "string" ? args.content_type : "prose",
    short_text_reviewed: args.short_text_reviewed === true,
    agent_id: String(input.agent_id || "main"),
    session_id: String(input.session_id || ""),
    session_epoch: sessionEpoch,
    channel: "claude-hook"
  };
  try {
    const verification = await callGuard(request);
    if (verification.valid !== true || typeof verification.delivery_grant !== "string") {
      rejectRelease(input, "The isolated BLUN verifier rejected this receipt. Do not reuse it; correct and release the exact final text again.");
      return;
    }
    writeRecord(input, {
      delivery_grant: verification.delivery_grant,
      session_sha256: sessionHash(input),
      session_epoch_sha256: textHash(sessionEpoch),
      source_sha256: textHash(source),
      target_sha256: textHash(target),
      language,
      task_kind: purpose,
      content_type: typeof args.content_type === "string" ? args.content_type : "prose",
      short_text_reviewed: args.short_text_reviewed === true,
      channel: "claude-hook",
      authorized_at: Date.now()
    });
  } catch (_) {
    rejectRelease(input, "The isolated BLUN verifier is unavailable. Fail closed and reconnect the language guard before finishing.");
  }
}

function postToolFailure(input) {
  const toolName = String(input.tool_name || "");
  if (!toolName.endsWith("__release_response") && !toolName.endsWith("__release_translation")) return;
  try {
    invalidateAgentRecord(input);
    emit({
      decision: "block",
      reason: "The BLUN release tool failed, so no response is authorized. Reconnect the language guard and retry the correct release tool with the exact final text.",
      hookSpecificOutput: {
        hookEventName: "PostToolUseFailure",
        additionalContext: "Fail closed after this release-tool failure. Any earlier unconsumed grant for this session and agent has been invalidated. Do not finish, reuse an earlier release, or deliver unchecked natural-language text. Reconnect the BLUN Language Guard, then call release_translation with the complete source and exact target for a translation, or release_response with the exact final answer. Stop and SubagentStop remain the authoritative delivery boundary."
      }
    });
  } catch (_) {
    emit(blocked("BLUN Language Guard could not invalidate stale release state after the release tool failed. Do not finish or deliver natural-language output until the guard and its protected state are repaired."));
  }
}

async function stop(input) {
  const target = typeof input.last_assistant_message === "string" ? input.last_assistant_message : "";
  const naturalLanguage = hasNaturalLanguage(target);
  try {
    const { destination, record } = readRecord(input);
    const { epoch: sessionEpoch } = readSessionEpoch(input);
    const fresh = record && Number.isFinite(record.authorized_at)
      && Date.now() - record.authorized_at >= 0
      && Date.now() - record.authorized_at <= MAX_RECORD_AGE_MS;
    const usable = fresh && typeof record.delivery_grant === "string"
      && record.session_epoch_sha256 === textHash(sessionEpoch);
    try { fs.unlinkSync(destination); } catch (error) { if (!error || error.code !== "ENOENT") throw error; }
    if (usable) {
      try {
        const result = await callGuard({
          operation: "consume_delivery",
          delivery_grant: record.delivery_grant,
          source_sha256: record.source_sha256,
          target_text: target,
          language: record.language,
          task_kind: record.task_kind,
          content_type: record.content_type,
          short_text_reviewed: record.short_text_reviewed === true,
          session_id: String(input.session_id || ""),
          agent_id: String(input.agent_id || "main"),
          session_epoch: sessionEpoch,
          channel: record.channel
        });
        if (naturalLanguage && record.target_sha256 === textHash(target) && result.valid === true) return;
      } catch (error) {
        if (naturalLanguage) throw error;
      }
    }
  } catch (_) {
    if (!naturalLanguage) return;
    emit(blockedStop(input, "The BLUN hook could not verify its protected release state. Fail closed and call the correct release tool again."));
    return;
  }
  if (!naturalLanguage) return;
  emit(blockedStop(input,
    "The exact final response has no fresh verified BLUN receipt. If this is a translation, load translate-native and call release_translation with the complete source and exact final target. Otherwise call release_response with the exact final answer. Do not edit the text after release."
  ));
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) throw new Error("Claude hook input is too large");
    chunks.push(chunk);
  }
  const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8").replace(/^\uFEFF/, ""));
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Claude hook input must be an object");
  return parsed;
}

async function main() {
  const mode = process.argv[2];
  const input = await readInput();
  currentHookInput = input;
  if (mode === "session-start") return sessionStart(input);
  if (mode === "subagent-start") return subagentStart(input);
  if (mode === "prompt-boundary") return promptBoundary(input);
  if (mode === "stop-failure") return stopFailure(input);
  if (mode === "post-tool") return postTool(input);
  if (mode === "post-tool-failure") return postToolFailure(input);
  if (mode === "stop") return stop(input);
  throw new Error("unknown Claude hook mode");
}

if (require.main === module) {
  main().catch(() => {
    emit(blockedStop(currentHookInput, "BLUN language hook failed closed. Repair or reconnect the guard and retry the exact release."));
    process.exitCode = 0;
  });
}

module.exports = { beginSessionEpoch, blockedStop, canonicalText, findRelease, hasNaturalLanguage, invalidateAgentRecord, invalidateSessionRecords, postToolFailure, readSessionEpoch, sessionHash, stopFailure, textHash };
