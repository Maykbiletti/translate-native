"use strict";

const net = require("node:net");

const MAX_BYTES = 8 * 1024 * 1024;
const EXACT_LANGUAGE = /^(?:[A-Za-z]{2,8}|x)(?:-[A-Za-z0-9]{1,8})*$/;
const TRANSLATION_OPERATIONS = new Set([
  "translate", "translation", "localize", "localization", "transcreate",
  "translation-review", "translation-proofread", "i18n", "l10n",
]);
const RESPONSE_OPERATIONS = new Set(["respond", "response", "chat", "answer", "compose"]);
const HOST_FIELDS = new Set([
  "task_kind", "language", "source_text", "content_type",
  "short_text_reviewed", "key_path", "delivery_channel",
]);

class LanguageGuardBlocked extends Error {
  constructor(message, code = "language_guard_blocked") {
    super(message);
    this.name = "LanguageGuardBlocked";
    this.code = code;
  }
}

function strictString(value, field, required = false) {
  if (value === undefined || value === null) return "";
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

async function verifyForDelivery({ rawEnvelope, hostContext, endpoint, serviceToken = "", agentId = "", channel = "" }) {
  const envelope = parseAgentEnvelope(rawEnvelope);
  const route = routeHostContext(hostContext);
  const result = await callGuardService(endpoint, {
    operation: "verify",
    task_kind: route.taskKind,
    source_text: route.sourceText,
    target_text: envelope.target_text,
    language: route.language,
    release_token: envelope.release_token,
    content_type: route.contentType,
    short_text_reviewed: hostContext.short_text_reviewed === true,
    agent_id: String(agentId || ""),
    channel: String(channel || ""),
  }, { serviceToken });
  if (result?.valid !== true) {
    throw new LanguageGuardBlocked("isolated guard rejected the exact output", "receipt_rejected");
  }
  return { text: envelope.target_text, route, verification: result };
}

function splitTelegramMessage(value, limit = 3900) {
  const chunks = [];
  let remaining = String(value || "").trim();
  while (remaining.length > limit) {
    let splitAt = remaining.lastIndexOf("\n", limit);
    if (splitAt < Math.floor(limit * 0.55)) splitAt = remaining.lastIndexOf(" ", limit);
    if (splitAt < Math.floor(limit * 0.55)) splitAt = limit;
    chunks.push(remaining.slice(0, splitAt).trimEnd());
    remaining = remaining.slice(splitAt).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

async function guardedTelegramSend(options) {
  const verified = await verifyForDelivery({ ...options, channel: options.channel || "telegram" });
  if (typeof options.telegramRequest !== "function") {
    throw new LanguageGuardBlocked("Telegram transport is unavailable", "delivery_unavailable");
  }
  let lastResult = null;
  for (const text of splitTelegramMessage(verified.text)) {
    lastResult = await options.telegramRequest(options.botToken, "sendMessage", {
      chat_id: options.chatId,
      text,
      ...(options.topicId ? { message_thread_id: options.topicId } : {}),
      ...(options.replyParameters ? { reply_parameters: options.replyParameters } : {}),
    });
  }
  return { sent: true, chunks: splitTelegramMessage(verified.text).length, lastResult, verification: verified.verification };
}

module.exports = {
  LanguageGuardBlocked,
  routeHostContext,
  parseAgentEnvelope,
  callGuardService,
  verifyForDelivery,
  splitTelegramMessage,
  guardedTelegramSend,
};
