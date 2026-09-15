"use strict";

const crypto = require("node:crypto");
const net = require("node:net");

const MAX_BYTES = 8 * 1024 * 1024;
const EXACT_LANGUAGE = /^(?:[A-Za-z]{2,8}|x)(?:-[A-Za-z0-9]{1,8})*$/;
const SESSION_EPOCH = /^[0-9a-f]{64}$/;
const TRANSLATION_OPERATIONS = new Set([
  "translate", "translation", "localize", "localization", "transcreate",
  "translation-review", "translation-proofread", "i18n", "l10n",
]);
const RESPONSE_OPERATIONS = new Set(["respond", "response", "chat", "answer", "compose"]);
const HOST_FIELDS = new Set([
  "task_kind", "language", "source_text", "content_type",
  "short_text_reviewed", "key_path", "delivery_channel",
  "session_id", "session_epoch", "agent_id",
]);

class LanguageGuardBlocked extends Error {
  constructor(message, code = "language_guard_blocked") {
    super(message);
    this.name = "LanguageGuardBlocked";
    this.code = code;
  }
}

function strictString(value, field, required = false) {
  if (value === undefined || value === null) {
    if (required) throw new LanguageGuardBlocked(`${field} is required`, "invalid_host_context");
    return "";
  }
  if (typeof value !== "string") throw new LanguageGuardBlocked(`${field} must be a string`, "invalid_host_context");
  if (required && !value.trim()) throw new LanguageGuardBlocked(`${field} is required`, "invalid_host_context");
  return value;
}

function routeHostContext(context = {}) {
  if (!context || typeof context !== "object" || Array.isArray(context)) {
    throw new LanguageGuardBlocked("host context must be an object", "invalid_host_context");
  }
  const explicit = strictString(context.task_kind, "task_kind").trim().toLowerCase();
  const operation = strictString(context.operation, "operation").trim().toLowerCase();
  const sourceText = strictString(context.source_text, "source_text");
  const contentType = strictString(context.content_type, "content_type").trim() || "prose";
  if (!new Set(["prose", "title", "meta_description", "ui"]).has(contentType)) {
    throw new LanguageGuardBlocked("invalid content_type", "invalid_host_context");
  }
  const translationEvidence = Boolean(sourceText.trim()) || TRANSLATION_OPERATIONS.has(operation);
  let taskKind;
  if (explicit) {
    if (!new Set(["response", "translation"]).has(explicit)) {
      throw new LanguageGuardBlocked("invalid task_kind", "invalid_host_context");
    }
    taskKind = explicit;
  } else if (translationEvidence) taskKind = "translation";
  else if (!operation || RESPONSE_OPERATIONS.has(operation)) taskKind = "response";
  else throw new LanguageGuardBlocked("unknown host operation", "invalid_host_context");

  if (taskKind === "translation" && !sourceText.trim()) {
    throw new LanguageGuardBlocked("translation route requires complete source_text", "invalid_host_context");
  }
  if (taskKind === "response" && sourceText.trim()) {
    throw new LanguageGuardBlocked("source_text cannot be downgraded to response", "mode_confusion");
  }
  if (taskKind === "response" && TRANSLATION_OPERATIONS.has(operation)) {
    throw new LanguageGuardBlocked("translation operation cannot be downgraded to response", "mode_confusion");
  }
  if (taskKind === "translation" && RESPONSE_OPERATIONS.has(operation)) {
    throw new LanguageGuardBlocked("response operation conflicts with translation source", "mode_confusion");
  }
  const language = strictString(
    taskKind === "translation"
      ? (context.target_language || context.language)
      : (context.response_language || context.language),
    taskKind === "translation" ? "target_language" : "response_language",
    true,
  ).trim();
  if (["auto", "all"].includes(language.toLowerCase()) || !EXACT_LANGUAGE.test(language)) {
    throw new LanguageGuardBlocked("exact language or locale is required", "invalid_host_context");
  }
  return { taskKind, language, sourceText: taskKind === "translation" ? sourceText : "", contentType };
}

function parseAgentEnvelope(raw) {
  const text = Buffer.isBuffer(raw) ? raw.toString("utf8") : String(raw ?? "");
  if (Buffer.byteLength(text, "utf8") > MAX_BYTES) {
    throw new LanguageGuardBlocked("agent envelope is too large", "invalid_envelope");
  }
  let envelope;
  try {
    envelope = JSON.parse(text.replace(/^\uFEFF/, ""));
  } catch {
    throw new LanguageGuardBlocked("agent output is not one JSON release envelope", "invalid_envelope");
  }
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
    throw new LanguageGuardBlocked("agent envelope must be an object", "invalid_envelope");
  }
  for (const field of Object.keys(envelope)) {
    if (HOST_FIELDS.has(field)) {
      throw new LanguageGuardBlocked(`agent attempted to override host field ${field}`, "host_override");
    }
    if (!new Set(["target_text", "release_token"]).has(field)) {
      throw new LanguageGuardBlocked(`unsupported agent envelope field ${field}`, "invalid_envelope");
    }
  }
  if (typeof envelope.target_text !== "string" || !envelope.target_text.trim()) {
    throw new LanguageGuardBlocked("target_text is required", "invalid_envelope");
  }
  if (typeof envelope.release_token !== "string" || !envelope.release_token.trim()) {
    throw new LanguageGuardBlocked("release_token is required", "invalid_envelope");
  }
  return envelope;
}

function endpointOptions(endpoint) {
  const value = String(endpoint || "").trim();
  if (value.startsWith("unix:") && value.slice(5)) return { path: value.slice(5) };
  if (value.startsWith("tcp:")) {
    const match = /^tcp:(127\.0\.0\.1|localhost|::1):(\d+)$/.exec(value);
    const port = match ? Number(match[2]) : 0;
    if (match && port >= 1 && port <= 65535) return { host: match[1], port };
  }
  throw new LanguageGuardBlocked("invalid isolated guard endpoint", "guard_unavailable");
}

function callGuardService(endpoint, request, { serviceToken = "", timeoutMs = 10000 } = {}) {
  const payload = { ...request };
  if (serviceToken) payload.service_token = serviceToken;
  const encoded = `${JSON.stringify(payload)}\n`;
  if (Buffer.byteLength(encoded, "utf8") > MAX_BYTES) {
    return Promise.reject(new LanguageGuardBlocked("guard request is too large", "guard_unavailable"));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let raw = "";
    const socket = net.createConnection(endpointOptions(endpoint));
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      if (error) reject(error);
      else resolve(value);
    };
    const timer = setTimeout(() => finish(new LanguageGuardBlocked("guard service timed out", "guard_unavailable")), timeoutMs);
    timer.unref?.();
    socket.setEncoding("utf8");
    socket.once("connect", () => socket.write(encoded));
    socket.on("data", chunk => {
      raw += chunk;
      if (Buffer.byteLength(raw, "utf8") > MAX_BYTES) {
        finish(new LanguageGuardBlocked("guard response is too large", "guard_unavailable"));
        return;
      }
      const newline = raw.indexOf("\n");
      if (newline < 0) return;
      try {
        const response = JSON.parse(raw.slice(0, newline));
        finish(null, response);
      } catch {
        finish(new LanguageGuardBlocked("guard returned invalid JSON", "guard_unavailable"));
      }
    });
    socket.once("error", () => finish(new LanguageGuardBlocked("guard service is unavailable", "guard_unavailable")));
    socket.once("end", () => {
      if (!settled) finish(new LanguageGuardBlocked("guard closed without a decision", "guard_unavailable"));
    });
  });
}

function exactUtf8Hash(value) {
  return crypto.createHash("sha256").update(String(value), "utf8").digest("hex");
}

function canonicalHash(value) {
  const text = String(value).replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").normalize("NFC");
  return exactUtf8Hash(text);
}

function bindCandidate(text) {
  const sha256 = exactUtf8Hash(text);
  return Object.freeze({ id: `sha256:${sha256}`, sha256, text });
}

function rejectionDetail(result) {
  const failedChecks = result?.checks && typeof result.checks === "object"
    ? Object.entries(result.checks).filter(([, passed]) => passed !== true).map(([name]) => name)
    : [];
  return failedChecks.length ? ` (failed checks: ${failedChecks.join(", ")})` : "";
}

async function verifyForDelivery({ rawEnvelope, hostContext, endpoint, serviceToken = "", agentId = "", channel = "" }) {
  const envelope = parseAgentEnvelope(rawEnvelope);
  const route = routeHostContext(hostContext);
  const candidate = bindCandidate(envelope.target_text);
  const result = await callGuardService(endpoint, {
    operation: "verify",
    task_kind: route.taskKind,
    source_text: route.sourceText,
    target_text: candidate.text,
    language: route.language,
    release_token: envelope.release_token,
    content_type: route.contentType,
    short_text_reviewed: hostContext.short_text_reviewed === true,
    agent_id: String(agentId || ""),
    channel: String(channel || ""),
  }, { serviceToken });
  if (result?.valid !== true) {
    throw new LanguageGuardBlocked(`isolated guard rejected the exact output${rejectionDetail(result)}`, "receipt_rejected");
  }
  return { text: candidate.text, candidate, route, verification: result };
}

async function authorizeForDelivery(options) {
  const envelope = parseAgentEnvelope(options.rawEnvelope);
  const route = routeHostContext(options.hostContext);
  const candidate = bindCandidate(envelope.target_text);
  const delivery = Object.freeze({
    sessionId: strictString(options.sessionId, "session_id", true),
    sessionEpoch: strictString(options.sessionEpoch, "session_epoch", true),
    agentId: strictString(options.agentId, "agent_id", true),
    channel: strictString(options.channel, "delivery_channel", true),
    shortTextReviewed: options.hostContext.short_text_reviewed === true,
  });
  if (!SESSION_EPOCH.test(delivery.sessionEpoch)) {
    throw new LanguageGuardBlocked("session_epoch must be 64 lowercase hexadecimal characters", "invalid_host_context");
  }
  const result = await callGuardService(options.endpoint, {
    operation: "authorize_delivery",
    task_kind: route.taskKind,
    source_text: route.sourceText,
    target_text: candidate.text,
    language: route.language,
    release_token: envelope.release_token,
    content_type: route.contentType,
    short_text_reviewed: delivery.shortTextReviewed,
    session_id: delivery.sessionId,
    session_epoch: delivery.sessionEpoch,
    agent_id: delivery.agentId,
    channel: delivery.channel,
  }, { serviceToken: options.serviceToken || "" });
  if (result?.valid !== true || typeof result.delivery_grant !== "string" || !result.delivery_grant) {
    throw new LanguageGuardBlocked(`isolated guard rejected delivery authorization${rejectionDetail(result)}`, "receipt_rejected");
  }
  return Object.freeze({ candidate, route, delivery, grant: result.delivery_grant, verification: result });
}

async function consumeAuthorizedDelivery(options) {
  const authorization = options.authorization;
  const candidate = options.candidate || authorization?.candidate;
  if (!authorization || !candidate || candidate !== authorization.candidate
      || candidate.id !== `sha256:${exactUtf8Hash(candidate.text)}`) {
    throw new LanguageGuardBlocked("approved candidate changed before delivery", "candidate_changed");
  }
  const { route, delivery } = authorization;
  const result = await callGuardService(options.endpoint, {
    operation: "consume_delivery",
    delivery_grant: authorization.grant,
    source_sha256: canonicalHash(route.sourceText),
    target_text: candidate.text,
    language: route.language,
    task_kind: route.taskKind,
    content_type: route.contentType,
    short_text_reviewed: delivery.shortTextReviewed,
    session_id: delivery.sessionId,
    session_epoch: delivery.sessionEpoch,
    agent_id: delivery.agentId,
    channel: delivery.channel,
  }, { serviceToken: options.serviceToken || "" });
  if (result?.valid !== true) {
    throw new LanguageGuardBlocked(`isolated guard rejected final delivery${rejectionDetail(result)}`, "delivery_rejected");
  }
  return result;
}

function safeCodeUnitLimit(value, limit) {
  let cut = Math.min(limit, value.length);
  if (cut > 0 && cut < value.length) {
    const before = value.charCodeAt(cut - 1);
    const after = value.charCodeAt(cut);
    if (before >= 0xd800 && before <= 0xdbff && after >= 0xdc00 && after <= 0xdfff) cut -= 1;
  }
  return cut || Math.min(value.length, 2);
}

function splitTelegramMessage(value, limit = 3900) {
  if (!Number.isInteger(limit) || limit < 2) {
    throw new LanguageGuardBlocked("Telegram chunk limit must be an integer of at least 2", "delivery_unavailable");
  }
  const chunks = [];
  let remaining = String(value || "");
  while (remaining.length > limit) {
    const hardLimit = safeCodeUnitLimit(remaining, limit);
    const threshold = Math.floor(limit * 0.55);
    let boundary = remaining.lastIndexOf("\n", hardLimit - 1);
    if (boundary < threshold) boundary = remaining.lastIndexOf(" ", hardLimit - 1);
    const cut = boundary >= threshold ? boundary + 1 : hardLimit;
    chunks.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut);
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

async function guardedTelegramSend(options) {
  if (typeof options.telegramRequest !== "function") {
    throw new LanguageGuardBlocked("Telegram transport is unavailable", "delivery_unavailable");
  }
  const authorization = await authorizeForDelivery({ ...options, channel: options.channel || "telegram" });
  const chunks = splitTelegramMessage(authorization.candidate.text, options.chunkLimit || 3900);
  if (chunks.join("") !== authorization.candidate.text
      || exactUtf8Hash(chunks.join("")) !== authorization.candidate.sha256) {
    throw new LanguageGuardBlocked("Telegram framing changed the approved candidate", "candidate_changed");
  }
  const delivery = await consumeAuthorizedDelivery({
    authorization,
    endpoint: options.endpoint,
    serviceToken: options.serviceToken || "",
  });
  let lastResult = null;
  for (const text of chunks) {
    lastResult = await options.telegramRequest(options.botToken, "sendMessage", {
      chat_id: options.chatId,
      text,
      ...(options.topicId ? { message_thread_id: options.topicId } : {}),
      ...(options.replyParameters ? { reply_parameters: options.replyParameters } : {}),
    });
  }
  return {
    sent: true,
    chunks: chunks.length,
    lastResult,
    candidateId: authorization.candidate.id,
    candidateSha256: authorization.candidate.sha256,
    delivery,
  };
}

module.exports = {
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
};
